"""migrate.py — one-time migration from legacy data files to agent.db / analytics.duckdb.

Migrates:
  trainer.db           → agent.db  (few_shot_examples, word_counts, hotwords)
  routing_log.jsonl    → agent.db  (commands, under a synthetic session id=-1)
  gesture_calibration.json → agent.db  (gesture_calibration)
  benchmark_results.json   → analytics.duckdb  (benchmark_runs / results / prompts)

Run once, verify counts, then delete the legacy files.

Usage:
    python migrate.py [--dry-run] [--delete-legacy]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import time
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _count_rows(db_path: Path, table: str) -> int:
    try:
        con = sqlite3.connect(db_path)
        n = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        con.close()
        return n
    except Exception:
        return -1


async def _migrate_trainer_db(agent_db, trainer_path: Path) -> dict[str, int]:
    """Copy few_shot_examples, word_counts, hotwords from trainer.db."""
    counts: dict[str, int] = {}
    if not trainer_path.exists():
        print(f"  [SKIP] {trainer_path} not found")
        return counts

    src = sqlite3.connect(trainer_path)
    src.row_factory = sqlite3.Row

    # Examples → few_shot_examples
    rows = src.execute("SELECT * FROM examples").fetchall()
    for r in rows:
        await agent_db._conn.execute(
            """INSERT OR IGNORE INTO few_shot_examples
               (text, action, source, domain, ts, usage_count)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (r["text"], r["action"], r["source"], r["domain"], r["ts"], r["usage_count"]),
        )
    await agent_db._conn.commit()
    counts["few_shot_examples"] = len(rows)

    # word_counts
    rows = src.execute("SELECT * FROM word_counts").fetchall()
    for r in rows:
        await agent_db._conn.execute(
            "INSERT OR IGNORE INTO word_counts (word, count) VALUES (?, ?)",
            (r["word"], r["count"]),
        )
    await agent_db._conn.commit()
    counts["word_counts"] = len(rows)

    # hotwords
    rows = src.execute("SELECT * FROM hotwords").fetchall()
    for r in rows:
        await agent_db._conn.execute(
            "INSERT OR IGNORE INTO hotwords (word, added) VALUES (?, ?)",
            (r["word"], r["added"]),
        )
    await agent_db._conn.commit()
    counts["hotwords"] = len(rows)

    src.close()
    return counts


async def _migrate_routing_log(agent_db, log_path: Path, session_id: int) -> int:
    """Parse routing_log.jsonl → commands table under the migration session."""
    if not log_path.exists():
        print(f"  [SKIP] {log_path} not found")
        return 0
    count = 0
    with log_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            action = e.get("action", "")
            success = None
            error_msg = None
            if action.startswith("CLARIFY inference error"):
                success = 0
                error_msg = action
                action = "CLARIFY"
            elif action:
                success = 1
            await agent_db._conn.execute(
                """INSERT INTO commands
                   (session_id, ts, source, text, action,
                    route, gate_that_decided, latency_ms,
                    whisper_logprob, gesture_confidence, success, error_msg)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    session_id,
                    e.get("ts", time.time()),
                    e.get("source", "unknown"),
                    e.get("text", ""),
                    action or None,
                    e.get("route"),
                    e.get("gate_that_decided"),
                    e.get("latency_ms"),
                    e.get("whisper_logprob"),
                    e.get("gesture_confidence"),
                    success,
                    error_msg,
                ),
            )
            count += 1
    await agent_db._conn.commit()
    return count


async def _migrate_gesture_calibration(agent_db, cal_path: Path) -> int:
    """Parse gesture_calibration.json → gesture_calibration rows."""
    if not cal_path.exists():
        print(f"  [SKIP] {cal_path} not found")
        return 0
    try:
        data = json.loads(cal_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"  [SKIP] gesture_calibration.json parse error: {exc}")
        return 0

    saved_at = data.get("saved_at", time.time())
    count = 0
    for gesture, entry in data.get("gestures", {}).items():
        floor = entry.get("confidence_floor", 0.60)
        sample_count = entry.get("sample_count", 0)
        samples = entry.get("samples", [])
        p10 = None
        if samples:
            s = sorted(samples)
            p10_idx = max(0, int(len(s) * 0.10) - 1)
            p10 = s[p10_idx]
        await agent_db._conn.execute(
            """INSERT INTO gesture_calibration
               (ts, gesture, confidence_floor, sample_count, p10)
               VALUES (?, ?, ?, ?, ?)""",
            (saved_at, gesture, floor, sample_count, p10),
        )
        count += 1
    await agent_db._conn.commit()
    return count


def _migrate_benchmark_results(analytics, bench_path: Path) -> int:
    """Parse benchmark_results.json → analytics.duckdb."""
    if not bench_path.exists():
        print(f"  [SKIP] {bench_path} not found")
        return 0
    if not analytics.available:
        print("  [SKIP] AnalyticsDB unavailable")
        return 0
    try:
        all_results = json.loads(bench_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"  [SKIP] benchmark_results.json parse error: {exc}")
        return 0

    run_id = analytics.insert_benchmark_run(
        ts=bench_path.stat().st_mtime,
        notes="migrated from benchmark_results.json",
    )
    count = 0
    for r in all_results:
        if "error" in r:
            analytics.insert_benchmark_result(
                run_id=run_id, model=r["model"],
                accuracy_pct=None, correct=None, total=None,
                p50_ms=None, p95_ms=None,
                vram_before_gb=None, vram_after_gb=None, vram_delta_gb=None,
                error=r["error"],
            )
            count += 1
            continue
        result_id = analytics.insert_benchmark_result(
            run_id=run_id,
            model=r["model"],
            accuracy_pct=r.get("accuracy_pct"),
            correct=r.get("correct"),
            total=r.get("total"),
            p50_ms=r.get("p50_ms"),
            p95_ms=r.get("p95_ms"),
            vram_before_gb=r.get("vram_before_gb"),
            vram_after_gb=r.get("vram_after_load_gb"),
            vram_delta_gb=r.get("vram_delta_gb"),
        )
        for p in r.get("prompts", []):
            analytics.insert_benchmark_prompt(
                result_id=result_id,
                prompt=p["prompt"],
                expected=p["expected"],
                got=p.get("got"),
                correct=bool(p.get("correct")),
                p50_ms=p.get("p50_ms"),
                p95_ms=p.get("p95_ms"),
            )
        count += 1
    return count


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def _run(dry_run: bool, delete_legacy: bool) -> None:
    from db import AgentDB, AnalyticsDB

    print("\n=== Personal Desktop Agent — Legacy Data Migration ===\n")

    if dry_run:
        print("  DRY RUN — no files will be written or deleted\n")

    legacy_files = {
        "trainer.db": Path("trainer.db"),
        "routing_log.jsonl": Path("routing_log.jsonl"),
        "gesture_calibration.json": Path("gesture_calibration.json"),
        "benchmark_results.json": Path("benchmark_results.json"),
    }

    print("  Legacy files found:")
    for name, p in legacy_files.items():
        status = f"  {p.stat().st_size:>10,} bytes" if p.exists() else "  NOT FOUND"
        print(f"    {name:<30}{status}")
    print()

    if dry_run:
        print("  [DRY RUN] Exiting without changes.")
        return

    # Open new databases
    agent_db = AgentDB()
    await agent_db.open(Path("agent.db"))

    analytics = AnalyticsDB()
    analytics.open(Path("analytics.duckdb"))

    # Create migration session (id may not be -1; we use whatever is assigned)
    session_id = await agent_db.insert_session(
        mode="migration",
        notes="Legacy data migrated from trainer.db + routing_log.jsonl",
    )
    print(f"  Migration session id: {session_id}")

    # --- Migrate trainer.db ---
    print("\n  Migrating trainer.db ...")
    counts = await _migrate_trainer_db(agent_db, legacy_files["trainer.db"])
    for table, n in counts.items():
        print(f"    {table}: {n} rows")

    # --- Migrate routing_log.jsonl ---
    print("\n  Migrating routing_log.jsonl ...")
    n = await _migrate_routing_log(agent_db, legacy_files["routing_log.jsonl"], session_id)
    print(f"    commands: {n} rows")

    # --- Migrate gesture_calibration.json ---
    print("\n  Migrating gesture_calibration.json ...")
    n = await _migrate_gesture_calibration(agent_db, legacy_files["gesture_calibration.json"])
    print(f"    gesture_calibration: {n} rows")

    # --- Migrate benchmark_results.json ---
    print("\n  Migrating benchmark_results.json ...")
    n = _migrate_benchmark_results(analytics, legacy_files["benchmark_results.json"])
    print(f"    benchmark_results: {n} model rows")

    # --- Verification counts ---
    await agent_db.close_session(session_id)
    await agent_db.close()
    analytics.close()

    print("\n  Verification (agent.db row counts):")
    for table in ("sessions", "commands", "few_shot_examples",
                  "word_counts", "hotwords", "gesture_calibration"):
        n = _count_rows(Path("agent.db"), table)
        print(f"    {table:<30} {n:>6} rows")

    print()
    if delete_legacy:
        for name, p in legacy_files.items():
            if p.exists():
                p.unlink()
                print(f"  Deleted: {p}")
        print()
    else:
        print("  Legacy files NOT deleted. Re-run with --delete-legacy to remove them.")
        print("  Verify counts above first.\n")


def main() -> None:
    p = argparse.ArgumentParser(description="Migrate legacy data to agent.db / analytics.duckdb")
    p.add_argument("--dry-run", action="store_true", help="Show what would be migrated, without writing")
    p.add_argument("--delete-legacy", action="store_true", help="Delete legacy files after migration")
    args = p.parse_args()
    asyncio.run(_run(args.dry_run, args.delete_legacy))


if __name__ == "__main__":
    main()
