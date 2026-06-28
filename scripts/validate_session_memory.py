#!/usr/bin/env python3
"""Validate cross-session working-memory relevance quality (Gap C, task 9).

`specs/resume-working-memory/` task 9 holds a DECISION (Brad): flip
`DA_SESSION_MEMORY` default ON only *after* validating relevance quality on real
back-to-back related runs. This harness produces the evidence for that decision.

It mirrors `DevAgent._session_seed_context` (inference/dev_agent.py) EXACTLY —
same `get_recent_runs(limit=20)` candidate set, same
`select_related_runs(top_k=3, min_score=0.2)`, same `summarize_run` /
`render_session_seed` — but runs OFFLINE and read-only over the live `agent.db`,
treating each recent run as a hypothetical "new goal" and reporting:

  - which prior runs would be selected (with their Jaccard relevance score), and
  - the exact `<prior-session-memory>` block that would be injected.

Brad eyeballs whether the selected runs are genuinely relevant and the seed is
useful. If yes for real dev runs → flip the default ON (task 9). If the corpus
has no suitable runs yet, the harness says so plainly (the precondition is unmet).

Modes:
  --from-db [--db PATH] [--limit N] [--sample M]
        Real validation. Scan the live DB and, for the M most-recent runs that
        have steps, show what the session seed would have been. This is the run
        whose output drives the flip-the-default decision.

  --synthetic
        Smoke / reference. Run the same pipeline over a crafted set of related
        multi-step dev runs so you can see what a GOOD seed looks like (proves
        the pipeline end-to-end, independent of whatever is in the live DB).

Pure read-only: opens the DB, never writes. No schema touch. No LLM, no model
load — the production path is deterministic and so is this.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

# The rendered seed contains unicode (— / →); force UTF-8 so it prints on a
# Windows cp1252 console without mangling.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# Import the SAME pure helpers production uses, so the harness can never drift
# from the real behavior.
_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from inference.working_memory import (  # noqa: E402
    select_related_runs,
    summarize_run,
    render_session_seed,
    score_relevance,
)

# Production constants (must match _session_seed_context + select_related_runs).
RECENT_LIMIT = 20
TOP_K = 3
MIN_SCORE = 0.2


def _load_recent_runs(conn: sqlite3.Connection, limit: int) -> list[dict]:
    """Replicate AgentDB.get_recent_runs (storage/db.py) read-only."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT id, goal, ts, success, status
           FROM agent_runs
           WHERE status IN ('completed','failed','interrupted')
           ORDER BY ts DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def _load_steps(conn: sqlite3.Connection, run_id: int) -> list[dict]:
    """Replicate AgentDB.get_steps_for_run (storage/db.py) read-only."""
    rows = conn.execute(
        """SELECT step_num, action, args, body, result, success
           FROM agent_steps WHERE run_id = ? ORDER BY step_num ASC""",
        (run_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _seed_for_goal(goal: str, candidates: list[dict], steps_loader) -> tuple[str, list[dict]]:
    """Mirror DevAgent._session_seed_context for one `goal` over `candidates`.

    Returns (rendered_seed_block, selected_runs). `steps_loader(run_id)->steps`.
    """
    related = select_related_runs(goal, candidates, top_k=TOP_K, min_score=MIN_SCORE)
    if not related:
        return "", []
    mems: list[tuple[str, object]] = []
    for run in related:
        run_id = run.get("id")
        if run_id is None:
            continue
        steps = steps_loader(int(run_id))
        if not steps:
            continue
        run_goal = run.get("goal", "") or ""
        mems.append((run_goal, summarize_run(run_goal, steps)))
    return render_session_seed(mems), related


def _report_scenario(goal: str, candidates: list[dict], steps_loader) -> bool:
    """Print one new-goal scenario. Returns True if a non-empty seed was produced."""
    print("=" * 78)
    print(f"NEW GOAL: {goal!r}")
    # Show the scored field so relevance quality is auditable, not just the cutoff.
    scored = sorted(
        ((score_relevance(goal, c.get("goal", "") or ""), c) for c in candidates),
        key=lambda t: t[0], reverse=True,
    )
    print(f"  candidates scanned: {len(candidates)} (top scoring shown)")
    for score, c in scored[:6]:
        mark = "SELECT" if score >= MIN_SCORE else "  skip"
        print(f"    [{mark} {score:0.2f}] run {c.get('id')}: {(c.get('goal') or '')[:55]!r}")
    seed, selected = _seed_for_goal(goal, candidates, steps_loader)
    if not seed:
        why = "no candidate cleared min_score" if not selected else \
              "selected runs had no derivable memory (no steps / no file touches)"
        print(f"  -> SEED: (empty) — {why}")
        return False
    print("  -> SEED block injected into the planner context:")
    for line in seed.splitlines():
        print(f"     | {line}")
    return True


def run_from_db(db_path: str, limit: int, sample: int) -> int:
    p = Path(db_path)
    if not p.exists():
        print(f"ERROR: {p} not found", file=sys.stderr)
        return 2
    conn = sqlite3.connect(str(p))
    try:
        candidates = _load_recent_runs(conn, limit)
        with_steps = [
            r for r in candidates
            if conn.execute(
                "SELECT 1 FROM agent_steps WHERE run_id=? LIMIT 1", (r["id"],)
            ).fetchone()
        ]
        total = conn.execute(
            "SELECT count(*) FROM agent_runs "
            "WHERE status IN ('completed','failed','interrupted')"
        ).fetchone()[0]
        print(f"DB: {p}  |  terminal runs: {total}  |  recent scanned: {len(candidates)}")
        print(f"recent runs that have ANY steps: {len(with_steps)}\n")

        if not with_steps:
            print("VERDICT: corpus insufficient to validate relevance quality.")
            print("  None of the recent runs has persisted steps, so every session")
            print("  seed derives to empty regardless of relevance matching. The")
            print("  feature is a no-op against this history.")
            print("  Precondition for task 9 (real back-to-back related dev runs)")
            print("  is UNMET — keep DA_SESSION_MEMORY default OFF.\n")
            print("  To generate validation data, run a few related multi-FILE dev")
            print("  tasks back-to-back through the DevAgent (e.g. 'add a helper to")
            print("  X.py', then 'write a test for that helper'), then re-run with")
            print("  --from-db.")
            return 1

        steps_loader = lambda rid: _load_steps(conn, rid)  # noqa: E731
        scenarios = with_steps[:sample]
        produced = 0
        for run in scenarios:
            goal = run.get("goal", "") or ""
            # Exclude the run itself from its own candidate pool (production
            # never sees the current run as a candidate — its row isn't created
            # until after context assembly).
            pool = [c for c in candidates if c["id"] != run["id"]]
            if _report_scenario(goal, pool, steps_loader):
                produced += 1
        print("=" * 78)
        print(f"\nSUMMARY: {produced}/{len(scenarios)} scenarios produced a non-empty seed.")
        if produced == 0:
            print("VERDICT: no useful seed produced — keep DA_SESSION_MEMORY OFF.")
        else:
            print("VERDICT: inspect the seeds above. If the selected prior runs are")
            print("  genuinely relevant and the rendered facts are useful, flip")
            print("  DA_SESSION_MEMORY default ON (task 9). If they are noisy/off-topic,")
            print("  raise min_score or keep OFF.")
        return 0
    finally:
        conn.close()


def run_synthetic() -> int:
    """Crafted related dev runs — shows what a GOOD seed looks like end-to-end."""
    print("SYNTHETIC SMOKE — crafted related multi-step dev runs\n")
    prior_runs = [
        {
            "id": 101, "ts": 1000.0, "goal": "add a retry helper to inference/net.py",
            "steps": [
                {"step_num": 1, "action": "READ_FILE", "args": "inference/net.py",
                 "result": "module with one function", "success": 1},
                {"step_num": 2, "action": "WRITE_FILE", "args": "inference/net.py",
                 "result": "added retry_with_backoff()", "success": 1},
            ],
        },
        {
            "id": 102, "ts": 1001.0, "goal": "fix the timeout bug in core/poller.py",
            "steps": [
                {"step_num": 1, "action": "EDIT_FILE", "args": "core/poller.py",
                 "result": "patched timeout", "success": 0,
                 },
            ],
        },
        {
            "id": 103, "ts": 1002.0, "goal": "update README badges",
            "steps": [
                {"step_num": 1, "action": "WRITE_FILE", "args": "README.md",
                 "result": "badges updated", "success": 1},
            ],
        },
    ]
    candidates = [{k: r[k] for k in ("id", "goal", "ts")} for r in prior_runs]
    steps_by_id = {r["id"]: r["steps"] for r in prior_runs}

    for goal in [
        "write a unit test for the retry helper in inference/net.py",
        "the poller still times out, finish the fix in core/poller.py",
        "deploy the service to prod",  # unrelated → should produce empty seed
    ]:
        _report_scenario(goal, candidates, lambda rid: steps_by_id.get(rid, []))
    print("=" * 78)
    print("\n(The first two should select the matching prior run and render a seed;")
    print(" the third is unrelated and should produce an empty seed.)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--from-db", action="store_true", help="validate over the live agent.db")
    ap.add_argument("--synthetic", action="store_true", help="reference smoke over crafted runs")
    ap.add_argument("--db", default="agent.db", help="path to agent.db (default: ./agent.db)")
    ap.add_argument("--limit", type=int, default=RECENT_LIMIT, help="recent runs to scan")
    ap.add_argument("--sample", type=int, default=10, help="new-goal scenarios to report")
    args = ap.parse_args()

    if args.synthetic:
        return run_synthetic()
    if args.from_db:
        return run_from_db(args.db, args.limit, args.sample)
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
