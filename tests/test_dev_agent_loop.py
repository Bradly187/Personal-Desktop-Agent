"""Tests for the closed-loop (ReAct) DevAgent controller (B2).

plan_and_run must: replan after a step failure (bounded), never blindly continue
past a failure (no compounding), respect the replan budget, and break promptly on
cancellation.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from inference.dev_agent import DevAgent, AgentStep
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
