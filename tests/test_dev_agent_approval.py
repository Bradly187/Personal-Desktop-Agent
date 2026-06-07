"""Tests for DevAgent approval gates (Fix #2 — fail-safe to DENY).

Destructive plans / ops must NOT auto-approve on silence, ambiguity, or
TTS/mic hardware failure — only an explicit spoken "yes" (or a prior whole-plan
authorization) proceeds. Read-only plans keep their convenient auto-approve.

Covered:
- _approve_plan_upfront: destructive plan + TTS-unavailable → DENY (no GoalSession)
- _approve_plan_upfront: read-only plan + TTS-unavailable → APPROVE (GoalSession written)
- _confirm_destructive_op: _plan_authorized short-circuit → APPROVE
- _confirm_destructive_op: TTS/mic hardware failure → DENY
- _confirm_destructive_op: silence → DENY
- _confirm_destructive_op: explicit "yes" → APPROVE; "no" → DENY; ambiguous → DENY
"""

from __future__ import annotations

import sys
import types
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from inference.dev_agent import DevAgent, AgentStep
from core.goal_session import GoalSessionStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _agent() -> DevAgent:
    return DevAgent(router=MagicMock())


@contextmanager
def _fake_tts(raises: bool = False):
    """Inject a fake tts.polly_stream module so no real TTS/audio stack loads."""
    mod = types.ModuleType("tts.polly_stream")

    def get_client(*a, **kw):
        if raises:
            raise RuntimeError("TTS unavailable (test)")
        client = MagicMock()
        client.speak_sync = MagicMock(return_value=None)
        return client

    mod.get_client = get_client
    saved = sys.modules.get("tts.polly_stream")
    sys.modules["tts.polly_stream"] = mod
    try:
        yield
    finally:
        if saved is not None:
            sys.modules["tts.polly_stream"] = saved
        else:
            sys.modules.pop("tts.polly_stream", None)


@contextmanager
def _fake_mic(loud: bool, transcript: str = ""):
    """Inject a fake sounddevice; pre-seed a fake whisper that returns `transcript`."""
    sd = types.ModuleType("sounddevice")
    amp = 0.5 if loud else 0.0

    def rec(frames, **kw):
        return np.full((frames, 1), amp, dtype="float32")

    sd.rec = rec
    sd.wait = lambda: None
    saved = sys.modules.get("sounddevice")
    sys.modules["sounddevice"] = sd
    try:
        yield
    finally:
        if saved is not None:
            sys.modules["sounddevice"] = saved
        else:
            sys.modules.pop("sounddevice", None)


def _seed_whisper(agent: DevAgent, transcript: str) -> None:
    """Pre-seed _confirm_whisper so the real faster_whisper import is skipped."""
    seg = MagicMock()
    seg.text = transcript
    model = MagicMock()
    model.transcribe.return_value = ([seg], MagicMock())
    agent._confirm_whisper = model


@pytest.fixture(autouse=True)
def _isolated_goal_session(tmp_path, monkeypatch):
    """Point GoalSessionStore at a temp file so tests never touch the real ~/.claude."""
    monkeypatch.setattr(GoalSessionStore, "PATH", tmp_path / "goal_session.json")
    GoalSessionStore.cancel()
    yield
    GoalSessionStore.cancel()


# ---------------------------------------------------------------------------
# _approve_plan_upfront — destructive vs read-only fail-safe fork
# ---------------------------------------------------------------------------

class TestApprovePlanUpfront:
    @pytest.mark.asyncio
    async def test_destructive_plan_tts_unavailable_denies(self):
        agent = _agent()
        steps = [AgentStep(action="WRITE_FILE", args="x.py"),
                 AgentStep(action="RUN_TERMINAL", args="pytest")]
        with _fake_tts(raises=True):
            approved = await agent._approve_plan_upfront("ship the feature", steps)
        assert approved is False
        assert GoalSessionStore.get_active() is None   # no grant written

    @pytest.mark.asyncio
    async def test_readonly_plan_tts_unavailable_auto_approves(self):
        agent = _agent()
        steps = [AgentStep(action="READ_FILE", args="x.py"),
                 AgentStep(action="EXPLAIN", body="here's what it does")]
        with _fake_tts(raises=True):
            approved = await agent._approve_plan_upfront("explain the module", steps)
        assert approved is True
        assert GoalSessionStore.get_active() is not None   # convenience grant


# ---------------------------------------------------------------------------
# _confirm_destructive_op — always fail-safe to DENY
# ---------------------------------------------------------------------------

class TestConfirmDestructiveOp:
    @pytest.mark.asyncio
    async def test_plan_authorized_short_circuits_to_approve(self):
        agent = _agent()
        agent._plan_authorized = True
        # No TTS/mic touched — returns immediately.
        assert await agent._confirm_destructive_op("git commit?") is True

    @pytest.mark.asyncio
    async def test_tts_failure_denies(self):
        agent = _agent()
        with _fake_tts(raises=True):
            assert await agent._confirm_destructive_op("git commit?") is False

    @pytest.mark.asyncio
    async def test_silence_denies(self):
        agent = _agent()
        with _fake_tts(), _fake_mic(loud=False):
            assert await agent._confirm_destructive_op("git commit?") is False

    @pytest.mark.asyncio
    async def test_explicit_yes_approves(self):
        agent = _agent()
        _seed_whisper(agent, "yes go ahead")
        with _fake_tts(), _fake_mic(loud=True):
            assert await agent._confirm_destructive_op("git commit?") is True

    @pytest.mark.asyncio
    async def test_explicit_no_denies(self):
        agent = _agent()
        _seed_whisper(agent, "no stop")
        with _fake_tts(), _fake_mic(loud=True):
            assert await agent._confirm_destructive_op("git commit?") is False

    @pytest.mark.asyncio
    async def test_ambiguous_denies(self):
        agent = _agent()
        _seed_whisper(agent, "banana bread recipe")
        with _fake_tts(), _fake_mic(loud=True):
            assert await agent._confirm_destructive_op("git commit?") is False
