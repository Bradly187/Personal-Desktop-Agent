"""source="chat" must flow through the full gating path, never bypass it.

The chat UI mints Command(source="chat"). The behavioral guarantee the chat
feature relies on: a chat message is gated exactly like a typed-but-trustworthy
voice command — full 4-gate (incl. Gate-0 privacy), never the touch/multimodal
bypass, and never the voice_local Gate-1 skip. Command defaults also clear the
Gate-1 confidence thresholds (perfect logprob / gesture confidence), since the
text was typed, not transcribed.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.command_executor import Command
from core import hybrid_coordinator as hc


def test_chat_source_not_bypassed():
    assert "chat" not in hc._BYPASS_SOURCES
    assert "chat" not in hc._SKIP_GATE1_SOURCES


def test_chat_command_defaults_clear_gate1():
    cmd = Command(text="explain the fusion engine", action="CLARIFY", source="chat")
    # Defaults: a typed message is maximally confident, so Gate-1 can't reject it.
    assert cmd.whisper_logprob == 0.0
    assert cmd.gesture_confidence == 1.0
    # trace_id is carried for live-DAG correlation (set by the chat server).
    assert cmd.trace_id == ""
    cmd2 = Command(text="x", action="CLARIFY", source="chat", trace_id="abc123")
    assert cmd2.trace_id == "abc123"
