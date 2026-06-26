"""monitoring/replay.py — reconstruct one command's full story from a trace_id.

Observability batch (2026-06-19). The agent records what it does across FOUR
stores, all correlatable by `trace_id` (and, for the audit trail, by the
`command_id` a trace resolves to):

    commands         (agent.db)  — the routed command + outcome
    inferences       (agent.db)  — model calls made for that command (tokens, latency)
    command_traces   (agent.db)  — per-stage span timeline (enqueue→…→execute)
    event_log        (agent.db)  — EventBus topics published during the command
    audit_events     (audit.db)  — security-relevant tool calls / approvals

Answering "what did the agent do, and why?" used to mean hand-writing four SQL
JOINs across two database files. `replay_trace(trace_id)` does it in one call and
returns a single time-ordered timeline. `format_timeline()` renders it for the
CLI:

    python -m monitoring.replay <trace_id> [--agent-db agent.db] [--audit-db audit.db]
    python -m monitoring.replay --recent 10        # list recent trace_ids
    python -m monitoring.replay --recent 10 --json  # machine-readable

Read-only and dependency-free: opens each SQLite file with `mode=ro`, never
writes, and degrades gracefully (missing file/table → empty section) so it is
safe to run against a live agent's databases.
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Optional


# --------------------------------------------------------------------------- #
# Read-only SQLite helpers
# --------------------------------------------------------------------------- #

def _connect_ro(path: str) -> Optional[sqlite3.Connection]:
    """Open `path` read-only. Returns None if the file is absent/unopenable."""
    if not path or not os.path.exists(path):
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception:
        return None


def _rows(conn: Optional[sqlite3.Connection], sql: str, params: tuple = ()) -> list[dict]:
    """Run a query, returning list-of-dicts. Empty on any error (missing table, etc.)."""
    if conn is None:
        return []
    try:
        cur = conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]
    except Exception:
        return []


# --------------------------------------------------------------------------- #
# Assemble
# --------------------------------------------------------------------------- #

def replay_trace(
    trace_id: str,
    agent_db_path: str = "agent.db",
    audit_db_path: str = "audit.db",
) -> dict:
    """Assemble everything recorded for `trace_id` into one ordered timeline.

    Returns a dict with the resolved command(s), the raw sections (spans, events,
    inferences, audit), a merged `timeline` sorted by timestamp, and a `summary`.
    Never raises — unreadable stores simply contribute empty sections.
    """
    result: dict = {
        "trace_id": trace_id,
        "command": None,
        "commands": [],
        "inferences": [],
        "spans": [],
        "events": [],
        "audit": [],
        "timeline": [],
        "summary": {},
    }

    adb = _connect_ro(agent_db_path)
    try:
        commands = _rows(
            adb,
            "SELECT id, session_id, ts, source, text, action, route, "
            "gate_that_decided, latency_ms, success, error_msg, corrected_to "
            "FROM commands WHERE trace_id = ? ORDER BY ts",
            (trace_id,),
        )
        result["commands"] = commands
        result["command"] = commands[0] if commands else None
        command_ids = [c["id"] for c in commands if c.get("id") is not None]

        # Inferences linked to those command(s) (token + latency per model call).
        inferences: list[dict] = []
        if command_ids:
            placeholders = ",".join("?" * len(command_ids))
            inferences = _rows(
                adb,
                f"SELECT id, command_id, ts, model, domain, tokens_in, tokens_out, "
                f"latency_ms, backend, error FROM inferences "
                f"WHERE command_id IN ({placeholders}) ORDER BY ts",
                tuple(command_ids),
            )
        result["inferences"] = inferences

        result["spans"] = _rows(
            adb,
            "SELECT seq, stage, ts, dur_ms, attrs_json FROM command_traces "
            "WHERE trace_id = ? ORDER BY seq",
            (trace_id,),
        )
        result["events"] = _rows(
            adb,
            "SELECT id, ts, topic, source, command_id, payload FROM event_log "
            "WHERE trace_id = ? ORDER BY ts",
            (trace_id,),
        )
    finally:
        if adb is not None:
            adb.close()

    # Audit events have no trace_id column — link via the command id(s) the trace
    # resolved to (the security trail is keyed to commands, not traces).
    command_ids = [c["id"] for c in result["commands"] if c.get("id") is not None]
    if command_ids:
        audb = _connect_ro(audit_db_path)
        try:
            placeholders = ",".join("?" * len(command_ids))
            result["audit"] = _rows(
                audb,
                f"SELECT id, ts, event_type, severity, actor, tool, detail, "
                f"outcome, command_id FROM audit_events "
                f"WHERE command_id IN ({placeholders}) ORDER BY ts",
                tuple(command_ids),
            )
        finally:
            if audb is not None:
                audb.close()

    result["timeline"] = _build_timeline(result)
    result["summary"] = _build_summary(result)
    return result


def _build_timeline(result: dict) -> list[dict]:
    """Merge spans + events + inferences + audit into one ts-sorted list."""
    timeline: list[dict] = []

    for c in result["commands"]:
        timeline.append({
            "t": c.get("ts") or 0.0,
            "kind": "command",
            "label": f"command [{c.get('source')}] {c.get('action') or c.get('text', '')!r}",
            "detail": c,
        })
    for s in result["spans"]:
        dur = s.get("dur_ms")
        attrs = _safe_json(s.get("attrs_json"))
        timeline.append({
            "t": s.get("ts") or 0.0,
            "kind": "span",
            "label": f"{s.get('stage')}" + (f" ({dur:.1f}ms)" if dur is not None else ""),
            "detail": {**s, "attrs": attrs},
        })
    for i in result["inferences"]:
        ti, to = i.get("tokens_in"), i.get("tokens_out")
        tok = f" tokens={ti}/{to}" if (ti is not None or to is not None) else ""
        timeline.append({
            "t": i.get("ts") or 0.0,
            "kind": "inference",
            "label": f"inference {i.get('model')} [{i.get('backend')}]{tok}",
            "detail": i,
        })
    for e in result["events"]:
        timeline.append({
            "t": e.get("ts") or 0.0,
            "kind": "event",
            "label": f"event {e.get('topic')}",
            "detail": {**e, "payload": _safe_json(e.get("payload"))},
        })
    for a in result["audit"]:
        timeline.append({
            "t": a.get("ts") or 0.0,
            "kind": "audit",
            "label": f"audit [{a.get('severity')}] {a.get('event_type')} {a.get('tool') or ''}".rstrip(),
            "detail": a,
        })

    timeline.sort(key=lambda x: x["t"])
    return timeline


def _build_summary(result: dict) -> dict:
    cmd = result["command"] or {}
    tokens_in = sum((i.get("tokens_in") or 0) for i in result["inferences"])
    tokens_out = sum((i.get("tokens_out") or 0) for i in result["inferences"])
    return {
        "found": bool(result["commands"]),
        "session_id": cmd.get("session_id"),
        "source": cmd.get("source"),
        "route": cmd.get("route"),
        "gate": cmd.get("gate_that_decided"),
        "success": cmd.get("success"),
        "latency_ms": cmd.get("latency_ms"),
        "n_spans": len(result["spans"]),
        "n_events": len(result["events"]),
        "n_inferences": len(result["inferences"]),
        "n_audit": len(result["audit"]),
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "stages": [s.get("stage") for s in result["spans"]],
    }


def recent_traces(agent_db_path: str = "agent.db", limit: int = 20,
                  source: Optional[str] = None, success: Optional[bool] = None,
                  action: Optional[str] = None) -> list[dict]:
    """List recent commands that carry a trace_id (newest first).

    Optional `source` / `success` / `action` filters narrow the list (R7.1). Each
    row sums tokens from its inferences and carries `error_msg` so a failed trace
    shows its reason inline without a replay (R7.2). Backward-compatible: the CLI
    `--recent` path calls this with just (path, limit).
    """
    adb = _connect_ro(agent_db_path)
    try:
        where = ["c.trace_id IS NOT NULL", "c.trace_id != ''"]
        params: list = []
        if source:
            where.append("c.source = ?"); params.append(source)
        if action:
            where.append("c.action = ?"); params.append(action)
        if success is not None:
            where.append("c.success = ?"); params.append(1 if success else 0)
        params.append(int(limit))
        return _rows(
            adb,
            "SELECT c.trace_id AS trace_id, c.ts AS ts, c.source AS source, "
            "c.action AS action, c.route AS route, c.success AS success, "
            "c.latency_ms AS latency_ms, c.error_msg AS error_msg, "
            "COALESCE(SUM(i.tokens_in), 0) AS tokens_in, "
            "COALESCE(SUM(i.tokens_out), 0) AS tokens_out "
            "FROM commands c LEFT JOIN inferences i ON i.command_id = c.id "
            "WHERE " + " AND ".join(where) + " "
            "GROUP BY c.id ORDER BY c.ts DESC LIMIT ?",
            tuple(params),
        )
    finally:
        if adb is not None:
            adb.close()


def _safe_json(v):
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except Exception:
        return v


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def format_timeline(result: dict) -> str:
    """Render a replay_trace() result as a readable text timeline."""
    s = result["summary"]
    if not s.get("found"):
        return (f"No command found for trace_id {result['trace_id']!r}.\n"
                f"(Tracing is on by default; try `--recent` to list known trace ids.)")

    import datetime
    lines = [
        f"Trace {result['trace_id']}  —  session {s.get('session_id')}",
        (f"  source={s.get('source')}  route={s.get('route')}  gate={s.get('gate')}  "
         f"success={s.get('success')}  latency={_ms(s.get('latency_ms'))}"),
        (f"  spans={s.get('n_spans')}  events={s.get('n_events')}  "
         f"inferences={s.get('n_inferences')}  audit={s.get('n_audit')}  "
         f"tokens={s.get('tokens_in')}/{s.get('tokens_out')}"),
        "",
        "  TIME          ±ms     KIND        DETAIL",
        "  " + "-" * 72,
    ]
    timeline = result["timeline"]
    t0 = timeline[0]["t"] if timeline else 0.0
    for entry in timeline:
        rel = (entry["t"] - t0) * 1000.0
        clock = datetime.datetime.fromtimestamp(entry["t"]).strftime("%H:%M:%S.%f")[:-3]
        lines.append(f"  {clock}  {rel:7.0f}  {entry['kind']:<10}  {entry['label']}")
    return "\n".join(lines)


def _ms(v) -> str:
    return f"{v:.0f}ms" if v is not None else "N/A"


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main() -> None:
    import argparse
    p = argparse.ArgumentParser(description="Replay a command's full trace timeline.")
    p.add_argument("trace_id", nargs="?", help="trace_id to replay")
    p.add_argument("--agent-db", default="agent.db")
    p.add_argument("--audit-db", default="audit.db")
    p.add_argument("--recent", type=int, metavar="N",
                   help="list the N most recent trace ids instead of replaying one")
    p.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = p.parse_args()

    if args.recent:
        rows = recent_traces(args.agent_db, args.recent)
        if args.json:
            print(json.dumps(rows, indent=2))
            return
        if not rows:
            print("No traced commands found in", args.agent_db)
            return
        print(f"{'TRACE_ID':<12}  {'SOURCE':<10}  {'ACTION':<12}  {'ROUTE':<8}  OK  LAT")
        for r in rows:
            print(f"{(r.get('trace_id') or ''):<12}  {(r.get('source') or ''):<10}  "
                  f"{(r.get('action') or ''):<12}  {(r.get('route') or ''):<8}  "
                  f"{r.get('success')!s:<3} {_ms(r.get('latency_ms'))}")
        return

    if not args.trace_id:
        p.error("provide a trace_id, or use --recent N")

    result = replay_trace(args.trace_id, args.agent_db, args.audit_db)
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(format_timeline(result))


if __name__ == "__main__":
    main()
