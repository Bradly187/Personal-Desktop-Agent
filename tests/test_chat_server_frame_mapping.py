"""ChatServer maps EventBus envelopes to client frames and fans them by trace_id.

_to_frame translates each topic to a UI frame; the event pump must deliver a
frame ONLY to the socket whose in-flight request matches the event's trace_id
(and ignore trace-less / unmatched events).
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.chat_server import ChatServer, _ChatClient


def _env(topic, payload, trace_id):
    return {"topic": topic, "payload": payload, "trace_id": trace_id}


def test_to_frame_topic_mapping():
    f = ChatServer._to_frame
    assert f(_env("gate.decided", {"gate": "all_pass", "latency_ms": 12, "domain": "command"}, "t"))["type"] == "gate"
    assert f(_env("command.executed", {"action": "CLICK", "route": "local", "success": True}, "t"))["type"] == "executed"
    assert f(_env("plan.generated", {"goal": "g", "steps": [{"n": 1}]}, "t"))["type"] == "plan"

    started = f(_env("dag.step_started", {"n": 2, "action": "WRITE_FILE"}, "t"))
    assert started == {"type": "node", "n": 2, "status": "running", "action": "WRITE_FILE"}

    done = f(_env("dag.step_completed", {"n": 2, "action": "WRITE_FILE", "success": True,
                                          "latency_ms": 9, "result_snippet": "ok"}, "t"))
    assert done["type"] == "node" and done["status"] == "success"

    failed = f(_env("dag.step_completed", {"n": 3, "action": "RUN_TERMINAL", "success": False}, "t"))
    assert failed["status"] == "failed"

    appr = f(_env("dag.approval_requested", {"message": "Approve all?", "destructive": True}, "t"))
    assert appr == {"type": "approval", "message": "Approve all?", "destructive": True}

    assert f(_env("chat.token", {"text": "hi"}, "t"))["type"] == "token"
    assert f(_env("step.failed", {"step_num": 1, "action": "X", "error": "boom"}, "t"))["type"] == "activity"
    # Unknown topic → ignored.
    assert f(_env("some.other.topic", {}, "t")) is None


def test_to_dashboard_frame_mapping():
    """R1.1, R1.2, R1.4 — new alert/ops topics map to feed frames; the original
    7 mappings stay intact; unknown topics are ignored."""
    f = ChatServer._to_dashboard_frame

    def env(topic, payload=None, ts=123.0):
        return {"topic": topic, "payload": payload or {}, "ts": ts}

    # Existing mappings unchanged (spot-check) — R1.4.
    assert f(env("command.executed", {"action": "CLICK", "route": "local",
                                       "success": True, "latency_ms": 5}))["kind"] == "command"
    assert f(env("vram.evicted", {"reason": "flare", "free_gb": 7.3}))["kind"] == "vram"
    assert f(env("inference.stalled", {"backend": "ollama", "timeout_s": 45}))["kind"] == "stall"

    # New alert mappings — R1.1.
    a = f(env("metric.threshold_crossed", {"metric": "success_rate_1m", "value": 0.5,
                                           "message": "low", "severity": "warning"}))
    assert a["kind"] == "alert" and a["severity"] == "warn" and a["text"] == "low"
    rec = f(env("metric.threshold_crossed", {"metric": "x", "value": 0.9,
                                             "message": "recovered", "severity": "info"}))
    assert rec["kind"] == "alert" and rec["severity"] == "info"
    slo = f(env("slo.breached", {"domain": "command", "metric": "p50",
                                 "value": 584, "budget": 600}))
    assert slo["kind"] == "alert" and slo["severity"] == "warn"

    # New ops/autonomous mappings — R1.2.
    assert f(env("voice.drift", {"drift_pct": 12}))["kind"] == "voice"
    assert f(env("step.failed", {"step_num": 1, "action": "WRITE_FILE",
                                 "error": "boom"}))["kind"] == "dev"
    assert f(env("replan.exhausted", {"goal": "g", "replans": 3}))["kind"] == "dev"
    assert f(env("email.arrived", {"subject": "hi"}))["kind"] == "email"

    # Unknown topic → ignored.
    assert f(env("some.other.topic")) is None

    # Every new topic is gated into the broadcast set, else the pump never calls
    # _to_dashboard_frame for it.
    from core.chat_server import _DASHBOARD_TOPICS
    for t in ("metric.threshold_crossed", "slo.breached", "voice.drift",
              "step.failed", "replan.exhausted", "email.arrived"):
        assert t in _DASHBOARD_TOPICS


async def test_event_pump_fans_only_matching_trace_id():
    events = [
        _env("gate.decided", {"gate": "all_pass"}, "T1"),         # → client A
        _env("dag.step_started", {"n": 1, "action": "X"}, "T1"),  # → client A
        _env("chat.token", {"text": "hi"}, "T2"),                 # no client for T2
        _env("gate.decided", {"gate": "x"}, None),                # no trace_id → skip
    ]

    class _FakeBus:
        async def subscribe(self, name, pattern, maxsize=256):
            for e in events:
                yield e

    cs = ChatServer()
    cs.set_event_bus(_FakeBus())
    client = _ChatClient(ws=MagicMock())   # do not start() — inspect the queue directly
    cs._clients[client.ws_id] = client
    cs._active["T1"] = client.ws_id

    await cs._event_pump()   # fake bus generator ends → pump returns

    frames = []
    while not client._q.empty():
        frames.append(client._q.get_nowait())

    types = [fr["type"] for fr in frames]
    assert types == ["gate", "node"]        # only the two T1 events, in order
