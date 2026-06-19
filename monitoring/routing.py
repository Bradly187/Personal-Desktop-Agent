"""monitoring/routing.py — gate-decision, route, and error breakdown.

Powers the dashboard "Routing" and "Errors" cards. Reads:
  * commands.gate_that_decided / route / success  → which gate decided each
    command (hence why it ran local vs cloud) and the local/cloud split.
  * inferences.error                              → the most common inference
    failures, by model.

Read-only, never raises — returns empty aggregates if the DB is unreadable.
"""
from __future__ import annotations

import os
import sqlite3
import time
from typing import Optional

# Cloud backends (everything else is local). Mirrors cost_ledger._CLOUD_BACKENDS.
_CLOUD_BACKENDS = {"anthropic", "bedrock"}

# Gate label → human one-liner (why the decision was made). Kept in sync with
# HybridCoordinator's gate chain (_gate0.._gate4 + bypass + all_pass).
_GATE_DESC = {
    "all_pass":         "all gates passed → local",
    "bypass":           "iPad/voice bypass → no LLM",
    "gate0_privacy":    "sensitive/personal → forced local",
    "gate1_voice_conf": "low voice confidence → cloud",
    "gate2_complexity": "too complex/long → cloud",
    "gate3_vram":       "low VRAM → cloud",
    "gate4_latency":    "local latency over budget → cloud",
}


def _connect_ro(path: str) -> Optional[sqlite3.Connection]:
    if not path or not os.path.exists(path):
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception:
        return None


def _short_error(msg: str) -> str:
    """Trim an inference error to a readable one-liner (drop the CLARIFY sentinel)."""
    msg = (msg or "").strip()
    if msg.upper().startswith("CLARIFY "):
        msg = msg[len("CLARIFY "):]
    return msg[:90]


def routing_breakdown(db_path: str = "agent.db", days: Optional[int] = 30) -> dict:
    """Gate-decision distribution, local/cloud route split, success rate, and the
    top inference errors. `days=None` = all time.

    Returns {days, n_commands, gates:[...], routes:{...}, success:{...}, errors:[...]}.
    """
    conn = _connect_ro(db_path)
    gates: dict = {}
    routes: dict = {}
    n_ok = n_fail = 0
    n_commands = 0
    errors: list = []
    try:
        if conn is not None:
            cwhere, cparams = "", ()
            iwhere, iparams = "WHERE error IS NOT NULL AND error != ''", ()
            if days is not None:
                cutoff = time.time() - days * 86400
                cwhere = "WHERE ts >= ?"
                cparams = (cutoff,)
                iwhere += " AND ts >= ?"
                iparams = (cutoff,)

            try:
                for r in conn.execute(
                    f"SELECT gate_that_decided AS g, route AS rt, success AS s, "
                    f"COUNT(*) AS n FROM commands {cwhere} GROUP BY g, rt, s",
                    cparams,
                ):
                    g = r["g"] or "(none)"
                    rt = r["rt"] or "(none)"
                    n = r["n"]
                    n_commands += n
                    ge = gates.setdefault(g, {
                        "gate": g, "count": 0, "route": rt,
                        "desc": _GATE_DESC.get(g, ""),
                    })
                    ge["count"] += n
                    # Associate the gate with its most common route.
                    if n > ge.get("_route_n", 0):
                        ge["route"] = rt
                        ge["_route_n"] = n
                    routes[rt] = routes.get(rt, 0) + n
                    if r["s"] == 1:
                        n_ok += n
                    elif r["s"] == 0:
                        n_fail += n
            except Exception:
                pass

            try:
                for r in conn.execute(
                    f"SELECT model AS m, backend AS b, error AS e, COUNT(*) AS n "
                    f"FROM inferences {iwhere} GROUP BY m, e ORDER BY n DESC LIMIT 12",
                    iparams,
                ):
                    errors.append({
                        "model": r["m"] or "(unknown)",
                        "local": (r["b"] or "") not in _CLOUD_BACKENDS,
                        "error": _short_error(r["e"]),
                        "count": r["n"],
                    })
            except Exception:
                pass
    finally:
        if conn is not None:
            conn.close()

    for ge in gates.values():
        ge.pop("_route_n", None)
    gate_list = sorted(gates.values(), key=lambda x: -x["count"])
    total_sc = n_ok + n_fail
    return {
        "days": days,
        "n_commands": n_commands,
        "gates": gate_list,
        "routes": routes,
        "success": {
            "ok": n_ok, "fail": n_fail,
            "rate": round(n_ok / total_sc, 4) if total_sc else None,
        },
        "errors": errors,
    }
