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
import asyncio
import time
import inference.step_executor as se

import sys
import types
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from inference.dev_agent import DevAgent, AgentStep
from core.approval_authority import (
    ApprovalResponse, plan_digest, step_action_digest,
)
from core.goal_session import GoalSessionStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _agent() -> DevAgent:
    return DevAgent(router=MagicMock())


async def _answer_active_request(agent: DevAgent, decision: str = "approve") -> None:
    deadline = asyncio.get_running_loop().time() + 2.0
    requests = ()
    while asyncio.get_running_loop().time() < deadline:
        requests = agent._approval_authority.active_pending_requests()
        if len(requests) == 1:
            break
        await asyncio.sleep(0.02)
    assert len(requests) == 1
    request = requests[0]
    agent._approval_authority.write_response(ApprovalResponse(
        request_id=request.request_id,
        action_digest=request.action_digest,
        decision=decision,
        responded_at=time.time(),
    ))


def _issue_plan_grant(agent: DevAgent, digests: tuple[str, ...], *, goal: str, trace: str):
    request = agent._approval_authority.create_request(
        trace_id=trace,
        action_digest=plan_digest(digests, goal=goal, trace_id=trace),
        description="plan",
    )
    agent._approval_authority.write_response(ApprovalResponse(
        request_id=request.request_id,
        action_digest=request.action_digest,
        decision="approve",
        responded_at=request.created_at,
    ))
    grant = agent._approval_authority.consume_response(
        request, approved_digests=digests, plan_goal=goal
    )
    assert grant is not None
    return grant


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
    """Keep goal sessions and structured approval files out of the real home."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
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
    async def test_bare_legacy_yes_cannot_authorize_active_plan(self):
        agent = _agent()
        root = agent._approval_authority.root
        root.mkdir(parents=True, exist_ok=True)
        (root / "response").write_text("yes", encoding="utf-8")
        steps = [AgentStep(action="WRITE_FILE", args="x.py", body="x")]
        with _fake_tts(raises=True):
            assert await agent._approve_plan_upfront("write", steps) == "denied"
        assert agent._approval_grant is None

    @pytest.mark.asyncio
    async def test_unverified_remote_close_denies_without_pc_mic(self, monkeypatch):
        import sounddevice as sd

        agent = _agent()
        agent._approval_authority.publish_pending_request = MagicMock(
            side_effect=OSError("pending unavailable")
        )
        agent._approval_authority.close_response_window = MagicMock(
            side_effect=OSError("close unavailable")
        )
        recorder = MagicMock(side_effect=AssertionError("PC mic must not start"))
        monkeypatch.setattr(sd, "rec", recorder)
        with _fake_tts():
            verdict = await agent._approve_plan_upfront(
                "write", [AgentStep(action="WRITE_FILE", args="x.py", body="x")]
            )
        assert verdict == "denied"
        recorder.assert_not_called()

    @pytest.mark.asyncio
    async def test_destructive_plan_authority_io_failure_denies(self):
        agent = _agent()
        agent._approval_authority.create_request = MagicMock(
            side_effect=OSError("approval store unavailable")
        )
        steps = [AgentStep(action="WRITE_FILE", args="x.py", body="x")]
        assert await agent._approve_plan_upfront("write", steps) == "denied"
        assert agent._approval_grant is None

    @pytest.mark.asyncio
    async def test_readonly_plan_tts_unavailable_auto_runs(self):
        agent = _agent()
        steps = [AgentStep(action="READ_FILE", args="x.py"),
                 AgentStep(action="EXPLAIN", body="here's what it does")]
        with _fake_tts(raises=True):
            verdict = await agent._approve_plan_upfront("explain the module", steps)
        assert verdict == "auto"
        assert GoalSessionStore.get_active() is not None   # convenience grant

    @pytest.mark.asyncio
    async def test_one_upfront_response_grants_all_exact_ordinary_steps(
        self, tmp_path, monkeypatch
    ):
        agent = _agent()
        agent._publish_live = AsyncMock()
        agent._active_trace_id = "trace-plan"
        goal = "write both files"
        steps = [
            AgentStep(action="WRITE_FILE", args=str(tmp_path / "a.py"), body="a", step_num=1),
            AgentStep(action="WRITE_FILE", args=str(tmp_path / "b.py"), body="b", step_num=2),
        ]

        writer = asyncio.create_task(_answer_active_request(agent))
        with _fake_tts():
            verdict = await agent._approve_plan_upfront(goal, steps)
        await writer

        assert verdict == "approved"
        assert agent._approval_grant is not None
        assert len(agent._approval_grant.approved_digests) == 2
        assert len(list(agent._approval_authority.consumed_dir.glob("*.json"))) == 1
        approval_payload = next(
            call.args[1]
            for call in agent._publish_live.await_args_list
            if call.args[0] == "dag.approval_requested"
        )
        assert approval_payload["request_id"] == agent._approval_grant.request_id
        assert approval_payload["action_digest"]

        agent._current_goal = goal
        gate = AsyncMock(side_effect=AssertionError("unexpected per-op prompt"))
        monkeypatch.setattr(se, "confirm_destructive_op_locked", gate)
        for step in steps:
            assert await se.confirm_destructive_op(
                agent,
                "Approve write?",
                step=step,
                canonical_path=step.args,
                content=step.body,
            )
        gate.assert_not_awaited()


# ---------------------------------------------------------------------------
# _confirm_destructive_op — always fail-safe to DENY
# ---------------------------------------------------------------------------

class TestConfirmDestructiveOp:
    @pytest.mark.asyncio
    async def test_exact_plan_grant_short_circuits_to_approve(self, tmp_path):
        agent = _agent()
        agent._current_goal = "commit change"
        agent._active_trace_id = "trace-1"
        step = AgentStep(action="GIT_COMMIT", args="message", step_num=1)
        digest = step_action_digest(
            step,
            trace_id="trace-1",
            goal="commit change",
            canonical_cwd=str(tmp_path),
            approval_context="plan",
        )
        agent._approval_grant = _issue_plan_grant(
            agent, (digest,), goal="commit change", trace="trace-1"
        )
        agent._plan_authorized = True  # status only; digest is the authority
        assert await se.confirm_destructive_op(
            agent,
            "git commit?",
            step=step,
            canonical_cwd=str(tmp_path),
        ) is True

    @pytest.mark.asyncio
    async def test_whole_plan_grant_covers_each_unchanged_step_without_prompts(
        self, tmp_path, monkeypatch
    ):
        agent = _agent()
        agent._current_goal = "two writes"
        agent._active_trace_id = "trace-plan"
        steps = [
            AgentStep(action="WRITE_FILE", args=str(tmp_path / "a.py"), body="a", step_num=1),
            AgentStep(action="WRITE_FILE", args=str(tmp_path / "b.py"), body="b", step_num=2),
        ]
        digests = tuple(
            step_action_digest(
                step,
                trace_id="trace-plan",
                goal="two writes",
                canonical_path=step.args,
                content=step.body,
                approval_context="plan",
            )
            for step in steps
        )
        agent._approval_grant = _issue_plan_grant(
            agent, digests, goal="two writes", trace="trace-plan"
        )
        gate = AsyncMock(side_effect=AssertionError("unexpected per-op prompt"))
        monkeypatch.setattr(se, "confirm_destructive_op_locked", gate)

        for step in steps:
            assert await se.confirm_destructive_op(
                agent,
                "approve write?",
                step=step,
                canonical_path=step.args,
                content=step.body,
            )
        gate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_authority_io_failure_denies_without_opening_responder(
        self, monkeypatch
    ):
        agent = _agent()
        agent._approval_authority.create_request = MagicMock(
            side_effect=OSError("approval store unavailable")
        )
        gate = AsyncMock(side_effect=AssertionError("responder must not open"))
        monkeypatch.setattr(se, "confirm_destructive_op_locked", gate)
        assert await se.confirm_destructive_op(agent, "Approve?") is False
        gate.assert_not_awaited()

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
    async def test_approved_string_without_typed_grant_is_not_authority(self):
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
        assert seen_auth == [False]

    @pytest.mark.asyncio
    async def test_replan_changed_same_verb_requires_per_op_approval(
        self, tmp_path, monkeypatch
    ):
        agent = _agent()
        agent._critic = None
        agent._critic_enabled = False
        agent._current_goal = "write config"
        agent._active_trace_id = "trace-1"
        original = AgentStep(
            action="WRITE_FILE", args=str(tmp_path / "a.py"),
            body="old", step_num=1,
        )
        original_digest = step_action_digest(
            original,
            trace_id="trace-1",
            goal="write config",
            canonical_path=str(tmp_path / "a.py"),
            content="old",
            approval_context="plan",
        )
        agent._approval_grant = _issue_plan_grant(
            agent, (original_digest,), goal="write config", trace="trace-1"
        )
        agent._plan_authorized = True
        changed = AgentStep(
            action="WRITE_FILE", args=str(tmp_path / "b.py"),
            body="old", step_num=1,
        )
        monkeypatch.setattr(
            "inference.executors.plan_executor._replan",
            AsyncMock(return_value=[changed]),
        )
        gate = AsyncMock(return_value=False)
        monkeypatch.setattr(se, "confirm_destructive_op_locked", gate)

        replanned = await agent._try_replan("write config", [], [original])
        result = await agent._execute_step(replanned[0])

        assert result == "WRITE_FILE cancelled by user"
        gate.assert_awaited_once()
        assert not (tmp_path / "b.py").exists()


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


@pytest.mark.asyncio
async def test_target_drift_changes_candidate_and_requires_new_per_op_approval(
    tmp_path, monkeypatch
):
    agent = _agent()
    agent._critic = None
    agent._critic_enabled = False
    agent._active_trace_id = "trace-drift"
    goal = "append resolved content"
    target = tmp_path / "drift.txt"
    target.write_text("before|", encoding="utf-8")
    step = AgentStep(
        action="WRITE_FILE", args=str(target), body="payload", step_num=1
    )

    def stateful_candidate(path, body, edit_format, applier):
        return Path(path).read_text(encoding="utf-8") + body

    monkeypatch.setattr(se, "apply_edit", stateful_candidate)

    writer = asyncio.create_task(_answer_active_request(agent))
    with _fake_tts():
        assert await agent._approve_plan_upfront(goal, [step]) == "approved"
    await writer

    target.write_text("drifted|", encoding="utf-8")
    agent._current_goal = goal
    gate = AsyncMock(return_value=False)
    monkeypatch.setattr(se, "confirm_destructive_op_locked", gate)

    result = await agent._execute_step(step)

    assert result == "WRITE_FILE cancelled by user"
    assert target.read_text(encoding="utf-8") == "drifted|"
    gate.assert_awaited_once()


def test_plan_reset_revokes_unconsumed_grant_positions():
    agent = _agent()
    a, b = "a" * 64, "b" * 64
    grant = _issue_plan_grant(
        agent, (a, b), goal="partial plan", trace="trace-reset"
    )
    agent._approval_grant = grant

    assert agent._approval_authority.claim_grant(grant, a)
    agent._reset_plan_state()
    assert not agent._approval_authority.claim_grant(grant, b)


@pytest.mark.asyncio
async def test_external_plan_task_cancellation_revokes_live_grant():
    agent = _agent()
    a, b = "a" * 64, "b" * 64
    grant = _issue_plan_grant(
        agent, (a, b), goal="cancelled plan", trace="trace-cancel"
    )
    started = asyncio.Event()
    never = asyncio.Event()

    async def blocked_plan(*args, **kwargs):
        agent._approval_grant = grant
        started.set()
        await never.wait()

    agent._plan_and_run_locked = blocked_plan
    task = asyncio.create_task(agent.plan_and_run("cancelled plan"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert agent._approval_grant is None
    assert not agent._approval_authority.claim_grant(grant, a)
