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
