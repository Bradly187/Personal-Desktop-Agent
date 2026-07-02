"""specs/chat-workbench-parity — frame plumbing.

R1: every trace-targeted frame the pump forwards carries its trace_id, and
_start_request pushes an `accepted` frame so the client can bind its turn.
R3.3: dag.step_completed carries args_snippet (capped) end-to-end.
R8.1/R8.2: walkthrough + run_finalized topics map to chat frames.
R8.4: _trace_usage aggregates a trace's inference rows; {} when absent.
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.chat_server import ChatServer, _ChatClient, _trace_usage


def _env(topic, payload, trace_id):
    return {"topic": topic, "payload": payload, "trace_id": trace_id}


def _drain(client):
    frames = []
    while not client._q.empty():
        frames.append(client._q.get_nowait())
    return frames


# --------------------------------------------------------------------------- #
# R1.1 — the pump stamps trace_id on EVERY forwarded frame
# --------------------------------------------------------------------------- #

async def test_pump_stamps_trace_id_on_every_frame():
    events = [
        _env("gate.decided", {"gate": "all_pass"}, "T1"),
        _env("chat.token", {"text": "hi"}, "T1"),
        _env("dag.step_completed", {"n": 1, "action": "X", "success": True}, "T1"),
    ]

    class _FakeBus:
        async def subscribe(self, name, pattern, maxsize=256):
            for e in events:
                yield e

    cs = ChatServer()
    cs.set_event_bus(_FakeBus())
    client = _ChatClient(ws=MagicMock())
    cs._clients[client.ws_id] = client
    cs._active["T1"] = client.ws_id

    await cs._event_pump()
    frames = _drain(client)
    assert len(frames) == 3
    assert all(fr["trace_id"] == "T1" for fr in frames)


# --------------------------------------------------------------------------- #
# R1 — _start_request pushes `accepted` before the route task can emit
# --------------------------------------------------------------------------- #

async def test_start_request_pushes_accepted_frame():
    cs = ChatServer()
    done = asyncio.get_running_loop().create_future()
    done.set_result({"status": "ok", "response": "hi"})

    scheduler = MagicMock()
    scheduler.submit = MagicMock(side_effect=lambda coro, *a, **k: (coro.close(), done)[1])
    coordinator = MagicMock()
    coordinator.route = MagicMock(return_value=_noop_coro())
    cs.set_scheduler(scheduler)
    cs.set_coordinator(coordinator)

    client = _ChatClient(ws=MagicMock())
    await cs._start_request(client, "hello")
    # Wait for the request task to finish.
    await asyncio.gather(*cs._requests.values())

    frames = _drain(client)
    assert frames[0]["type"] == "accepted"
    tid = frames[0]["trace_id"]
    final = [fr for fr in frames if fr["type"] == "final"]
    assert final and final[0]["trace_id"] == tid


async def _noop_coro():
    return None


# --------------------------------------------------------------------------- #
# R3.3 — args_snippet rides dag.step_completed, capped
# --------------------------------------------------------------------------- #

async def test_step_completed_carries_capped_args_snippet():
    from inference.dev_agent import AgentStep, DevAgent

    class _Bus:
        def __init__(self):
            self.events = []

        async def publish(self, topic, payload, source="", trace_id=""):
            self.events.append((topic, payload))

    agent = DevAgent(router=MagicMock())
    bus = _Bus()
    agent.set_event_bus(bus)
    agent._active_trace_id = "T"
    step = AgentStep(action="READ_FILE", args="x" * 500)
    step.success = True
    step.latency_ms = 5.0
    await agent._emit_step_completed(step, 1)

    topic, payload = bus.events[-1]
    assert topic == "dag.step_completed"
    assert payload["args_snippet"] == "x" * DevAgent._ARGS_SNIPPET_CHARS
    assert len(payload["args_snippet"]) <= 2000  # spec cap


def test_to_frame_node_carries_args():
    f = ChatServer._to_frame(_env(
        "dag.step_completed",
        {"n": 2, "action": "READ_FILE", "success": True,
         "result_snippet": "text", "args_snippet": "f.py"}, "t"))
    assert f["args"] == "f.py" and f["result"] == "text"


# --------------------------------------------------------------------------- #
# R8.1 / R8.2 — walkthrough + run_finalized frames
# --------------------------------------------------------------------------- #

def test_walkthrough_and_run_finalized_frames():
    w = ChatServer._to_frame(_env("dag.walkthrough", {"markdown": "# Done"}, "t"))
    assert w == {"type": "walkthrough", "markdown": "# Done"}
    r = ChatServer._to_frame(_env(
        "dag.run_finalized", {"run_id": 7, "status": "completed", "rewindable": True}, "t"))
    assert r["type"] == "run_finalized" and r["rewindable"] is True and r["run_id"] == 7


# --------------------------------------------------------------------------- #
# R8.4 — _trace_usage aggregation (and clean omission)
# --------------------------------------------------------------------------- #

def _mk_db(tmp_path):
    db = tmp_path / "agent.db"
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE commands (id INTEGER PRIMARY KEY, trace_id TEXT);
        CREATE TABLE inferences (
            id INTEGER PRIMARY KEY, command_id INTEGER, model TEXT,
            backend TEXT, tokens_in INTEGER, tokens_out INTEGER, latency_ms REAL);
    """)
    con.execute("INSERT INTO commands (id, trace_id) VALUES (1, 'T1')")
    con.execute("INSERT INTO inferences (command_id, model, backend, tokens_in, tokens_out, latency_ms)"
                " VALUES (1, 'llama3.1:8b', 'ollama', 100, 40, 200.0)")
    con.execute("INSERT INTO inferences (command_id, model, backend, tokens_in, tokens_out, latency_ms)"
                " VALUES (1, 'qwen3-coder:30b', 'ollama', 900, 300, 900.0)")
    con.commit()
    con.close()
    return str(db)


def test_trace_usage_aggregates(tmp_path):
    path = _mk_db(tmp_path)
    u = _trace_usage(path, "T1")
    assert u["tokens_in"] == 1000 and u["tokens_out"] == 340
    assert u["inferences"] == 2
    assert set(u["models"]) == {"llama3.1:8b", "qwen3-coder:30b"}
    assert u["cost_usd"] == 0.0  # local models are unpriced


def test_trace_usage_empty_and_bad_db(tmp_path):
    path = _mk_db(tmp_path)
    assert _trace_usage(path, "NOPE") == {}          # nothing recorded → omit
    assert _trace_usage(str(tmp_path / "missing.db"), "T1") == {}  # never raises
