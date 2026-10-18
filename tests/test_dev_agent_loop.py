"""Tests for the closed-loop (ReAct) DevAgent controller (B2).

plan_and_run must: replan after a step failure (bounded), never blindly continue
past a failure (no compounding), respect the replan budget, and break promptly on
cancellation.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.action_lifecycle import ActionIdentity, ActionStatus
from core.command_executor import CommandExecutor
from inference.dev_agent import DevAgent
from inference.sandbox import WorkTrust
from core.goal_session import GoalSessionStore


class _RR:
    """Minimal RouterResult stand-in (ok/text/model/error)."""
    def __init__(self, text: str, ok: bool = True, model: str = "test-model"):
        self.text = text
        self.ok = ok
        self.model = model
        self.error = None if ok else "err"


@pytest.fixture(autouse=True)
def _isolated_goal_session(tmp_path, monkeypatch):
    monkeypatch.setattr(GoalSessionStore, "PATH", tmp_path / "goal_session.json")
    GoalSessionStore.cancel()
    yield
    GoalSessionStore.cancel()


def _agent_with_plan(*router_responses) -> DevAgent:
    """DevAgent whose router.infer yields the given responses in order, with all
    side-channels (RAG, git, approval, reflect, persist, TTS) stubbed out."""
    router = MagicMock()
    router.infer = AsyncMock(side_effect=list(router_responses))
    agent = DevAgent(router=router)
    agent._approve_plan_upfront = AsyncMock(return_value=True)
    agent._rag_context = AsyncMock(return_value="")
    agent._git_context = AsyncMock(return_value="")
    agent._format_context = lambda: ""
    agent._reflect = AsyncMock(return_value="summary")
    agent._persist_run = AsyncMock()
    agent._speak_plan_completion = AsyncMock()
    return agent


@pytest.mark.asyncio
async def test_failure_triggers_one_replan_and_recovers():
    agent = _agent_with_plan(
        _RR("Step 1: [WRITE_FILE a]\nStep 2: [WRITE_FILE b FAIL]"),  # initial plan
        _RR("Step 1: [WRITE_FILE c]"),                                # recovery replan
    )

    async def exec_step(step):
        if "FAIL" in step.args:
            raise RuntimeError("boom")
        return f"ok:{step.action}"

    agent._execute_step = exec_step
    result = await agent.plan_and_run("do the thing")

    assert result.success is True                       # recovered → success
    assert [s.action for s in result.steps] == ["WRITE_FILE", "WRITE_FILE", "WRITE_FILE"]
    assert [s.success for s in result.steps] == [True, False, True]
    assert agent._router.infer.await_count == 2         # initial plan + 1 replan


@pytest.mark.asyncio
async def test_replan_budget_is_bounded():
    # Every step fails; planner keeps proposing more failing steps.
    agent = _agent_with_plan(
        _RR("Step 1: [WRITE_FILE x FAIL]"),
        _RR("Step 1: [WRITE_FILE y FAIL]"),
        _RR("Step 1: [WRITE_FILE z FAIL]"),
        _RR("Step 1: [WRITE_FILE w FAIL]"),   # should never be requested
    )

    async def exec_step(step):
        raise RuntimeError("always fails")

    agent._execute_step = exec_step
    result = await agent.plan_and_run("impossible goal")

    assert result.success is False
    # initial plan + MAX_REPLANS replans, then halt
    assert agent._router.infer.await_count == 1 + agent.MAX_REPLANS
    assert len(result.steps) == 1 + agent.MAX_REPLANS


@pytest.mark.asyncio
async def test_no_compounding_when_replan_declines():
    # Step 1 fails; planner returns no recovery → must HALT, not run step 2.
    agent = _agent_with_plan(
        _RR("Step 1: [WRITE_FILE a FAIL]\nStep 2: [WRITE_FILE b]"),
        _RR(""),   # empty replan → no recovery
    )
    ran: list[str] = []

    async def exec_step(step):
        ran.append(step.args)
        if "FAIL" in step.args:
            raise RuntimeError("boom")
        return "ok"

    agent._execute_step = exec_step
    result = await agent.plan_and_run("two step goal")

    assert result.success is False
    assert "b" not in ran            # step 2 never ran — no compounding
    assert len(result.steps) == 1


@pytest.mark.asyncio
async def test_cancellation_breaks_promptly():
    agent = _agent_with_plan(
        _RR("Step 1: [WRITE_FILE a]\nStep 2: [WRITE_FILE b]"),
    )
    ran: list[str] = []

    async def exec_step(step):
        ran.append(step.args)
        agent.request_cancel()   # cancel after the first step
        return "ok"

    agent._execute_step = exec_step
    result = await agent.plan_and_run("cancel me")

    assert result.success is False        # cancelled
    assert ran == ["a"]                    # step 2 not executed
    assert agent._router.infer.await_count == 1   # no replan on cancel


@pytest.mark.asyncio
async def test_destructive_unknown_does_not_replan_or_compensate():
    agent = _agent_with_plan(
        _RR("Step 1: [WRITE_FILE a]\nStep 2: [WRITE_FILE b]"),
        _RR("Step 1: [WRITE_FILE replacement]"),
    )
    agent.STEP_TIMEOUT_S = 0.01
    agent.RECONCILIATION_GRACE_S = 0.01
    release = __import__("asyncio").Event()
    ran = []
    agent._halt_and_compensate = AsyncMock()
    agent._run_compensations = AsyncMock()

    async def exec_step(step):
        ran.append(step.args)
        await release.wait()
        return "late success"

    agent._execute_step = exec_step
    result = await agent.plan_and_run("unknown write")

    assert result.success is False
    assert result.error and "outcome remains unknown" in result.error
    assert ran == ["a"]
    assert agent._router.infer.await_count == 1
    agent._halt_and_compensate.assert_not_awaited()
    agent._run_compensations.assert_not_awaited()
    agent._speak_plan_completion.assert_not_awaited()
    assert result.steps[0].success is None
    assert result.steps[0].lifecycle_status.value == "outcome_unknown"

    release.set()
    await __import__("asyncio").sleep(0.02)


@pytest.mark.asyncio
async def test_delegated_command_unknown_interrupts_plan_and_keeps_original_identity(
    monkeypatch,
):
    import core.command_executor as command_module

    agent = _agent_with_plan(
        _RR("Step 1: [CLICK Save]\nStep 2: [WRITE_FILE must-not-run]"),
        _RR("Step 1: [WRITE_FILE replacement]"),
    )
    executor = CommandExecutor()
    executor._tool_timeout_ms = AsyncMock(return_value=10)
    coordinator = MagicMock()
    coordinator._executor = executor
    agent._coordinator = coordinator
    release = threading.Event()
    calls = []

    monkeypatch.setattr(command_module, "_PROXY_URL", None)
    monkeypatch.setattr(command_module, "_RECONCILIATION_GRACE_S", 0.01)
    monkeypatch.setattr(command_module, "_get_action_verifier", lambda: None)

    def dispatch(action, cmd):
        calls.append((action, cmd.action_id, cmd.trace_id, cmd.run_id, cmd.step_num))
        release.wait(2)
        return {"clicked": True}

    monkeypatch.setattr(executor, "_dispatch", dispatch)
    result = await agent.plan_and_run("click then write", trace_id="trace-composed")

    assert result.success is False
    assert result.error and "CLICK outcome remains unknown" in result.error
    assert len(result.steps) == 1
    step = result.steps[0]
    assert step.lifecycle_status is ActionStatus.OUTCOME_UNKNOWN
    assert calls == [("CLICK", step.action_id, "trace-composed", step.run_id, 1)]
    assert agent._router.infer.await_count == 1

    release.set()
    await asyncio.sleep(0.03)
    observed = await agent._action_registry.query(
        ActionIdentity(step.action_id, "CLICK", "trace-composed", step.run_id, 1)
    )
    assert observed is not None and observed.status is ActionStatus.COMPLETED


@pytest.mark.asyncio
async def test_delegated_proxy_unknown_remains_locally_reconcilable(monkeypatch):
    import core.command_executor as command_module

    agent = _agent_with_plan(
        _RR("Step 1: [CLICK Save]\nStep 2: [WRITE_FILE must-not-run]")
    )
    executor = CommandExecutor()
    executor._tool_timeout_ms = AsyncMock(return_value=10)
    coordinator = MagicMock()
    coordinator._executor = executor
    agent._coordinator = coordinator
    release = asyncio.Event()
    result_queries = 0

    monkeypatch.setattr(command_module, "_PROXY_URL", "http://proxy.test")
    monkeypatch.setattr(command_module, "_PROXY_TOKEN", "t" * 64)
    monkeypatch.setattr(command_module, "_PROXY_INSTANCE_ID", "i" * 32)
    monkeypatch.setattr(command_module, "_RECONCILIATION_GRACE_S", 0.01)

    async def signed_post(_session, path, payload, *, timeout_s):
        nonlocal result_queries
        if path == "/execute":
            return 200, {
                **payload,
                "status": "outcome_unknown",
                "lifecycle_status": "outcome_unknown",
                "error": "receiver still executing",
            }
        result_queries += 1
        await release.wait()
        return 200, {
            **payload,
            "status": "ok",
            "lifecycle_status": "completed",
            "result": {"clicked": True},
        }

    monkeypatch.setattr(executor, "_signed_proxy_post", signed_post)
    result = await agent.plan_and_run("proxy click then write", trace_id="proxy-trace")

    assert result.success is False
    assert len(result.steps) == 1
    step = result.steps[0]
    assert step.lifecycle_status is ActionStatus.OUTCOME_UNKNOWN
    assert agent._router.infer.await_count == 1

    release.set()
    await asyncio.sleep(0.03)
    observed = await agent._action_registry.query(
        ActionIdentity(step.action_id, "CLICK", "proxy-trace", step.run_id, 1)
    )
    assert result_queries >= 1
    assert observed is not None and observed.status is ActionStatus.COMPLETED
    assert observed.result == {"clicked": True}


@pytest.mark.asyncio
async def test_initial_rag_context_taints_later_terminal_work():
    agent = _agent_with_plan(_RR("Step 1: [RUN_TERMINAL pytest -q]"))
    agent._rag_context = AsyncMock(return_value="retrieved code context")
    observed = []

    async def exec_step(step):
        observed.append(agent._active_work_trust)
        return "ok"

    agent._execute_step = exec_step
    result = await agent.plan_and_run(
        "test the change", work_trust=WorkTrust.TRUSTED_MANUAL
    )
    assert result.success is True
    assert observed == [WorkTrust.AUTONOMOUS_OR_UNTRUSTED]


@pytest.mark.asyncio
async def test_attachment_context_taints_initial_terminal_plan():
    agent = _agent_with_plan(_RR("Step 1: [RUN_TERMINAL pytest -q]"))
    observed = []

    async def exec_step(step):
        observed.append(agent._active_work_trust)
        return "ok"

    agent._execute_step = exec_step
    result = await agent.plan_and_run(
        "follow the attachment",
        extra_context="untrusted uploaded document text",
        work_trust=WorkTrust.TRUSTED_MANUAL,
    )
    assert result.success is True
    assert observed == [WorkTrust.AUTONOMOUS_OR_UNTRUSTED]


@pytest.mark.asyncio
async def test_workspace_context_taints_initial_terminal_plan():
    agent = _agent_with_plan(_RR("Step 1: [RUN_TERMINAL pytest -q]"))
    agent._workspace_context = MagicMock(return_value="repository instructions")
    observed = []

    async def exec_step(step):
        observed.append(agent._active_work_trust)
        return "ok"

    agent._execute_step = exec_step
    result = await agent.plan_and_run(
        "test the project", work_trust=WorkTrust.TRUSTED_MANUAL
    )
    assert result.success is True
    assert observed == [WorkTrust.AUTONOMOUS_OR_UNTRUSTED]


@pytest.mark.asyncio
async def test_resume_seed_context_taints_initial_terminal_plan():
    agent = _agent_with_plan(_RR("Step 1: [RUN_TERMINAL pytest -q]"))
    observed = []

    async def exec_step(step):
        observed.append(agent._active_work_trust)
        return "ok"

    agent._execute_step = exec_step
    result = await agent.plan_and_run(
        "resume work",
        seed_context="prior persisted step output",
        work_trust=WorkTrust.TRUSTED_MANUAL,
    )
    assert result.success is True
    assert observed == [WorkTrust.AUTONOMOUS_OR_UNTRUSTED]
