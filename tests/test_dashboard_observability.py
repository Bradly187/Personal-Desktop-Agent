"""Dashboard observability gap-closure (spec: specs/dashboard-observability-gaps).

P0 coverage:
  R1.3 — the metric-alert topic is published via the events.py constant.
  R3.1/R3.2 — _live_session_kpis returns the session rollup shape, 0 for an
              empty session, and {} (frontend fallback) on any error.
"""
from __future__ import annotations

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
