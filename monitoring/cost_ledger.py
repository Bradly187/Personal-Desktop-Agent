"""monitoring/cost_ledger.py — roll up cloud (Bedrock) token spend.

Observability batch (2026-06-19). Local inference is free; the only real cost is
the cloud path, now exclusively Amazon Bedrock (Claude). Token usage was already
landing in the `inferences` table for the cloud *command* path, and this batch
added it for the cloud *dev* path (Opus 4.8 — the most expensive calls). This
module reads those rows and reports spend by model, by day, and (where the
inference is linked to a command) by session.

    python -m monitoring.cost_ledger [--db agent.db] [--days 30] [--json]

Costing
-------
Prices are list $/MTok (input/output) for first-party model aliases — the same
aliases stored in `inferences.model`. They are env-overridable because list
prices drift and the Bedrock cross-region inference profile ("us") carries a ~10%
regional premium:

  DA_BEDROCK_PRICES   JSON: {"claude-opus-4-8": [5.0, 25.0], ...}  (overrides table)
  DA_BEDROCK_PRICE_MULT  float multiplier applied to every price (default 1.0;
                          set ~1.10 to model the "us" regional premium)

Models not in the price table (llama*, qwen*, gemma*, deepseek* — all local) are
counted as $0. Costs are estimates for relative spend tracking, not billing.
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Optional

# First-party alias → (input $/MTok, output $/MTok). List prices; override via env.
_DEFAULT_PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-4-8":   (5.0, 25.0),
    "claude-opus-4-7":   (5.0, 25.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5":  (1.0, 5.0),
}


def _load_prices() -> dict[str, tuple[float, float]]:
    prices = dict(_DEFAULT_PRICES)
    raw = os.environ.get("DA_BEDROCK_PRICES")
    if raw:
        try:
            for k, v in json.loads(raw).items():
                if isinstance(v, (list, tuple)) and len(v) == 2:
                    prices[k] = (float(v[0]), float(v[1]))
        except Exception:
            pass
    mult = 1.0
    try:
        mult = float(os.environ.get("DA_BEDROCK_PRICE_MULT", "1.0"))
    except Exception:
        mult = 1.0
    if mult != 1.0:
        prices = {k: (i * mult, o * mult) for k, (i, o) in prices.items()}
    return prices


def _price_for(model: str, prices: dict) -> Optional[tuple[float, float]]:
    """Return (in,out) $/MTok for a model alias, tolerating a Bedrock profile id.

    `inferences.model` normally stores the alias ("claude-opus-4-8"), but be
    lenient if a full profile id ("us.anthropic.claude-opus-4-8") ever lands here.
    """
    if not model:
        return None
    if model in prices:
        return prices[model]
    for alias, p in prices.items():
        if alias in model:
            return p
    return None


def estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    """Estimate USD cost for a single inference. 0.0 for local/unpriced models.

    Public companion to `cost_rollup` for live attribution (e.g. attaching a
    `cost_usd` attr to an inference trace span) without re-querying the DB. Honors
    the same `DA_BEDROCK_PRICES` / `DA_BEDROCK_PRICE_MULT` env overrides. Never
    raises — returns 0.0 on any bad input.
    """
    try:
        price = _price_for(model or "", _load_prices())
        if price is None:
            return 0.0
        ti = int(tokens_in or 0)
        to = int(tokens_out or 0)
        return round(ti / 1e6 * price[0] + to / 1e6 * price[1], 6)
    except Exception:
        return 0.0


def _connect_ro(path: str) -> Optional[sqlite3.Connection]:
    if not path or not os.path.exists(path):
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception:
        return None


def cost_rollup(db_path: str = "agent.db", days: Optional[int] = 30) -> dict:
    """Roll up cloud token spend from `inferences`. Never raises.

    Returns {by_model, by_day, by_session, totals, days, n_cloud_inferences}.
    `days=None` means all-time.
    """
    prices = _load_prices()
    conn = _connect_ro(db_path)
    rows: list[dict] = []
    try:
        if conn is not None:
            import time as _time
            where, params = "", ()
            if days is not None:
                where = "WHERE i.ts >= ?"
                params = (_time.time() - days * 86400,)
            try:
                cur = conn.execute(
                    f"SELECT i.ts AS ts, i.model AS model, i.backend AS backend, "
                    f"i.tokens_in AS tokens_in, i.tokens_out AS tokens_out, "
                    f"c.session_id AS session_id "
                    f"FROM inferences i LEFT JOIN commands c ON i.command_id = c.id "
                    f"{where} ORDER BY i.ts",
                    params,
                )
                rows = [dict(r) for r in cur.fetchall()]
            except Exception:
                rows = []
    finally:
        if conn is not None:
            conn.close()

    import datetime
    by_model: dict = {}
    by_day: dict = {}
    by_session: dict = {}
    tot_in = tot_out = 0
    tot_cost = 0.0
    n_cloud = 0

    for r in rows:
        price = _price_for(r.get("model") or "", prices)
        if price is None:
            continue  # local / unpriced → $0, skip from the cloud ledger
        n_cloud += 1
        ti = r.get("tokens_in") or 0
        to = r.get("tokens_out") or 0
        cost = ti / 1e6 * price[0] + to / 1e6 * price[1]
        tot_in += ti
        tot_out += to
        tot_cost += cost

        m = by_model.setdefault(r["model"], {"calls": 0, "tokens_in": 0, "tokens_out": 0, "cost": 0.0})
        m["calls"] += 1
        m["tokens_in"] += ti
        m["tokens_out"] += to
        m["cost"] += cost

        day = datetime.datetime.fromtimestamp(r.get("ts") or 0).strftime("%Y-%m-%d")
        d = by_day.setdefault(day, {"calls": 0, "cost": 0.0})
        d["calls"] += 1
        d["cost"] += cost

        sid = r.get("session_id")
        if sid is not None:
            sset = by_session.setdefault(sid, {"calls": 0, "cost": 0.0})
            sset["calls"] += 1
            sset["cost"] += cost

    return {
        "days": days,
        "n_cloud_inferences": n_cloud,
        "by_model": by_model,
        "by_day": dict(sorted(by_day.items())),
        "by_session": by_session,
        "totals": {
            "tokens_in": tot_in,
            "tokens_out": tot_out,
            "cost": round(tot_cost, 4),
        },
        "prices_used": {k: list(v) for k, v in prices.items()},
    }


def format_rollup(result: dict) -> str:
    days = result.get("days")
    span = f"last {days} days" if days else "all time"
    t = result["totals"]
    lines = [
        f"Cloud cost ledger — {span}",
        (f"  cloud inferences: {result['n_cloud_inferences']}   "
         f"tokens: {t['tokens_in']:,} in / {t['tokens_out']:,} out   "
         f"est. cost: ${t['cost']:.4f}"),
    ]
    if not result["by_model"]:
        lines.append("\n  (no priced cloud inferences found — has the cloud path been used?)")
        return "\n".join(lines)

    lines += ["", "  BY MODEL", "  " + "-" * 64,
              f"  {'MODEL':<22}  {'CALLS':>5}  {'TOK IN':>9}  {'TOK OUT':>9}  {'COST':>9}"]
    for model, m in sorted(result["by_model"].items(), key=lambda x: -x[1]["cost"]):
        lines.append(f"  {model:<22}  {m['calls']:>5}  {m['tokens_in']:>9,}  "
                     f"{m['tokens_out']:>9,}  ${m['cost']:>8.4f}")

    by_day = result["by_day"]
    if by_day:
        lines += ["", "  BY DAY", "  " + "-" * 40, f"  {'DATE':<12}  {'CALLS':>5}  {'COST':>9}"]
        for day, d in by_day.items():
            lines.append(f"  {day:<12}  {d['calls']:>5}  ${d['cost']:>8.4f}")

    by_session = result["by_session"]
    if by_session:
        top = sorted(by_session.items(), key=lambda x: -x[1]["cost"])[:10]
        lines += ["", "  TOP SESSIONS BY COST", "  " + "-" * 40,
                  f"  {'SESSION':>7}  {'CALLS':>5}  {'COST':>9}"]
        for sid, sset in top:
            lines.append(f"  {sid:>7}  {sset['calls']:>5}  ${sset['cost']:>8.4f}")
    return "\n".join(lines)


def main() -> None:
    import argparse
    p = argparse.ArgumentParser(description="Roll up cloud (Bedrock) token spend.")
    p.add_argument("--db", default="agent.db")
    p.add_argument("--days", type=int, default=30, help="lookback window (0 = all time)")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    days = None if args.days == 0 else args.days
    result = cost_rollup(args.db, days)
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(format_rollup(result))


if __name__ == "__main__":
    main()
