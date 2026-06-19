"""In-chat approval: a dag.approval_requested event becomes an approval frame,
and a yes/no click writes the shared ~/.claude/approval/response signal file in
the vocabulary DevAgent classifies (reusing the existing protocol — no new path).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core import chat_server as cs_mod
from core.chat_server import ChatServer
from core.approval_keywords import classify_confirmation


def test_approval_event_becomes_card_frame():
    frame = ChatServer._to_frame({
        "topic": "dag.approval_requested",
        "payload": {"message": "I'll run 2 steps: WRITE_FILE, RUN_TERMINAL. Approve all?",
                    "destructive": True},
        "trace_id": "T1",
    })
    assert frame["type"] == "approval"
    assert frame["destructive"] is True
    assert "Approve all?" in frame["message"]


async def test_approve_click_writes_yes(tmp_path, monkeypatch):
    monkeypatch.setattr(cs_mod, "_APPROVAL_DIR", tmp_path / "approval")
    cs = ChatServer()
    await cs._write_approval(True)
    response = (tmp_path / "approval" / "response").read_text(encoding="utf-8")
    assert response == "yes"
    # DevAgent reads this file and runs it through classify_confirmation.
    assert classify_confirmation(response) == "approve"


async def test_deny_click_writes_no(tmp_path, monkeypatch):
    monkeypatch.setattr(cs_mod, "_APPROVAL_DIR", tmp_path / "approval")
    cs = ChatServer()
    await cs._write_approval(False)
    response = (tmp_path / "approval" / "response").read_text(encoding="utf-8")
    assert response == "no"
    assert classify_confirmation(response) == "deny"
