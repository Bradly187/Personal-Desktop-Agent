"""monitoring/trends.py — cross-session trend report over session_summaries.

Observability batch (2026-06-19). `SessionAnalyzer` writes one KPI rollup row per
session to `session_summaries`, but until now nothing read across sessions — so
slow drift (cloud-rate creeping up, p95 regressing after a change, success-rate
sliding on flare days) was invisible. This module reads the rollups and reports
the trend.

    python -m monitoring.trends [--db agent.db] [--limit 30] [--json]

Read-only and dependency-free: opens agent.db with `mode=ro` via stdlib sqlite3
(the percentiles are already precomputed in session_summaries, so no DuckDB is
needed on this path). For each metric it compares the recent half of the window
against the older half and flags improving / worsening / flat, respecting each
metric's polarity (e.g. lower cloud-rate is better, higher success-rate is
better).
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Optional

# Metric → (column, "higher_better"|"lower_better", human label, formatter)
_METRICS = [
    ("success_rate",          "higher", "success rate",   "pct"),
    ("cloud_escalation_rate", "lower",  "cloud rate",     "pct"),
    ("latency_p50_ms",        "lower",  "latency p50",    "ms"),
    ("latency_p95_ms",        "lower",  "latency p95",    "ms"),
    ("pain_day_pct",          "lower",  "pain-day frac",  "pct"),
    ("corrections_count",     "lower",  "corrections",    "num"),
]


def _connect_ro(path: str) -> Optional[sqlite3.Connection]:
    if not path or not os.path.exists(path):
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception:
        return None


def session_trends(db_path: str = "agent.db", limit: int = 30) -> dict:
    """Return recent session rollups + per-metric recent-vs-older deltas.

    Never raises — a missing db/table yields an empty `sessions` list.
    """
    conn = _connect_ro(db_path)
    sessions: list[dict] = []
    try:
        if conn is not None:
            try:
                cur = conn.execute(
                    "SELECT session_id, ts, duration_s, total_commands, success_rate, "
                    "cloud_escalation_rate, latency_p50_ms, latency_p95_ms, pain_day_pct, "
                    "corrections_count, gate2_blocks, gate3_blocks, gate4_blocks "
                    "FROM session_summaries WHERE total_commands > 0 ORDER BY ts DESC LIMIT ?",
                    (int(limit),),
                )
                sessions = [dict(r) for r in cur.fetchall()]
            except Exception:
                sessions = []
    finally:
        if conn is not None:
            conn.close()

    # Oldest→newest for display and windowing.
    sessions.reverse()
    deltas = _compute_deltas(sessions)
    return {
        "sessions": sessions,
        "deltas": deltas,
        "n_sessions": len(sessions),
    }


def _compute_deltas(sessions: list[dict]) -> dict:
    """Compare the recent half of the window vs the older half, per metric."""
    deltas: dict = {}
    n = len(sessions)
    if n < 2:
        return deltas
    mid = n // 2
    older, recent = sessions[:mid], sessions[mid:]

    def _mean(rows, col):
        vals = [r[col] for r in rows if r.get(col) is not None]
        return (sum(vals) / len(vals)) if vals else None

    for col, polarity, label, fmt in _METRICS:
        o, r = _mean(older, col), _mean(recent, col)
        if o is None or r is None:
            continue
        change = r - o
        # "verdict" respects polarity; flat if change is negligible vs the older mean.
        ref = abs(o) if abs(o) > 1e-9 else 1.0
        rel = change / ref
        if abs(rel) < 0.05:
            verdict = "flat"
        elif (polarity == "higher" and change > 0) or (polarity == "lower" and change < 0):
            verdict = "improving"
        else:
            verdict = "worsening"
        deltas[col] = {
            "label": label, "fmt": fmt, "polarity": polarity,
            "older": o, "recent": r, "change": change, "verdict": verdict,
        }
    return deltas


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def format_trends(result: dict) -> str:
    import datetime
    sessions = result["sessions"]
    if not sessions:
        return ("No session_summaries rows found.\n"
                "(Summaries are written at session close by SessionAnalyzer.)")

    lines = [
        f"Cross-session trends — {result['n_sessions']} session(s)",
        "",
        f"  {'DATE':<16}  {'SID':>4}  {'CMDS':>5}  {'OK%':>5}  {'CLOUD%':>6}  "
        f"{'P50':>6}  {'P95':>7}  {'PAIN%':>5}  {'CORR':>4}",
        "  " + "-" * 78,
    ]
    for s in sessions:
        date = datetime.datetime.fromtimestamp(s.get("ts") or 0).strftime("%Y-%m-%d %H:%M")
        lines.append(
            f"  {date:<16}  {_i(s.get('session_id')):>4}  {_i(s.get('total_commands')):>5}  "
            f"{_pct(s.get('success_rate')):>5}  {_pct(s.get('cloud_escalation_rate')):>6}  "
            f"{_ms(s.get('latency_p50_ms')):>6}  {_ms(s.get('latency_p95_ms')):>7}  "
            f"{_pct(s.get('pain_day_pct')):>5}  {_i(s.get('corrections_count')):>4}"
        )

    deltas = result["deltas"]
    if deltas:
        lines += ["", "  TREND (recent half vs older half)", "  " + "-" * 50]
        arrow = {"improving": "▲ improving", "worsening": "▼ worsening", "flat": "= flat"}
        for col, _pol, label, fmt in _METRICS:
            d = deltas.get(col)
            if not d:
                continue
            f = {"pct": _pct, "ms": _ms, "num": _i}[d["fmt"]]
            lines.append(
                f"  {label:<14} {f(d['older']):>8} → {f(d['recent']):>8}   "
                f"{arrow.get(d['verdict'], d['verdict'])}"
            )
    return "\n".join(lines)


def _pct(v) -> str:
    return f"{v * 100:.0f}%" if v is not None else "—"

def _ms(v) -> str:
    return f"{v:.0f}" if v is not None else "—"

def _i(v) -> str:
    return str(v) if v is not None else "—"


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main() -> None:
    import argparse
    p = argparse.ArgumentParser(description="Cross-session trend report over session_summaries.")
    p.add_argument("--db", default="agent.db")
    p.add_argument("--limit", type=int, default=30, help="how many recent sessions to include")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    result = session_trends(args.db, args.limit)
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(format_trends(result))


if __name__ == "__main__":
    main()
