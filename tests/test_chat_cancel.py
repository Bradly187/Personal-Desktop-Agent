"""specs/chat-workbench-parity R4 — chat Stop / cancel.

R4.1: a cancel message kills the in-flight request task and resolves the turn
with a cancelled final frame. R4.2: the DevAgent is signalled to halt
gracefully (request_dev_cancel). R4.3: cancel for an unknown/finished trace is
an idempotent no-op. R4.4: a cancel NEVER writes the approval signal file —
a gate blocked on approval resolves as on silence: DENY.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from core import chat_server as cs_mod
from core.chat_server import ChatServer, _ChatClient


def _drain(client):
    frames = []
    while not client._q.empty():
        frames.append(client._q.get_nowait())
    return frames


def _server_with_hanging_request():
    """ChatServer whose scheduler future never resolves (a long-running plan)."""
    cs = ChatServer()
    hang = asyncio.get_event_loop().create_future()
    scheduler = MagicMock()
    scheduler.submit = MagicMock(side_effect=lambda coro, *a, **k: (coro.close(), hang)[1])
    coordinator = MagicMock()
    coordinator.route = MagicMock(return_value=_noop_coro())
    coordinator.request_dev_cancel = MagicMock(return_value=True)
    cs.set_scheduler(scheduler)
    cs.set_coordinator(coordinator)
    return cs, coordinator


async def _noop_coro():
    return None


async def test_cancel_resolves_turn_and_signals_dev_agent(tmp_path, monkeypatch):
    monkeypatch.setattr(cs_mod, "_APPROVAL_DIR", tmp_path / "approval")
    cs, coordinator = _server_with_hanging_request()
    client = _ChatClient(ws=MagicMock())
    await cs._start_request(client, "long plan")
    tid = next(iter(cs._requests))

    cs._cancel_request(tid)
    await asyncio.gather(*cs._requests.values(), return_exceptions=True)

    frames = _drain(client)
    final = [f for f in frames if f["type"] == "final"]
    assert final and final[0]["cancelled"] is True
    assert final[0]["trace_id"] == tid
    assert final[0]["result"]["response"] == "(cancelled)"
    # R4.2 — graceful halt requested from the actual worker.
    coordinator.request_dev_cancel.assert_called_once()
    # R4.4 — the approval signal file was never written by the cancel path.
    assert not (tmp_path / "approval" / "response").exists()
    # Bookkeeping cleaned up.
    assert tid not in cs._active and tid not in cs._user_cancelled


async def test_cancel_unknown_trace_is_noop():
    cs, coordinator = _server_with_hanging_request()
    cs._cancel_request("does-not-exist")        # must not raise (R4.3)
    coordinator.request_dev_cancel.assert_not_called()


async def test_cancel_finished_trace_is_noop():
    cs = ChatServer()
    done_future = asyncio.get_event_loop().create_future()
    done_future.set_result({"response": "hi"})
    scheduler = MagicMock()
    scheduler.submit = MagicMock(side_effect=lambda coro, *a, **k: (coro.close(), done_future)[1])
    coordinator = MagicMock()
    coordinator.route = MagicMock(return_value=_noop_coro())
    coordinator.request_dev_cancel = MagicMock()
    cs.set_scheduler(scheduler)
    cs.set_coordinator(coordinator)
    client = _ChatClient(ws=MagicMock())
    await cs._start_request(client, "quick")
    tid = next(iter(cs._requests))
    await asyncio.gather(*cs._requests.values())

    cs._cancel_request(tid)                     # already done → ignored
    coordinator.request_dev_cancel.assert_not_called()
    frames = _drain(client)
    assert not any(f.get("cancelled") for f in frames)


async def test_shutdown_cancellation_still_propagates():
    """A cancel NOT initiated by the user (e.g. stop()) re-raises so shutdown
    semantics are unchanged."""
    cs, _ = _server_with_hanging_request()
    client = _ChatClient(ws=MagicMock())
    await cs._start_request(client, "long plan")
    tid, task = next(iter(cs._requests.items()))
    task.cancel()   # no _user_cancelled marker → shutdown path
    results = await asyncio.gather(task, return_exceptions=True)
    assert isinstance(results[0], asyncio.CancelledError)
    frames = _drain(client)
    assert not any(f["type"] == "final" for f in frames)
