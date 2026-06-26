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
