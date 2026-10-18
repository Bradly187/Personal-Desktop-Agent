"""specs/chat-workbench-parity R5/R6/R8.3 — informed approval cards.

R5.1/R5.4: the approval frame passes through file_path/diff/command/goal/steps
when the publisher supplied them, and renders the legacy message-only shape
when absent. R5.3/R6.2: the per-op confirm accepts a chat/signal-file answer
(deny wins; approve grants) ONLY when a chat request is in flight; non-chat
runs keep the voice-only gate byte-identical (TTS failure → DENY). R8.3: a
declined rewind rolls back nothing.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.approval_authority import ApprovalAuthority, ApprovalResponse
from core.chat_server import ChatServer, _ChatClient
from inference.dev_agent import DevAgent


def _env(topic, payload, trace_id="t"):
    return {"topic": topic, "payload": payload, "trace_id": trace_id}


# --------------------------------------------------------------------------- #
# Frame passthrough (R5.1 / R5.4 / R6.1)
# --------------------------------------------------------------------------- #

def test_approval_frame_passes_through_extras():
    f = ChatServer._to_frame(_env("dag.approval_requested", {
        "message": "Approve writing file f.py?", "destructive": True,
        "request_id": "r" * 32, "action_digest": "d" * 64,
        "file_path": "f.py", "diff": "--- a/f.py\n+++ b/f.py\n+x\n",
    }))
    assert f["file_path"] == "f.py" and f["diff"].startswith("---")

    g = ChatServer._to_frame(_env("dag.approval_requested", {
        "message": "Approve all?", "destructive": True,
        "request_id": "r" * 32, "action_digest": "d" * 64,
        "goal": "do it", "steps": [{"n": 1, "action": "WRITE_FILE", "args": "f.py"}],
    }))
    assert g["goal"] == "do it" and g["steps"][0]["action"] == "WRITE_FILE"


def test_approval_frame_without_correlation_is_rejected():
    assert ChatServer._to_frame(_env(
        "dag.approval_requested",
        {"message": "Approve all?", "destructive": False},
    )) is None


# --------------------------------------------------------------------------- #
# Per-op confirm: chat/signal-file responder (R5.3, fail-safe preserved)
# --------------------------------------------------------------------------- #

class _Bus:
    def __init__(self):
        self.events = []

    async def publish(self, topic, payload, source="", trace_id=""):
        self.events.append((topic, payload))


@pytest.fixture()
def chat_agent(tmp_path, monkeypatch):
    """DevAgent with a live chat trace, home dir redirected, TTS stubbed."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    import tts.polly_stream as tts_mod
    fake_tts = MagicMock()
    fake_tts.speak_sync = MagicMock()
    monkeypatch.setattr(tts_mod, "get_client", lambda: fake_tts)
    agent = DevAgent(router=MagicMock())
    agent.set_event_bus(_Bus())
    agent._active_trace_id = "T1"
    return agent


def _answer_later(tmp_path, decision, delay=0.2):
    async def _write():
        await asyncio.sleep(delay)
        authority = ApprovalAuthority(tmp_path / ".claude" / "approval")
        deadline = asyncio.get_running_loop().time() + 2.0
        requests = ()
        while asyncio.get_running_loop().time() < deadline:
            requests = authority.active_pending_requests()
            if len(requests) == 1:
                break
            await asyncio.sleep(0.02)
        assert len(requests) == 1
        request = requests[0]
        authority.write_response(ApprovalResponse(
            request_id=request.request_id,
            action_digest=request.action_digest,
            decision=decision,
            responded_at=__import__("time").time(),
        ), require_open_window=True)
    return asyncio.ensure_future(_write())


async def test_chat_click_approves_per_op_confirm(tmp_path, chat_agent):
    writer = _answer_later(tmp_path, "approve")
    ok = await chat_agent._confirm_destructive_op(
        "Approve writing file f.py?", card={"file_path": "f.py", "diff": "+x"})
    await writer
    assert ok is True
    # The card was published with its extras (R5.1).
    topics = dict(chat_agent._event_bus.events)
    card = topics["dag.approval_requested"]
    assert card["file_path"] == "f.py" and card["diff"] == "+x" and card["destructive"]


async def test_chat_click_deny_blocks(tmp_path, chat_agent):
    writer = _answer_later(tmp_path, "deny")
    ok = await chat_agent._confirm_destructive_op("Approve running command: rm?",
                                                  card={"command": "rm -rf x"})
    await writer
    assert ok is False


async def test_non_chat_tts_failure_still_denies(tmp_path, monkeypatch):
    """Voice-only runs are byte-identical: no chat trace → TTS failure DENYs
    without any signal-file window."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    import tts.polly_stream as tts_mod
    monkeypatch.setattr(tts_mod, "get_client",
                        MagicMock(side_effect=RuntimeError("no tts")))
    agent = DevAgent(router=MagicMock())          # no event bus, no trace
    ok = await agent._confirm_destructive_op("Approve?")
    assert ok is False
    assert not (tmp_path / ".claude" / "approval" / "pending").exists()


async def test_unverified_per_op_close_denies_without_pc_mic(
    chat_agent, monkeypatch
):
    import sounddevice as sd

    chat_agent._approval_authority.publish_pending_request = MagicMock(
        side_effect=OSError("pending unavailable")
    )
    chat_agent._approval_authority.close_response_window = MagicMock(
        side_effect=OSError("close unavailable")
    )
    recorder = MagicMock(side_effect=AssertionError("PC mic must not start"))
    monkeypatch.setattr(sd, "rec", recorder)
    assert await chat_agent._confirm_destructive_op("Approve?") is False
    recorder.assert_not_called()


async def test_chat_tts_failure_without_answer_denies(tmp_path, chat_agent, monkeypatch):
    """Chat card shown but TTS down and nobody clicks → fail-safe DENY without
    falling through to an uninformed mic capture."""
    import tts.polly_stream as tts_mod
    monkeypatch.setattr(tts_mod, "get_client",
                        MagicMock(side_effect=RuntimeError("no tts")))
    chat_agent._chat_confirm_window_s = 0.3   # shrink the window for the test
    ok = await asyncio.wait_for(
        chat_agent._confirm_destructive_op("Approve?", card=None), timeout=5.0)
    assert ok is False


# --------------------------------------------------------------------------- #
# Rewind (R8.3): declined confirm rolls back nothing
# --------------------------------------------------------------------------- #

class _Row(dict):
    def __getitem__(self, k):
        return dict.__getitem__(self, k)


async def test_rewind_deny_rolls_back_nothing():
    agent = DevAgent(router=MagicMock())
    db = MagicMock()
    db.available = True
    cur = MagicMock()
    cur.fetchone = AsyncMock(return_value=_Row(id=7, goal="the goal"))
    db._conn.execute = AsyncMock(return_value=cur)
    db.sagas.get_checkpoint_compensations = AsyncMock(return_value=[
        {"compensation_args": '{"path": "f.py"}'}])
    db.misc.promote_checkpoints_to_pending = AsyncMock()
    agent._db = MagicMock(return_value=db)
    agent._confirm_destructive_op = AsyncMock(return_value=False)

    ok = await agent.revert_last_run(trace_id="T9")
    assert ok is False
    db.misc.promote_checkpoints_to_pending.assert_not_awaited()
    # The chat trace was adopted so the confirm card lands on the right socket.
    assert agent._active_trace_id == "T9"
    # The confirm saw the file list (card context).
    card = agent._confirm_destructive_op.call_args.kwargs.get("card")
    assert card and "f.py" in card["command"]
    approval = agent._confirm_destructive_op.await_args.kwargs
    assert approval["approval_action"] == "REWIND_RUN"
    assert approval["security_args"] == {"run_id": 7, "files": ["f.py"]}
    assert approval["content"] == "the goal"


async def test_chat_server_rewind_flow():
    cs = ChatServer()
    coordinator = MagicMock()
    coordinator.revert_last_dev_run = AsyncMock(return_value=True)
    cs.set_coordinator(coordinator)
    client = _ChatClient(ws=MagicMock())

    cs._start_rewind(client)
    await asyncio.gather(*cs._requests.values())

    frames = []
    while not client._q.empty():
        frames.append(client._q.get_nowait())
    assert frames[0]["type"] == "accepted"
    tid = frames[0]["trace_id"]
    coordinator.revert_last_dev_run.assert_awaited_once_with(trace_id=tid)
    final = [f for f in frames if f["type"] == "final"]
    assert final and "Rollback started" in final[0]["result"]["response"]
