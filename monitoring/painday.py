"""monitoring/painday.py — pain-day impact rollup.

The dashboard shows a single pain-day score, but not what it reflects or how
much time the user spends in a flare. This reads twin_pain_day_log (the
PainDayEngine's signal log) and reports the current score + its four driver
signals, a downsampled trend for a sparkline, and how much of the window was
spent in an active flare.

This is the accessibility core of the project: pain-day state drives adaptive
thresholds via BehavioralTwinState.apply_pain_day(), so seeing the score AND its
drivers (failures, clarifies, gesture-confidence drop, command-rate drop) tells
the user why the agent is adapting. Read-only, never raises.
"""
from __future__ import annotations

import os
import sqlite3
import time
from typing import Optional

# Score at/above which the agent treats the day as an active flare (matches the
# Now-panel KPI warn threshold). The authoritative flag is pain_day_active in the
# log; this constant is only a display reference line for the sparkline.
FLARE_THRESHOLD = 0.6


def _connect_ro(path: str) -> Optional[sqlite3.Connection]:
    if not path or not os.path.exists(path):
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception:
        return None


def painday_breakdown(db_path: str = "agent.db", days: Optional[int] = 7,
                      points: int = 48) -> dict:
    """Current pain-day score + drivers, % time in flare, and a sparkline trend.

    `days=None` = all time. `points` caps the downsampled sparkline length.
    Returns {days, n_samples, current:{...}, pct_active, peak_score, avg_score,
    flare_threshold, spark:[...]}.
    """
    conn = _connect_ro(db_path)
    current: Optional[dict] = None
    series: list = []  # (score, active)
    try:
        if conn is not None:
            try:
                row = conn.execute(
                    "SELECT ts, pain_day_score, pain_day_active, fail_ratio, "
                    "clarify_ratio, gesture_conf_delta, cmd_rate_delta "
                    "FROM twin_pain_day_log ORDER BY ts DESC LIMIT 1"
                ).fetchone()
                if row is not None:
                    current = dict(row)
            except Exception:
                pass
            try:
                where, params = "", ()
                if days is not None:
                    where = "WHERE ts >= ?"
                    params = (time.time() - days * 86400,)
                for r in conn.execute(
                    f"SELECT pain_day_score AS s, pain_day_active AS a "
                    f"FROM twin_pain_day_log {where} ORDER BY ts", params,
                ):
                    series.append((r["s"], r["a"]))
            except Exception:
                pass
    finally:
        if conn is not None:
            conn.close()

    n = len(series)
    pct_active = round(sum(1 for _, a in series if a) / n, 4) if n else None
    peak = round(max((s for s, _ in series), default=0.0), 4) if n else None
    avg = round(sum(s for s, _ in series) / n, 4) if n else None

    # Downsample the score series to <= `points` buckets (mean per bucket) so the
    # sparkline stays small regardless of how many 60s samples the window holds.
    spark: list = []
    if n:
        bsize = max(1, n // points)
        for i in range(0, n, bsize):
            chunk = [s for s, _ in series[i:i + bsize]]
            if chunk:
                spark.append(round(sum(chunk) / len(chunk), 4))

    return {
        "days": days,
        "n_samples": n,
        "current": current,
        "pct_active": pct_active,
        "peak_score": peak,
        "avg_score": avg,
        "flare_threshold": FLARE_THRESHOLD,
        "spark": spark,
    }
