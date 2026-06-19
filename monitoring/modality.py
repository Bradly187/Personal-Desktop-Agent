"""monitoring/modality.py — input-modality usage breakdown.

For a single accessibility user, *which* input modality is actually used — and
how well it works — is real signal: is voice degrading, is touch carrying the
load, is a modality being abandoned? Aggregates the commands table by
`source` (gaze_dwell / touch / voice / voice_local / chat / sound_action /
multimodal …): count, success rate, avg latency, and cloud-escalation rate.

Read-only, never raises.
"""
from __future__ import annotations

import os
import sqlite3
import time
from typing import Optional


def _connect_ro(path: str) -> Optional[sqlite3.Connection]:
    if not path or not os.path.exists(path):
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception:
        return None


def modality_breakdown(db_path: str = "agent.db", days: Optional[int] = 30) -> dict:
    """Per-source usage: calls, success rate, avg latency, cloud rate, last-used.

    `days=None` = all time. Returns {days, n_commands, sources:[...]} sorted by
    call count descending.
    """
    conn = _connect_ro(db_path)
    sources: list = []
    n_total = 0
    try:
        if conn is not None:
            where, params = "", ()
            if days is not None:
                where = "WHERE ts >= ?"
                params = (time.time() - days * 86400,)
            try:
                cur = conn.execute(
                    f"SELECT COALESCE(source, '(unknown)') AS source, "
                    f"COUNT(*) AS n, "
                    f"AVG(success) AS success_rate, "
                    f"AVG(latency_ms) AS avg_latency, "
                    f"AVG(CASE WHEN route = 'cloud' THEN 1.0 ELSE 0.0 END) AS cloud_rate, "
                    f"MAX(ts) AS last_ts "
                    f"FROM commands {where} GROUP BY source ORDER BY n DESC",
                    params,
                )
                for r in cur:
                    n = r["n"] or 0
                    n_total += n
                    sources.append({
                        "source": r["source"],
                        "n": n,
                        "success_rate": round(r["success_rate"], 4) if r["success_rate"] is not None else None,
                        "avg_latency_ms": round(r["avg_latency"], 1) if r["avg_latency"] is not None else None,
                        "cloud_rate": round(r["cloud_rate"], 4) if r["cloud_rate"] is not None else None,
                        "last_ts": r["last_ts"],
                    })
            except Exception:
                sources = []
    finally:
        if conn is not None:
            conn.close()

    return {"days": days, "n_commands": n_total, "sources": sources}
