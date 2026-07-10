"""Tests for DevAgent approval gates (Fix #2 — fail-safe to DENY, hardened
2026-06-09: DENY now aborts the whole plan, WRITE_FILE/RUN_TERMINAL are
per-op gated, and a replan that injects unapproved destructive verbs revokes
the blanket authorization).

Destructive plans / ops must NOT auto-approve on silence, ambiguity, or
TTS/mic hardware failure — only an explicit spoken "yes" (or a prior whole-plan
authorization) proceeds. Read-only plans keep their convenient auto-run, but
auto-run does NOT grant blanket authorization.

Covered:
- _approve_plan_upfront: destructive plan + TTS-unavailable → "denied" (no GoalSession)
- _approve_plan_upfront: read-only plan + TTS-unavailable → "auto" (GoalSession written)
- plan_and_run: "denied" verdict aborts — zero steps execute
- plan_and_run: "auto" verdict runs WITHOUT _plan_authorized
- plan_and_run: replan injecting an unapproved destructive verb revokes authorization
- _execute_step: WRITE_FILE / RUN_TERMINAL are per-op gated when not authorized
- _confirm_destructive_op: _plan_authorized short-circuit → APPROVE
- _confirm_destructive_op: TTS/mic hardware failure → DENY
- _confirm_destructive_op: silence → DENY
- _confirm_destructive_op: explicit "yes" → APPROVE; "no" → DENY; ambiguous → DENY
"""

from __future__ import annotations
import inference.step_executor as se

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
            verdict = await agent._approve_plan_upfront("ship the feature", steps)
        assert verdict == "denied"
        assert GoalSessionStore.get_active() is None   # no grant written

    @pytest.mark.asyncio
    async def test_readonly_plan_tts_unavailable_auto_runs(self):
        agent = _agent()
        steps = [AgentStep(action="READ_FILE", args="x.py"),
                 AgentStep(action="EXPLAIN", body="here's what it does")]
        with _fake_tts(raises=True):
            verdict = await agent._approve_plan_upfront("explain the module", steps)
        assert verdict == "auto"
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
        assert await se.confirm_destructive_op(agent, "git commit?") is True

    @pytest.mark.asyncio
    async def test_tts_failure_denies(self):
        agent = _agent()
        with _fake_tts(raises=True):
            assert await se.confirm_destructive_op(agent, "git commit?") is False

    @pytest.mark.asyncio
    async def test_silence_denies(self):
        agent = _agent()
        with _fake_tts(), _fake_mic(loud=False):
            assert await se.confirm_destructive_op(agent, "git commit?") is False

    @pytest.mark.asyncio
    async def test_explicit_yes_approves(self):
        agent = _agent()
        _seed_whisper(agent, "yes go ahead")
        with _fake_tts(), _fake_mic(loud=True):
            assert await se.confirm_destructive_op(agent, "git commit?") is True

    @pytest.mark.asyncio
    async def test_explicit_no_denies(self):
        agent = _agent()
        _seed_whisper(agent, "no stop")
        with _fake_tts(), _fake_mic(loud=True):
            assert await se.confirm_destructive_op(agent, "git commit?") is False

    @pytest.mark.asyncio
    async def test_ambiguous_denies(self):
        agent = _agent()
        _seed_whisper(agent, "banana bread recipe")
        with _fake_tts(), _fake_mic(loud=True):
            assert await se.confirm_destructive_op(agent, "git commit?") is False


# ---------------------------------------------------------------------------
# plan_and_run — DENY aborts; auto runs unauthorized; replan revokes
# ---------------------------------------------------------------------------

class _RR:
    """Minimal RouterResult stand-in (ok/text/model/error)."""
    def __init__(self, text: str, ok: bool = True, model: str = "test-model"):
        self.text = text
        self.ok = ok
        self.model = model
        self.error = None if ok else "err"


def _plan_agent(*router_responses, verdict: str) -> DevAgent:
    """DevAgent with a canned plan + approval verdict, side-channels stubbed."""
    from unittest.mock import AsyncMock
    router = MagicMock()
    router.infer = AsyncMock(side_effect=list(router_responses))
    agent = DevAgent(router=router)
    agent._approve_plan_upfront = AsyncMock(return_value=verdict)
    agent._rag_context = AsyncMock(return_value="")
    agent._git_context = AsyncMock(return_value="")
    agent._format_context = lambda: ""
    agent._reflect = AsyncMock(return_value="summary")
    agent._persist_run = AsyncMock()
    agent._speak_plan_completion = AsyncMock()
    return agent


class TestPlanDenyAborts:
    @pytest.mark.asyncio
    async def test_denied_verdict_executes_zero_steps(self):
        agent = _plan_agent(
            _RR("Step 1: [WRITE_FILE x.py]\nStep 2: [RUN_TERMINAL pytest]"),
            verdict="denied",
        )
        ran: list[str] = []

        async def exec_step(step):
            ran.append(step.action)
            return "ok"

        agent._execute_step = exec_step
        result = await agent.plan_and_run("destructive goal")

        assert ran == []                       # nothing executed
        assert result.success is False
        assert result.error == "Plan rejected by user"
        assert result.steps == []

    @pytest.mark.asyncio
    async def test_legacy_false_still_aborts(self):
        # Backward compat: a stubbed bool False must behave like "denied".
        agent = _plan_agent(
            _RR("Step 1: [WRITE_FILE x.py]"), verdict=False,
        )
        ran: list[str] = []

        async def exec_step(step):
            ran.append(step.action)
            return "ok"

        agent._execute_step = exec_step
        result = await agent.plan_and_run("goal")
        assert ran == []
        assert result.success is False

    @pytest.mark.asyncio
    async def test_auto_verdict_runs_without_blanket_authorization(self):
        agent = _plan_agent(
            _RR("Step 1: [READ_FILE x.py]"), verdict="auto",
        )
        seen_auth: list[bool] = []

        async def exec_step(step):
            seen_auth.append(agent._plan_authorized)
            return "ok"

        agent._execute_step = exec_step
        result = await agent.plan_and_run("read-only goal")
        assert result.success is True
        assert seen_auth == [False]            # ran, but not blanket-authorized

    @pytest.mark.asyncio
    async def test_approved_verdict_sets_blanket_authorization(self):
        agent = _plan_agent(
            _RR("Step 1: [WRITE_FILE x.py]"), verdict="approved",
        )
        seen_auth: list[bool] = []

        async def exec_step(step):
            seen_auth.append(agent._plan_authorized)
            return "ok"

        agent._execute_step = exec_step
        result = await agent.plan_and_run("write goal")
        assert result.success is True
        assert seen_auth == [True]

    @pytest.mark.asyncio
    async def test_replan_injecting_destructive_verb_revokes_authorization(self):
        # Approved plan contains only WRITE_FILE; the recovery replan injects
        # RUN_TERMINAL — blanket authorization must be revoked for the remainder.
        agent = _plan_agent(
            _RR("Step 1: [WRITE_FILE a FAIL]"),        # original (approved) plan
            _RR("Step 1: [RUN_TERMINAL rm -rf x]"),    # replan injects new verb
            verdict="approved",
        )
        observed: list[tuple[str, bool]] = []

        async def exec_step(step):
            observed.append((step.action, agent._plan_authorized))
            if "FAIL" in (step.args or ""):
                raise RuntimeError("boom")
            return "ok"

        agent._execute_step = exec_step
        await agent.plan_and_run("goal")

        assert observed[0] == ("WRITE_FILE", True)     # approved plan verb
        assert observed[1] == ("RUN_TERMINAL", False)  # injected verb → revoked


class TestPerOpGates:
    @pytest.mark.asyncio
    async def test_write_file_denied_does_not_write(self, tmp_path):
        from unittest.mock import AsyncMock
        agent = _agent()
        agent._plan_authorized = False
        import inference.step_executor as se
        agent._confirm_destructive_op = se.confirm_destructive_op = AsyncMock(return_value=False)
        target = tmp_path / "out.txt"
        step = AgentStep(action="WRITE_FILE", args=str(target), body="data")
        result = await agent._execute_step(step)
        assert result == "WRITE_FILE cancelled by user"
        assert not target.exists()

    @pytest.mark.asyncio
    async def test_write_file_approved_writes(self, tmp_path):
        from unittest.mock import AsyncMock
        agent = _agent()
        import inference.step_executor as se
        agent._confirm_destructive_op = se.confirm_destructive_op = AsyncMock(return_value=True)
        target = tmp_path / "out.txt"
        step = AgentStep(action="WRITE_FILE", args=str(target), body="data")
        await agent._execute_step(step)
        assert target.read_text(encoding="utf-8") == "data"

    @pytest.mark.asyncio
    async def test_run_terminal_denied_does_not_run(self):
        from unittest.mock import AsyncMock
        agent = _agent()
        agent._plan_authorized = False
        import inference.step_executor as se
        agent._confirm_destructive_op = se.confirm_destructive_op = AsyncMock(return_value=False)
        step = AgentStep(action="RUN_TERMINAL", args="echo pwned")
        result = await agent._execute_step(step)
        assert result == "RUN_TERMINAL cancelled by user"
