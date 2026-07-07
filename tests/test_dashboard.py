"""Unified observability dashboard (chat_server) — frame mapping + JSON routes.

The dashboard reuses the chat EventBus pump to broadcast ops events to every
client, and exposes read-only /api/* routes that wrap the monitoring/* modules.

Run:
    python -m pytest tests/test_dashboard.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.chat_server import ChatServer, _ChatClient, _DASHBOARD_TOPICS
from storage.db import AgentDB


def _env(topic, payload, trace_id=None):
    return {"topic": topic, "payload": payload, "trace_id": trace_id, "ts": 1000.0}


class _Req:
    """Minimal stand-in for aiohttp.web.Request (handlers use query/match_info only)."""
    def __init__(self, query=None, match=None):
        self.query = query or {}
        self.match_info = match or {}


# --------------------------------------------------------------------------- #
# Dashboard frame mapping
# --------------------------------------------------------------------------- #

def test_to_dashboard_frame_topic_mapping():
    f = ChatServer._to_dashboard_frame
    cmd = f(_env("command.executed", {"action": "CLICK", "route": "local",
                                       "success": True, "latency_ms": 12}))
    assert cmd["type"] == "dash_event" and cmd["kind"] == "command"
    assert cmd["action"] == "CLICK" and cmd["route"] == "local"

    gd = f(_env("goal.dequeued", {"goal_id": 5, "goal": "tidy", "source_trigger": "manual"}))
    assert gd["kind"] == "goal" and "5" in gd["text"]

    gc = f(_env("goal.completed", {"goal_id": 5, "status": "failed", "success": False}))
    assert gc["kind"] == "goal" and gc["severity"] == "warn"

    ev = f(_env("vram.evicted", {"free_gb": 12.0, "reason": "flare"}))
    assert ev["kind"] == "vram" and ev["severity"] == "warn"

    br = f(_env("breaker.opened", {"name": "ollama", "reason": "3 failures"}))
    assert br["kind"] == "breaker" and "ollama" in br["text"]

    st = f(_env("inference.stalled", {"backend": "ollama", "timeout_s": 45, "phase": "waiting"}))
    assert st["kind"] == "stall"

    # Non-ops topic → not a dashboard frame.
    assert f(_env("chat.token", {"text": "hi"})) is None


def test_dashboard_topics_are_covered_by_mapping():
    # Every broadcast topic must produce a frame (no silently-dropped ops event).
    samples = {
        "command.executed": {"action": "X"},
        "goal.dequeued": {"goal_id": 1, "goal": "g"},
        "goal.completed": {"goal_id": 1, "status": "done", "success": True},
        "vram.evicted": {"free_gb": 1.0, "reason": "flare"},
        "vram.restored": {"free_gb": 9.0},
        "breaker.opened": {"name": "ollama", "reason": "x"},
        "inference.stalled": {"backend": "ollama", "timeout_s": 45, "phase": "waiting"},
        "metric.threshold_crossed": {"metric": "misroute_rate", "value": 0.2,
                                     "severity": "warning", "message": "misroute_rate high"},
        "slo.breached": {"domain": "code", "metric": "latency_p95", "value": 900, "budget": 500},
        "voice.drift": {"drift_pct": 12},
        "step.failed": {"step_num": 3, "action": "WRITE_FILE", "error": "boom"},
        "replan.exhausted": {"replans": 3, "goal": "refactor module"},
        "email.arrived": {"subject": "Build failed"},
        "file.written": {"path": "/tmp/test.txt"},
    }
    assert set(samples) == _DASHBOARD_TOPICS
    for topic, payload in samples.items():
        assert ChatServer._to_dashboard_frame(_env(topic, payload)) is not None, topic


async def test_event_pump_broadcasts_ops_event_to_all_clients():
    events = [
        _env("vram.evicted", {"free_gb": 5.0, "reason": "flare"}, trace_id=None),  # broadcast, no trace
        _env("chat.token", {"text": "hi"}, trace_id="T2"),                          # unmatched chat frame
    ]

    class _FakeBus:
        async def subscribe(self, name, pattern, maxsize=256):
            for e in events:
                yield e

    cs = ChatServer()
    cs.set_event_bus(_FakeBus())
    a = _ChatClient(ws=MagicMock())
    b = _ChatClient(ws=MagicMock())
    cs._clients[a.ws_id] = a
    cs._clients[b.ws_id] = b

    await cs._event_pump()

    def _drain(c):
        out = []
        while not c._q.empty():
            out.append(c._q.get_nowait())
        return out

    fa, fb = _drain(a), _drain(b)
    # The vram.evicted broadcast reached BOTH clients; the trace-less chat token did not.
    assert [x["type"] for x in fa] == ["dash_event"]
    assert [x["type"] for x in fb] == ["dash_event"]
    assert fa[0]["kind"] == "vram"


# --------------------------------------------------------------------------- #
# JSON routes
# --------------------------------------------------------------------------- #

async def test_api_metrics_returns_snapshot():
    cs = ChatServer()
    resp = await cs._api_metrics(_Req())
    assert resp.status == 200
    data = json.loads(resp.body)
    assert "counters" in data and "gauges" in data


async def test_api_routes_read_agent_db(tmp_path):
    db = AgentDB()
    await db.open(tmp_path / "agent.db")
    if not db.available:
        pytest.skip("aiosqlite unavailable")
    sid = await db.sessions.insert_session(mode="test")
    # one traced command
    async with db._conn.execute(
        "INSERT INTO commands (session_id, ts, source, text, action, route, "
        "gate_that_decided, latency_ms, success, trace_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (sid, 1000.0, "voice", "hi", "CLICK", "local", "g", 50.0, 1, "tidABC"),
    ) as cur:
        cmd_id = cur.lastrowid
    await db._conn.commit()
    await db.inferences.insert_inference(command_id=cmd_id, model="claude-opus-4-8", domain="code",
                             prompt=None, response=None, tokens_in=1_000_000,
                             tokens_out=0, latency_ms=0.0, backend="bedrock")
    await db.sessions.insert_session_summary(dict(session_id=sid, ts=1000.0, total_commands=1,
                                         success_rate=1.0, cloud_escalation_rate=0.0,
                                         latency_p50_ms=50, latency_p95_ms=50, pain_day_pct=0.0,
                                         corrections_count=0))

    cs = ChatServer()
    cs.set_agent_db(db, sid)

    traces = json.loads((await cs._api_recent_traces(_Req(query={"limit": "10"}))).body)
    assert any(t["trace_id"] == "tidABC" for t in traces)

    replay = json.loads((await cs._api_replay(_Req(match={"tid": "tidABC"}))).body)
    assert replay["summary"]["found"] is True

    trends = json.loads((await cs._api_trends(_Req(query={"limit": "10"}))).body)
    assert trends["n_sessions"] == 1

    cost = json.loads((await cs._api_cost(_Req(query={"days": "30"}))).body)
    assert cost["totals"]["cost"] == pytest.approx(5.0)  # 1M in × $5/MTok (opus)

    await db.close()


def test_qint_falls_back_on_bad_input():
    assert ChatServer._qint(_Req(query={"limit": "7"}), "limit", 20) == 7
    assert ChatServer._qint(_Req(query={"limit": "abc"}), "limit", 20) == 20
    assert ChatServer._qint(_Req(query={}), "limit", 20) == 20
