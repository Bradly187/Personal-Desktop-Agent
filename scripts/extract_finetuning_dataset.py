"""Extract a fine-tuning dataset from agent.db.

Joins inferences + commands, filters noise, deduplicates, and exports in
Alpaca-format JSONL ready for Axolotl or HuggingFace TRL.

Usage:
    python scripts/extract_finetuning_dataset.py [--db path/to/agent.db] [--out dataset.jsonl]
    python scripts/extract_finetuning_dataset.py --stats          # show DB state only

Output schema (one JSON object per line):
    {
        "instruction": <system prompt>,
        "input":       <user utterance + optional session context>,
        "output":      <action string, e.g. "CLICK save button">,
        "metadata": {
            "source":             "voice" | "gesture" | "touch" | ...,
            "whisper_logprob":    float | null,
            "gesture_confidence": float | null,
            "success":            true | false,
            "model":              "llama3.1:8b" | "claude-haiku-4-5" | ...,
            "domain":             "command" | "code" | ...,
            "latency_ms":         float,
            "synthetic":          false
        }
    }

Synthetic examples (generate_synthetic_data.py) use the same schema with
"synthetic": true so they can be filtered out at training time.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

_DEFAULT_DB = Path(__file__).parent.parent / "agent.db"
_DEFAULT_OUT = Path(__file__).parent.parent / "finetuning_dataset.jsonl"

# The system prompt injected into every training example so the fine-tuned
# model sees the same instruction format it will receive at inference time.
_SYSTEM_PROMPT = (
    "You are a desktop control assistant. Convert the user's natural-language "
    "request into exactly ONE action from the following vocabulary.\n\n"
    "CLICK <target>  SCROLL <direction> [<amount>]  TYPE <text>  OPEN <app-or-file>\n"
    "CLOSE [<target>]  HOTKEY <key1> [<key2>...]  DICTATE <text>\n"
    "CLARIFY <question>  SCREENSHOT\n\n"
    "Reply with ONLY the action string. Do not explain or comment."
)

# Quality filters
_MIN_WHISPER_LOGPROB = -0.9   # drop very low-confidence voice transcripts
_CLARIFY_ERROR_PREFIX = "CLARIFY inference error"   # drop backend-offline noise


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db",    default=str(_DEFAULT_DB),  help="Path to agent.db")
    p.add_argument("--out",   default=str(_DEFAULT_OUT), help="Output JSONL path")
    p.add_argument("--stats", action="store_true",        help="Print DB stats and exit")
    p.add_argument("--include-synthetic", action="store_true",
                   help="Also merge finetuning_synthetic.jsonl if it exists")
    p.add_argument("--min-success-rate", type=float, default=0.0,
                   help="Skip examples with success=0 unless corrected_to is set (0=keep all)")
    return p.parse_args()


def _load_rows(conn: sqlite3.Connection) -> list[dict]:
    cur = conn.execute("""
        SELECT
            i.id            AS inference_id,
            i.prompt        AS prompt,
            i.response      AS response,
            i.model         AS model,
            i.domain        AS domain,
            i.tokens_in     AS tokens_in,
            i.tokens_out    AS tokens_out,
            i.latency_ms    AS latency_ms,
            i.backend       AS backend,
            c.text          AS cmd_text,
            c.source        AS source,
            c.whisper_logprob    AS whisper_logprob,
            c.gesture_confidence AS gesture_confidence,
            c.success            AS success,
            c.corrected_to       AS corrected_to,
            c.session_context    AS session_context
        FROM inferences i
        JOIN commands c ON i.command_id = c.id
        WHERE i.prompt IS NOT NULL
          AND i.response IS NOT NULL
        ORDER BY i.ts
    """)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _filter(rows: list[dict]) -> list[dict]:
    kept = []
    for r in rows:
        # Drop backend-offline errors — these are dev noise, not real interactions
        if r["response"] and r["response"].startswith(_CLARIFY_ERROR_PREFIX):
            continue
        # Drop low-confidence voice transcripts unless there's a correction
        logprob = r.get("whisper_logprob")
        if logprob is not None and logprob < _MIN_WHISPER_LOGPROB:
            if not r.get("corrected_to"):
                continue
        # Drop failed commands with no correction signal
        if r.get("success") == 0 and not r.get("corrected_to"):
            continue
        kept.append(r)
    return kept


def _deduplicate(rows: list[dict]) -> list[dict]:
    """Simple exact-text dedup: keep the first occurrence of each (cmd_text, response) pair."""
    seen: set[tuple[str, str]] = set()
    deduped = []
    for r in rows:
        key = (r["cmd_text"].strip().lower(), (r["response"] or "").strip().upper())
        if key not in seen:
            seen.add(key)
            deduped.append(r)
    return deduped


def _to_alpaca(r: dict) -> dict:
    # Build the user-facing input: the utterance plus any session context
    # embedded in the stored prompt JSON (if available).
    cmd_text = r["cmd_text"] or ""
    ctx_lines = ""
    if r.get("prompt"):
        try:
            messages = json.loads(r["prompt"])
            for m in messages:
                if m.get("role") == "user" and "Recent commands:" in m.get("content", ""):
                    ctx_lines = "\n" + m["content"]
                    break
        except Exception:
            pass

    # Use the corrected action as the training target when available
    output = r.get("corrected_to") or r["response"] or ""

    return {
        "instruction": _SYSTEM_PROMPT,
        "input": cmd_text + ctx_lines,
        "output": output,
        "metadata": {
            "source":             r.get("source"),
            "whisper_logprob":    r.get("whisper_logprob"),
            "gesture_confidence": r.get("gesture_confidence"),
            "success":            bool(r.get("success")),
            "model":              r.get("model"),
            "domain":             r.get("domain"),
            "latency_ms":         r.get("latency_ms"),
            "tokens_in":          r.get("tokens_in"),
            "tokens_out":         r.get("tokens_out"),
            "synthetic":          False,
        },
    }


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in rows)


def _print_stats(conn: sqlite3.Connection) -> None:
    print("\n=== agent.db fine-tuning readiness ===")
    total_inf = conn.execute("SELECT COUNT(*) FROM inferences").fetchone()[0]
    if not _has_column(conn, "inferences", "prompt"):
        print(f"  inferences total:          {total_inf}")
        print("  inferences with prompt:    0  (prompt column not yet created)")
        print("  NOTE: Start main.py once to apply the schema migration (v3 -> v4),")
        print("        then re-run this script.")
        print()
        return
    with_prompt = conn.execute("SELECT COUNT(*) FROM inferences WHERE prompt IS NOT NULL").fetchone()[0]
    with_tokens = conn.execute(
        "SELECT COUNT(*) FROM inferences WHERE tokens_in IS NOT NULL"
    ).fetchone()[0]
    print(f"  inferences total:          {total_inf}")
    print(f"  inferences with prompt:    {with_prompt}  ({with_prompt/max(total_inf,1)*100:.0f}%)")
    print(f"  inferences with tokens:    {with_tokens}  ({with_tokens/max(total_inf,1)*100:.0f}%)")

    by_domain = conn.execute(
        "SELECT domain, COUNT(*), AVG(tokens_in) FROM inferences WHERE prompt IS NOT NULL GROUP BY domain"
    ).fetchall()
    if by_domain:
        print("\n  domain distribution (prompt-captured):")
        for domain, cnt, avg_tok in by_domain:
            tok_str = f"{avg_tok:.0f} avg tokens_in" if avg_tok else "tokens unknown"
            print(f"    {domain:<12}  {cnt:>4} rows   {tok_str}")

    linkable = conn.execute(
        """SELECT COUNT(*) FROM inferences i
           JOIN commands c ON i.command_id = c.id
           WHERE i.prompt IS NOT NULL"""
    ).fetchone()[0]
    print(f"\n  linked inference+command:  {linkable}  (usable for fine-tuning)")
    print()


def main() -> None:
    args = _parse_args()
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: DB not found at {db_path}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    if args.stats:
        _print_stats(conn)
        conn.close()
        return

    _print_stats(conn)

    if not _has_column(conn, "inferences", "prompt"):
        conn.close()
        sys.exit(0)

    rows = _load_rows(conn)
    conn.close()

    print(f"Loaded {len(rows)} linked inference+command rows")
    rows = _filter(rows)
    print(f"After quality filter: {len(rows)}")
    rows = _deduplicate(rows)
    print(f"After deduplication:  {len(rows)}")

    examples = [_to_alpaca(r) for r in rows]

    # Optionally merge synthetic examples
    if args.include_synthetic:
        synthetic_path = Path(args.out).parent / "finetuning_synthetic.jsonl"
        if synthetic_path.exists():
            with open(synthetic_path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        examples.append(json.loads(line))
            print(f"Merged synthetic examples → total: {len(examples)}")

    out_path = Path(args.out)
    with open(out_path, "w", encoding="utf-8") as f:
        for ex in examples:
            f.write(json.dumps(ex, ensure_ascii=False) + "\n")

    # Summary stats
    sources: dict[str, int] = {}
    domains: dict[str, int] = {}
    success_count = 0
    for ex in examples:
        m = ex.get("metadata", {})
        src = m.get("source") or "unknown"
        dom = m.get("domain") or "unknown"
        sources[src] = sources.get(src, 0) + 1
        domains[dom] = domains.get(dom, 0) + 1
        if m.get("success"):
            success_count += 1

    print(f"\nWrote {len(examples)} examples → {out_path}")
    print(f"  Success rate: {success_count}/{len(examples)} = {success_count/max(len(examples),1)*100:.0f}%")
    print(f"  Sources:  {dict(sorted(sources.items(), key=lambda kv: -kv[1]))}")
    print(f"  Domains:  {dict(sorted(domains.items(), key=lambda kv: -kv[1]))}")

    if len(examples) < 200:
        print(f"\n  NOTE: {len(examples)} examples is below the 200-example minimum for")
        print("  task-specific LoRA. Keep collecting — run --stats to monitor progress.")


if __name__ == "__main__":
    main()
