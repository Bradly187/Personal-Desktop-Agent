"""Dashboard observability gap-closure (spec: specs/dashboard-observability-gaps).

P0 coverage:
  R1.3 — the metric-alert topic is published via the events.py constant.
  R3.1/R3.2 — _live_session_kpis returns the session rollup shape, 0 for an
              empty session, and {} (frontend fallback) on any error.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.chat_server import _live_session_kpis
from core import events


def test_metric_threshold_topic_constant():
    """R1.3 — MetricWatcher publishes via the constant, whose value is the
    canonical wire string the dashboard maps."""
    assert events.TOPIC_METRIC_THRESHOLD == "metric.threshold_crossed"
    assert events.TOPIC_SLO_BREACHED == "slo.breached"


def _seed_commands_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.execute(
        """CREATE TABLE commands (
               session_id INTEGER, success INTEGER, route TEXT,
               gate_that_decided TEXT, latency_ms REAL, source TEXT)"""
    )
    rows = [
        (7, 1, "local", "all_pass", 100.0, "voice"),
        (7, 0, "cloud", "gate4_latency", 900.0, "chat"),
        (7, 1, "local", "bypass", 50.0, "ipad"),
        (8, 1, "local", "bypass", 10.0, "voice"),   # different session — excluded
    ]
    con.executemany("INSERT INTO commands VALUES (?,?,?,?,?,?)", rows)
    con.commit()
    con.close()


def test_live_session_kpis_rollup(tmp_path):
    """R3.1 — current-session rollup matches session_summaries shape."""
    db = tmp_path / "agent.db"
    _seed_commands_db(db)
    k = _live_session_kpis(str(db), 7)
    assert k["session_id"] == 7
    assert k["total_commands"] == 3            # session 8 row excluded
    assert k["success_rate"] == round(2 / 3, 4)
    assert k["cloud_escalation_rate"] == round(1 / 3, 4)
    assert k["latency_p50_ms"] is not None
    assert set(k["source_counts"]) == {"voice", "chat", "ipad"}


def test_live_session_kpis_empty_session(tmp_path):
    """R3.2 — a session with no rows reports 0, not an error."""
    db = tmp_path / "agent.db"
    _seed_commands_db(db)
    k = _live_session_kpis(str(db), 999)
    assert k == {"session_id": 999, "total_commands": 0}


def test_live_session_kpis_bad_path_returns_empty():
    """R3.2 — unreadable DB → {} so the frontend falls back to /api/metrics."""
    assert _live_session_kpis("/nonexistent/agent.db", 1) == {}


# ── P1: Alerts read (R2.2, R2.5) ─────────────────────────────────────────────

def _seed_event_log(path: Path, rows) -> None:
    con = sqlite3.connect(path)
    con.execute(
        """CREATE TABLE event_log (
               id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, topic TEXT,
               session_id INTEGER, command_id INTEGER, trace_id TEXT,
               source TEXT, payload TEXT)"""
    )
    con.executemany(
        "INSERT INTO event_log (ts, topic, source, payload) VALUES (?,?,?,?)", rows
    )
    con.commit()
    con.close()


def test_recent_alerts_shapes_and_active_flag(tmp_path):
    """R2.2 — newest-first alerts with active vs recovered classification."""
    import json
    from core.chat_server import _recent_alerts
    db = tmp_path / "agent.db"
    _seed_event_log(db, [
        (1.0, "command.executed", "exec", json.dumps({"action": "CLICK"})),   # not an alert
        (2.0, "metric.threshold_crossed", "metric_watcher",
         json.dumps({"metric": "success_rate_1m", "value": 0.5, "message": "low", "severity": "warning"})),
        (3.0, "metric.threshold_crossed", "metric_watcher",
         json.dumps({"metric": "success_rate_1m", "value": 0.9, "message": "recovered", "severity": "info"})),
        (4.0, "slo.breached", "continuous_trainer",
         json.dumps({"domain": "command", "metric": "p50_latency_ms", "value": 800, "budget": 600})),
    ])
    out = _recent_alerts(str(db), 50)["alerts"]
    assert len(out) == 3                       # the command.executed row is excluded
    assert out[0]["topic"] == "slo.breached" and out[0]["active"] is True   # newest first
    recovered = [a for a in out if a["topic"] == "metric.threshold_crossed" and not a["active"]]
    assert len(recovered) == 1                 # the severity:"info" one is recovered


def test_recent_alerts_bad_path_returns_empty():
    """R2.5 — unreadable DB → {"alerts": []} so the panel degrades, never errors."""
    from core.chat_server import _recent_alerts
    assert _recent_alerts("/nonexistent/agent.db", 10) == {"alerts": []}


# ── P1: Backend health (R6.4 — no secret leak) ───────────────────────────────

async def test_health_probe_reports_bedrock_presence_without_leaking_token():
    """R6.1, R6.4 — bedrock reports presence only; the token never appears."""
    import json
    from core.chat_server import ChatServer
    prev = os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
    os.environ["AWS_BEARER_TOKEN_BEDROCK"] = "SECRET_SENTINEL_DO_NOT_LEAK"
    try:
        result = await ChatServer._probe_backends()
    finally:
        if prev is None:
            os.environ.pop("AWS_BEARER_TOKEN_BEDROCK", None)
        else:
            os.environ["AWS_BEARER_TOKEN_BEDROCK"] = prev
    names = {b["name"] for b in result["backends"]}
    assert names == {"ollama", "action-proxy", "bedrock"}
    bedrock = next(b for b in result["backends"] if b["name"] == "bedrock")
    assert bedrock["status"] == "configured"
    assert "SECRET_SENTINEL_DO_NOT_LEAK" not in json.dumps(result)


# ── P1: ContinuousTrainer publishes slo.breached (R2.1) ──────────────────────

async def test_continuous_trainer_publishes_slo_breached():
    """R2.1 — a per-domain SLO breach publishes slo.breached on the wired bus."""
    from types import SimpleNamespace
    from adaptive.continuous_trainer import ContinuousTrainer
    from core.slo import SLOConfig

    published = []

    class _Bus:
        async def publish(self, topic, source=None, payload=None):
            published.append((topic, payload))

    class _DB:
        available = True
        async def get_inference_stats_by_domain(self, limit=1000):
            # command budget is 600 ms / 0.95 — this breaches latency.
            return {"command": {"count": 100, "p50_latency_ms": 5000.0, "success_rate": 0.99}}
        async def log_adaptation(self, **kw):
            return None

    t = ContinuousTrainer(agent_db=_DB(), config=SimpleNamespace(slo=SLOConfig()))
    t.set_event_bus(_Bus())
    await t._adapt_per_domain_slo()

    assert published, "expected an slo.breached publish"
    topic, payload = published[0]
    assert topic == "slo.breached"
    assert payload["domain"] == "command" and payload["verdict"] == "breach_latency"
    assert payload["budget"] == 600.0


# ── P2: trace filters + tokens/error (R7.1, R7.2) ────────────────────────────

def _seed_traces_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.execute(
        """CREATE TABLE commands (
               id INTEGER PRIMARY KEY, session_id INTEGER, ts REAL, source TEXT,
               text TEXT, action TEXT, route TEXT, gate_that_decided TEXT,
               latency_ms REAL, success INTEGER, error_msg TEXT,
               corrected_to TEXT, trace_id TEXT)"""
    )
    con.execute("CREATE TABLE inferences (id INTEGER PRIMARY KEY, command_id INTEGER, tokens_in INTEGER, tokens_out INTEGER)")
    con.executemany(
        "INSERT INTO commands (id, ts, source, text, action, route, success, error_msg, corrected_to, trace_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            (1, 10.0, "voice", "open vscode", "OPEN", "local", 1, None, None, "tA"),
            (2, 20.0, "chat", "do thing", "RUN_TERMINAL", "cloud", 0, "boom failed", None, "tB"),
            (3, 30.0, "voice", "click ok", "CLICK", "local", 1, None, "DOUBLECLICK", "tC"),
        ],
    )
    con.executemany("INSERT INTO inferences (command_id, tokens_in, tokens_out) VALUES (?,?,?)",
                    [(1, 100, 20), (1, 50, 5), (2, 200, 80)])
    con.commit()
    con.close()


def test_recent_traces_tokens_and_error_inline(tmp_path):
    """R7.2 — rows carry summed tokens + the failed command's error_msg."""
    from monitoring.replay import recent_traces
    db = tmp_path / "agent.db"
    _seed_traces_db(db)
    rows = recent_traces(str(db), 25)
    by_id = {r["trace_id"]: r for r in rows}
    assert by_id["tA"]["tokens_in"] == 150 and by_id["tA"]["tokens_out"] == 25   # summed
    assert by_id["tB"]["error_msg"] == "boom failed" and by_id["tB"]["success"] == 0


def test_recent_traces_filters(tmp_path):
    """R7.1 — source / success filters narrow the list."""
    from monitoring.replay import recent_traces
    db = tmp_path / "agent.db"
    _seed_traces_db(db)
    assert {r["trace_id"] for r in recent_traces(str(db), 25, source="voice")} == {"tA", "tC"}
    assert {r["trace_id"] for r in recent_traces(str(db), 25, success=False)} == {"tB"}
    assert {r["trace_id"] for r in recent_traces(str(db), 25, action="CLICK")} == {"tC"}


# ── P2: operational read helpers (R8.1, R8.5) ────────────────────────────────

def test_operational_read_helpers(tmp_path):
    from core.chat_server import _recent_goals, _recent_escalations, _recent_corrections
    db = tmp_path / "agent.db"
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE goal_queue (id INTEGER PRIMARY KEY, ts REAL, goal TEXT,
                   domain TEXT, status TEXT, attempts INTEGER, max_attempts INTEGER,
                   last_error TEXT, source_trigger TEXT)""")
    con.execute("INSERT INTO goal_queue (ts, goal, domain, status, attempts, max_attempts, source_trigger) "
                "VALUES (1, 'tidy desktop', 'plan', 'queued', 0, 3, 'manual')")
    con.execute("""CREATE TABLE dev_escalations (id INTEGER PRIMARY KEY, ts REAL, goal TEXT,
                   reason TEXT, failed_action TEXT, replans INTEGER, status TEXT)""")
    con.execute("INSERT INTO dev_escalations (ts, goal, reason, failed_action, replans, status) "
                "VALUES (1, 'refactor', 'max_replans', 'WRITE_FILE', 3, 'pending')")
    con.execute("""CREATE TABLE commands (id INTEGER PRIMARY KEY, ts REAL, source TEXT,
                   text TEXT, action TEXT, corrected_to TEXT)""")
    con.execute("INSERT INTO commands (ts, source, text, action, corrected_to) "
                "VALUES (1, 'voice', 'click ok', 'CLICK', 'DOUBLECLICK')")
    con.commit(); con.close()

    assert _recent_goals(str(db))["goals"][0]["status"] == "queued"
    assert _recent_escalations(str(db))["escalations"][0]["reason"] == "max_replans"
    assert _recent_corrections(str(db))["corrections"][0]["corrected_to"] == "DOUBLECLICK"
    # R8.5 — bad path degrades to empty, never errors.
    assert _recent_goals("/nope/agent.db") == {"goals": []}
    assert _recent_escalations("/nope/agent.db") == {"escalations": []}
    assert _recent_corrections("/nope/agent.db") == {"corrections": []}


def test_no_mutation_routes_for_operational_panels():
    """R8.3 — the operational/approval surface is GET-only; no approve/deny (or any
    mutation) route exists that could bypass the voice gate. The sole permitted
    mutation route is ``/upload`` (chat file attachments, specs/chat-context-attachments
    R2.1) — a hardened endpoint that never touches the approval gate. Any new
    mutation route on an ``/api/*`` or approve/deny path must fail this guard."""
    import inspect
    import re
    from core.chat_server import ChatServer
    src = inspect.getsource(ChatServer.start)
    assert 'add_get("/api/escalations"' in src
    assert 'add_get("/api/goals"' in src
    assert 'add_get("/api/corrections"' in src
    # Every mutation route registered must be on the allowlist of known-safe paths.
    _MUTATION_ALLOWLIST = {"/upload"}
    mutation_paths = re.findall(r'add_(?:post|put|delete)\(\s*"([^"]+)"', src)
    assert set(mutation_paths) <= _MUTATION_ALLOWLIST, (
        f"unexpected mutation route(s): {set(mutation_paths) - _MUTATION_ALLOWLIST}")
