"""monitoring/latency.py — latency split by route and stage.

The dashboard's blended p50/p95 hides the real story: inference dominates, and
a cold local model is far slower than a cloud call. This unblends command
latency into INFERENCE vs EXECUTE stages, split by LOCAL vs CLOUD route, so the
cold-load tax on the local path is visible.

  * inference_ms = SUM(inferences.latency_ms) linked to the command
  * execute_ms   = commands.latency_ms - inference_ms   (clamped >= 0)
  * total_ms     = commands.latency_ms

Only commands with a linked inference are counted (bypass commands never hit an
LLM, so they have no stage split). Read-only, never raises.
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


def _pct(values: list, q: float) -> Optional[float]:
    """Nearest-rank q-percentile (q in [0,1]) of non-null values. None if empty."""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    k = max(0, min(len(vals) - 1, int(round(q * (len(vals) - 1)))))
    return round(vals[k], 1)


def _stage(values: list) -> dict:
    return {"p50": _pct(values, 0.50), "p95": _pct(values, 0.95)}


def latency_breakdown(db_path: str = "agent.db", days: Optional[int] = 30) -> dict:
    """Per-route inference/execute/total latency percentiles. `days=None` = all time.

    Returns {days, routes: {<route>: {n, infer:{p50,p95}, execute:{p50,p95},
    total:{p50,p95}}}}.
    """
    conn = _connect_ro(db_path)
    buckets: dict = {}  # route -> {"infer": [], "execute": [], "total": []}
    try:
        if conn is not None:
            conds = ["c.latency_ms IS NOT NULL", "i.latency_ms IS NOT NULL"]
            params: tuple = ()
            if days is not None:
                conds.append("c.ts >= ?")
                params = (time.time() - days * 86400,)
            where = "WHERE " + " AND ".join(conds)
            try:
                cur = conn.execute(
                    f"SELECT c.route AS route, c.latency_ms AS total, "
                    f"SUM(i.latency_ms) AS infer "
                    f"FROM commands c JOIN inferences i ON i.command_id = c.id "
                    f"{where} GROUP BY c.id",
                    params,
                )
                for r in cur:
                    route = r["route"] or "(none)"
                    total = r["total"]
                    infer = r["infer"]
                    if total is None or infer is None:
                        continue
                    execute = max(0.0, total - infer)
                    b = buckets.setdefault(route, {"infer": [], "execute": [], "total": []})
                    b["infer"].append(infer)
                    b["execute"].append(execute)
                    b["total"].append(total)
            except Exception:
                pass
    finally:
        if conn is not None:
            conn.close()

    routes: dict = {}
    for route, b in buckets.items():
        routes[route] = {
            "n": len(b["total"]),
            "infer": _stage(b["infer"]),
            "execute": _stage(b["execute"]),
            "total": _stage(b["total"]),
        }
    return {"days": days, "routes": routes}
