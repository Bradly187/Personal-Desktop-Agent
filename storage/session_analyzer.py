"""storage.session_analyzer.py — DuckDB analytical queries over AgentDB.

Runs at session close (called by main.py) and can also be invoked standalone
for ad-hoc analysis.  Produces a dict of KPIs written to session_summaries and
an optional markdown report.

Requirements addressed:
  - Gate distribution (how often each gate decided routing)
  - Latency p50/p95 per domain and overall
  - Source accuracy (success rate per sensor source)
  - Pain-day correlation (command success vs pain_day_active)
  - Domain mix (command/code/math/vision/plan/general breakdown)
  - Top-10 action verbs
  - Cloud escalation rate over time (trend)
  - Whisper logprob and gesture confidence distributions
  - Sensor telemetry summary (mean tilt magnitude, gaze activity, head activity)
"""

from __future__ import annotations

import json
import logging
import statistics
import time
from pathlib import Path
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from storage.db import AgentDB

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SessionAnalyzer
# ---------------------------------------------------------------------------

class SessionAnalyzer:
    """Runs DuckDB analytical queries over agent.db and writes session_summaries.

    Usage:
        analyzer = SessionAnalyzer(agent_db_path="agent.db")
        summary = await analyzer.analyze(session_id=42)   # async (uses AgentDB)
        report  = analyzer.format_report(summary)
        print(report)
    """

    def __init__(self, agent_db_path: str = "agent.db") -> None:
        self._db_path = str(agent_db_path)
        self._duck = None   # DuckDB connection — opened lazily

    # ---------------------------------------------------------------------- #
    # DuckDB connection
    # ---------------------------------------------------------------------- #

    def _get_duck(self):
        """Return a DuckDB connection with agent.db attached (lazy init)."""
        if self._duck is not None:
            return self._duck
        try:
            import duckdb
            conn = duckdb.connect(database=":memory:")
            conn.execute(f"ATTACH '{self._db_path}' AS ops (TYPE SQLITE)")
            self._duck = conn
            return conn
        except Exception as exc:
            log.warning("SessionAnalyzer: DuckDB unavailable: %s", exc)
            return None

    def close(self) -> None:
        if self._duck is not None:
            try:
                self._duck.close()
            except Exception:
                pass
            self._duck = None

    # ---------------------------------------------------------------------- #
    # Core analysis — runs DuckDB queries for a single session
    # ---------------------------------------------------------------------- #

    def _query(self, sql: str, params=None):
        """Run a DuckDB query; return list-of-dicts or empty list on error."""
        conn = self._get_duck()
        if conn is None:
            return []
        try:
            if params:
                result = conn.execute(sql, params).fetchall()
            else:
                result = conn.execute(sql).fetchall()
            cols = [d[0] for d in conn.description]
            return [dict(zip(cols, row)) for row in result]
        except Exception as exc:
            log.debug("SessionAnalyzer query failed: %s | SQL: %s", exc, sql[:120])
            return []

    def _scalar(self, sql: str, params=None, default=None):
        rows = self._query(sql, params)
        if rows:
            return list(rows[0].values())[0]
        return default

    def analyze_session(self, session_id: int) -> dict:
        """Run all analytical queries for one session. Returns KPI dict."""
        sid = session_id
        ts_now = time.time()

        # ── Basic counts ──────────────────────────────────────────────────
        total = self._scalar(
            "SELECT COUNT(*) FROM ops.commands WHERE session_id = ?", (sid,), 0)
        success_count = self._scalar(
            "SELECT COUNT(*) FROM ops.commands WHERE session_id = ? AND success = 1", (sid,), 0)
        success_rate = (success_count / total) if total > 0 else None

        corrections = self._scalar(
            "SELECT COUNT(*) FROM ops.commands WHERE session_id = ? AND corrected_to IS NOT NULL",
            (sid,), 0)

        # ── Gate distribution ─────────────────────────────────────────────
        gate_rows = self._query(
            """SELECT gate_that_decided, COUNT(*) AS n
               FROM ops.commands WHERE session_id = ?
               GROUP BY gate_that_decided""", (sid,))
        gate_map = {r["gate_that_decided"] or "unknown": r["n"] for r in gate_rows}

        cloud_cmds = self._scalar(
            "SELECT COUNT(*) FROM ops.commands WHERE session_id = ? AND route = 'cloud'",
            (sid,), 0)
        cloud_rate = (cloud_cmds / total) if total > 0 else None

        # ── Latency percentiles ───────────────────────────────────────────
        latency_rows = self._query(
            "SELECT latency_ms FROM ops.commands WHERE session_id = ? AND latency_ms IS NOT NULL",
            (sid,))
        latencies = sorted(r["latency_ms"] for r in latency_rows)
        p50 = _percentile(latencies, 50)
        p95 = _percentile(latencies, 95)

        # ── Source breakdown ──────────────────────────────────────────────
        src_rows = self._query(
            """SELECT source, COUNT(*) AS n
               FROM ops.commands WHERE session_id = ?
               GROUP BY source ORDER BY n DESC""", (sid,))
        source_breakdown = {r["source"]: r["n"] for r in src_rows}

        # ── Domain breakdown ──────────────────────────────────────────────
        dom_rows = self._query(
            """SELECT domain, COUNT(*) AS n FROM ops.inferences i
               JOIN ops.commands c ON i.command_id = c.id
               WHERE c.session_id = ?
               GROUP BY domain ORDER BY n DESC""", (sid,))
        domain_breakdown = {r["domain"]: r["n"] for r in dom_rows}

        # ── Top actions ───────────────────────────────────────────────────
        act_rows = self._query(
            """SELECT action, COUNT(*) AS n FROM ops.commands
               WHERE session_id = ? AND action IS NOT NULL
               GROUP BY action ORDER BY n DESC LIMIT 10""", (sid,))
        top_actions = [[r["action"], r["n"]] for r in act_rows]

        # ── Voice quality ─────────────────────────────────────────────────
        avg_logprob = self._scalar(
            "SELECT AVG(whisper_logprob) FROM ops.commands WHERE session_id = ? AND whisper_logprob IS NOT NULL",
            (sid,))
        avg_gesture_conf = self._scalar(
            "SELECT AVG(gesture_confidence) FROM ops.commands WHERE session_id = ? AND gesture_confidence IS NOT NULL",
            (sid,))

        # ── Pain day fraction ─────────────────────────────────────────────
        pain_day_rows = self._query(
            "SELECT pain_day_active FROM ops.sensor_telemetry WHERE session_id = ?", (sid,))
        if pain_day_rows:
            pain_day_pct = sum(r["pain_day_active"] for r in pain_day_rows) / len(pain_day_rows)
        else:
            # Fall back to twin_pain_day_log
            pd_active = self._scalar(
                "SELECT AVG(pain_day_active) FROM ops.twin_pain_day_log WHERE session_id = ?",
                (sid,))
            pain_day_pct = pd_active

        # ── Session duration ──────────────────────────────────────────────
        duration = self._scalar(
            "SELECT ended_at - started_at FROM ops.sessions WHERE id = ?", (sid,))

        # ── Sensor activity (from telemetry) ──────────────────────────────
        telem_summary = self._query(
            """SELECT
                 COUNT(*) AS samples,
                 AVG(SQRT(tilt_rx * tilt_rx + tilt_ry * tilt_ry)) AS mean_tilt_mag,
                 AVG(gaze_conf) AS mean_gaze_conf,
                 AVG(ABS(head_pitch) + ABS(head_yaw)) AS mean_head_activity,
                 AVG(rms_ambient) AS mean_rms
               FROM ops.sensor_telemetry WHERE session_id = ?""", (sid,))
        telem = telem_summary[0] if telem_summary else {}

        return {
            "session_id": sid,
            "ts": ts_now,
            "duration_s": duration,
            "total_commands": total,
            "success_rate": success_rate,
            "cloud_escalation_rate": cloud_rate,
            "gate0_blocks": gate_map.get("gate0_privacy", 0),
            "gate1_blocks": gate_map.get("gate1_confidence", gate_map.get("discard", 0)),
            "gate2_blocks": gate_map.get("gate2_complexity", 0),
            "gate3_blocks": gate_map.get("gate3_vram", 0),
            "gate4_blocks": gate_map.get("gate4_latency", 0),
            "latency_p50_ms": p50,
            "latency_p95_ms": p95,
            "pain_day_pct": pain_day_pct,
            "corrections_count": corrections,
            "avg_whisper_logprob": avg_logprob,
            "avg_gesture_conf": avg_gesture_conf,
            "source_breakdown": json.dumps(source_breakdown),
            "domain_breakdown": json.dumps(domain_breakdown),
            "top_actions": json.dumps(top_actions),
            # Extra fields (not stored in DB, used for markdown report only)
            "_telem_samples": telem.get("samples", 0),
            "_mean_tilt_mag": telem.get("mean_tilt_mag"),
            "_mean_gaze_conf": telem.get("mean_gaze_conf"),
            "_mean_head_activity": telem.get("mean_head_activity"),
            "_mean_rms": telem.get("mean_rms"),
        }

    # ---------------------------------------------------------------------- #
    # Markdown report
    # ---------------------------------------------------------------------- #

    def format_report(self, summary: dict) -> str:
        """Format a session summary dict as a readable markdown string."""
        lines = [
            f"# Session {summary.get('session_id')} Summary",
            f"Generated: {_fmt_ts(summary.get('ts', 0))}",
            "",
            "## Commands",
            f"- **Total:** {summary.get('total_commands', 0)}",
            f"- **Success rate:** {_pct(summary.get('success_rate'))}",
            f"- **Corrections:** {summary.get('corrections_count', 0)}",
            f"- **Cloud escalation:** {_pct(summary.get('cloud_escalation_rate'))}",
            f"- **Duration:** {_fmt_dur(summary.get('duration_s'))}",
            "",
            "## Latency",
            f"- **p50:** {_ms(summary.get('latency_p50_ms'))}",
            f"- **p95:** {_ms(summary.get('latency_p95_ms'))}",
            "",
            "## Gate Distribution",
            f"- Gate 0 (Privacy blocks): {summary.get('gate0_blocks', 0)}",
            f"- Gate 1 (Confidence/discards): {summary.get('gate1_blocks', 0)}",
            f"- Gate 2 (Complexity → cloud): {summary.get('gate2_blocks', 0)}",
            f"- Gate 3 (VRAM → cloud): {summary.get('gate3_blocks', 0)}",
            f"- Gate 4 (Latency → cloud): {summary.get('gate4_blocks', 0)}",
            "",
            "## Voice Quality",
            f"- **Avg Whisper logprob:** {_float2(summary.get('avg_whisper_logprob'))}",
            f"- **Avg gesture confidence:** {_float2(summary.get('avg_gesture_conf'))}",
            f"- **Pain day fraction:** {_pct(summary.get('pain_day_pct'))}",
            "",
        ]

        src = _load_json(summary.get("source_breakdown"))
        if src:
            lines += ["## Source Breakdown"]
            for s, n in sorted(src.items(), key=lambda x: -x[1]):
                lines.append(f"  - {s}: {n}")
            lines.append("")

        dom = _load_json(summary.get("domain_breakdown"))
        if dom:
            lines += ["## Domain Breakdown"]
            for d, n in sorted(dom.items(), key=lambda x: -x[1]):
                lines.append(f"  - {d}: {n}")
            lines.append("")

        acts = _load_json(summary.get("top_actions"))
        if acts:
            lines += ["## Top Actions"]
            for a, n in (acts if isinstance(acts[0], list) else [[a, n] for a, n in acts.items()]):
                lines.append(f"  - {a}: {n}")
            lines.append("")

        ns = summary.get("_telem_samples", 0)
        if ns:
            lines += [
                "## Sensor Telemetry",
                f"- Samples: {ns}",
                f"- Mean tilt magnitude: {_float3(summary.get('_mean_tilt_mag'))} rad/s",
                f"- Mean gaze confidence: {_float2(summary.get('_mean_gaze_conf'))}",
                f"- Mean head activity: {_float2(summary.get('_mean_head_activity'))} °",
                f"- Mean RMS ambient: {_float3(summary.get('_mean_rms'))}",
                "",
            ]

        return "\n".join(lines)

    # ---------------------------------------------------------------------- #
    # Async wrapper — persists summary to AgentDB
    # ---------------------------------------------------------------------- #

    async def run_and_persist(self, session_id: int, agent_db: "AgentDB") -> dict:
        """Analyze session, persist summary, return summary dict."""
        import asyncio
        summary = await asyncio.to_thread(self.analyze_session, session_id)
        await agent_db.insert_session_summary(summary)
        log.info(
            "SessionAnalyzer: session %d — %d commands, success=%.0f%%, cloud=%.0f%%",
            session_id,
            summary.get("total_commands", 0),
            (summary.get("success_rate") or 0) * 100,
            (summary.get("cloud_escalation_rate") or 0) * 100,
        )
        return summary


# ---------------------------------------------------------------------------
# Standalone CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """CLI: python session_analyzer.py [session_id] [agent_db_path]"""
    import sys
    logging.basicConfig(level=logging.WARNING)
    args = sys.argv[1:]
    db_path = args[1] if len(args) > 1 else "agent.db"
    session_id = int(args[0]) if args else None

    analyzer = SessionAnalyzer(agent_db_path=db_path)

    if session_id is None:
        # Find the most recent session
        conn = analyzer._get_duck()
        if conn is None:
            print("DuckDB unavailable — install: pip install duckdb")
            return
        row = conn.execute(
            "SELECT id FROM ops.sessions ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        if not row:
            print("No sessions found in", db_path)
            return
        session_id = row[0]

    print(f"Analyzing session {session_id} from {db_path} …\n")
    summary = analyzer.analyze_session(session_id)
    print(analyzer.format_report(summary))
    analyzer.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _percentile(sorted_vals: list, p: int) -> Optional[float]:
    if not sorted_vals:
        return None
    idx = (len(sorted_vals) - 1) * p / 100
    lo, hi = int(idx), min(int(idx) + 1, len(sorted_vals) - 1)
    frac = idx - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def _pct(v) -> str:
    return f"{v * 100:.1f}%" if v is not None else "N/A"

def _ms(v) -> str:
    return f"{v:.1f} ms" if v is not None else "N/A"

def _float2(v) -> str:
    return f"{v:.2f}" if v is not None else "N/A"

def _float3(v) -> str:
    return f"{v:.3f}" if v is not None else "N/A"

def _fmt_ts(ts: float) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")

def _fmt_dur(s) -> str:
    if s is None:
        return "N/A"
    m, sec = divmod(int(s), 60)
    return f"{m}m {sec}s" if m else f"{sec}s"

def _load_json(v):
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except Exception:
        return None


if __name__ == "__main__":
    main()
