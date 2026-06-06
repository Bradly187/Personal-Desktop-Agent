"""Tests for the dependency-DAG wave executor (orchestration gap A).

Covers dependency parsing, wave scheduling (independent fan-out-safe steps run
concurrently; barriers run solo), and the failure/cancel fallback to the
sequential+replan loop. End-to-end plan_and_run tests confirm a deps-annotated
plan parallelizes while a plain plan stays sequential.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from inference.dev_agent import DevAgent, AgentStep, _parse_deps, _parse_plan
from core.scheduler import AccessibilityScheduler
from core.goal_session import GoalSessionStore


@pytest.fixture(autouse=True)
def _isolated_goal_session(tmp_path, monkeypatch):
    monkeypatch.setattr(GoalSessionStore, "PATH", tmp_path / "goal_session.json")
    GoalSessionStore.cancel()
    yield
    GoalSessionStore.cancel()


# ---------------------------------------------------------------------------
# Dependency parsing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("line,expected", [
    ("Step 3: [WRITE_FILE c] (after: 1, 2)", [1, 2]),
    ("Step 2: [RUN_TERMINAL pytest] [deps: 1]", [1]),
    ("Step 4: [GIT_COMMIT m] depends on 1 and 3", [1, 3]),
    ("Step 1: [READ_FILE a]", []),
    ("Step 5: [WRITE_FILE x] (after 2)", [2]),
])
def test_parse_deps(line, expected):
    assert _parse_deps(line) == expected


def test_parse_plan_captures_deps_and_strips_from_args():
    plan = "Step 1: [READ_FILE a]\nStep 2: [WRITE_FILE b] (after: 1)"
    steps = _parse_plan(plan)
    assert steps[0].deps == []
    assert steps[1].deps == [1]
    assert "after" not in steps[1].args.lower()   # annotation not swallowed into args


def test_plan_has_deps():
    assert DevAgent._plan_has_deps([AgentStep("READ_FILE"), AgentStep("WRITE_FILE", deps=[1])])
    assert not DevAgent._plan_has_deps([AgentStep("READ_FILE"), AgentStep("WRITE_FILE")])


# ---------------------------------------------------------------------------
# Wave executor
# ---------------------------------------------------------------------------

def _agent():
    agent = DevAgent(router=MagicMock())
    agent.set_scheduler(AccessibilityScheduler())
    agent._persist_step = AsyncMock()
    return agent


async def test_independent_safe_steps_run_concurrently():
    """Two independent WRITE_FILE steps execute in the same wave, concurrently."""
    agent = _agent()
    order = []
    peak = {"now": 0, "max": 0}

    async def _exec(step):
        peak["now"] += 1
        peak["max"] = max(peak["max"], peak["now"])
        await asyncio.sleep(0.02)
        peak["now"] -= 1
        order.append(step.args)
        return "ok"

    agent._execute_step = _exec
    remaining = [AgentStep("WRITE_FILE", args="a"), AgentStep("WRITE_FILE", args="b")]
    executed: list[AgentStep] = []
    cancelled = await agent._run_dag_waves(remaining, executed, run_id=1)

    assert cancelled is False
    assert peak["max"] == 2            # both ran at once
    assert {s.args for s in executed} == {"a", "b"}
    assert remaining == []             # all completed, nothing deferred


async def test_dependent_step_waits_for_its_dependency():
    agent = _agent()
    completed_order = []

    async def _exec(step):
        completed_order.append(step.args)
        return "ok"

    agent._execute_step = _exec
    # Step 2 depends on step 1 → must run after it.
    remaining = [AgentStep("WRITE_FILE", args="first"),
                 AgentStep("WRITE_FILE", args="second", deps=[1])]
    executed: list[AgentStep] = []
    await agent._run_dag_waves(remaining, executed, run_id=1)
    assert completed_order == ["first", "second"]


async def test_barrier_runs_solo():
    """A RUN_TERMINAL step is a barrier — never concurrent, even when ready."""
    agent = _agent()
    peak = {"now": 0, "max": 0}

    async def _exec(step):
        peak["now"] += 1
        peak["max"] = max(peak["max"], peak["now"])
        await asyncio.sleep(0.01)
        peak["now"] -= 1
        return "ok"

    agent._execute_step = _exec
    # Two RUN_TERMINAL with no deps: both "ready" but each is a barrier → solo.
    remaining = [AgentStep("RUN_TERMINAL", args="t1"), AgentStep("RUN_TERMINAL", args="t2")]
    executed: list[AgentStep] = []
    await agent._run_dag_waves(remaining, executed, run_id=1)
    assert peak["max"] == 1            # never concurrent


async def test_failure_defers_remainder_to_sequential():
    agent = _agent()

    async def _exec(step):
        if step.args == "boom":
            raise RuntimeError("fail")
        return "ok"

    agent._execute_step = _exec
    # Step 1 ok, step 2 (barrier) fails, step 3 depends on 2.
    remaining = [
        AgentStep("WRITE_FILE", args="ok1"),
        AgentStep("RUN_TERMINAL", args="boom"),
        AgentStep("WRITE_FILE", args="downstream", deps=[2]),
    ]
    executed: list[AgentStep] = []
    await agent._run_dag_waves(remaining, executed, run_id=1)
    # ok1 + the failed barrier are executed; the downstream step is deferred.
    assert [s.args for s in executed] == ["ok1", "boom"]
    assert [s.args for s in remaining] == ["downstream"]
    assert remaining[0].deps == []     # deps cleared for the sequential drain


async def test_cancellation_stops_waves():
    agent = _agent()
    agent._cancel_event.set()          # already cancelled
    agent._execute_step = AsyncMock(return_value="ok")
    remaining = [AgentStep("WRITE_FILE", args="a")]
    executed: list[AgentStep] = []
    cancelled = await agent._run_dag_waves(remaining, executed, run_id=1)
    assert cancelled is True
    assert executed == []              # nothing ran


# ---------------------------------------------------------------------------
# plan_and_run integration
# ---------------------------------------------------------------------------

class _RR:
    def __init__(self, text, ok=True, model="m"):
        self.text, self.ok, self.model = text, ok, model
        self.error = None if ok else "err"


def _agent_with_plan(plan_text):
    router = MagicMock()
    router.infer = AsyncMock(return_value=_RR(plan_text))
    agent = DevAgent(router=router)
    agent.set_scheduler(AccessibilityScheduler())
    agent._approve_plan_upfront = AsyncMock(return_value=True)
    agent._rag_context = AsyncMock(return_value="")
    agent._git_context = AsyncMock(return_value="")
    agent._format_context = lambda: ""
    agent._reflect = AsyncMock(return_value="done")
    agent._speak_plan_completion = AsyncMock()
    return agent


async def test_plan_with_deps_uses_dag_and_succeeds():
    agent = _agent_with_plan(
        "Step 1: [WRITE_FILE a]\nStep 2: [WRITE_FILE b]\nStep 3: [RUN_TERMINAL pytest] (after: 1, 2)"
    )
    ran = []

    async def _exec(step):
        ran.append(step.action)
        return "ok"

    agent._execute_step = _exec
    result = await agent.plan_and_run("build it")
    assert result.success is True
    assert len(result.steps) == 3
    assert ran[-1] == "RUN_TERMINAL"   # the (after:1,2) barrier ran last


async def test_plain_plan_stays_sequential():
    """No deps declared → DAG path not engaged (sequential loop handles it)."""
    agent = _agent_with_plan("Step 1: [WRITE_FILE a]\nStep 2: [WRITE_FILE b]")
    agent._execute_step = AsyncMock(return_value="ok")
    # Spy: _run_dag_waves must NOT be called.
    agent._run_dag_waves = AsyncMock(side_effect=AssertionError("DAG should not engage"))
    result = await agent.plan_and_run("do it")
    assert result.success is True
    assert len(result.steps) == 2
