"""Tests for the unified agent-run trace tree (orchestration gap C).

A DevAgent plan emits one trace_id that all its spans share, and — because the
id is set as the current ContextVar — descendant spans recorded WITHOUT an
explicit trace_id (ModelRouter.infer, scheduler.fan_out children) attach to the
same run tree. These tests drive the tracer directly (set_enabled) so they don't
depend on DA_TRACE being set in the environment.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from monitoring.trace import get_tracer
from core.scheduler import AccessibilityScheduler
from inference.dev_agent import DevAgent
from core.goal_session import GoalSessionStore


@pytest.fixture(autouse=True)
def _tracing_on():
    tracer = get_tracer()
    prev = tracer.enabled
    tracer.set_enabled(True)
    yield tracer
    tracer.set_enabled(prev)


@pytest.fixture(autouse=True)
def _isolated_goal_session(tmp_path, monkeypatch):
    monkeypatch.setattr(GoalSessionStore, "PATH", tmp_path / "goal_session.json")
    GoalSessionStore.cancel()
    yield
    GoalSessionStore.cancel()


class _RR:
    def __init__(self, text, ok=True, model="test-model"):
        self.text, self.ok, self.model = text, ok, model
        self.error = None if ok else "err"


def _agent_with_plan(*responses):
    router = MagicMock()
    router.infer = AsyncMock(side_effect=list(responses))
    agent = DevAgent(router=router)
    agent._approve_plan_upfront = AsyncMock(return_value=True)
    agent._rag_context = AsyncMock(return_value="")
    agent._git_context = AsyncMock(return_value="")
    agent._format_context = lambda: ""
    agent._reflect = AsyncMock(return_value="summary")
    agent._persist_run = AsyncMock()
    agent._speak_plan_completion = AsyncMock()
    return agent


# ---------------------------------------------------------------------------
# scheduler.fan_out span
# ---------------------------------------------------------------------------

async def test_fan_out_records_span_under_current_trace(_tracing_on):
    tracer = _tracing_on
    tid = tracer.new_trace(kind="test")
    tok = tracer.set_current(tid)
    try:
        sched = AccessibilityScheduler()

        async def _c(x):
            return x

        await sched.fan_out([_c(1), _c(2), _c(3)], label="reads")
    finally:
        tracer.reset_current(tok)

    tr = tracer.get_trace(tid)
    fan = [s for s in tr["spans"] if s["stage"] == "fan_out"]
    assert len(fan) == 1
    assert fan[0]["attrs"]["count"] == 3
    assert fan[0]["attrs"]["label"] == "reads"


# ---------------------------------------------------------------------------
# DevAgent plan run tree
# ---------------------------------------------------------------------------

async def test_plan_run_is_one_trace_with_step_spans():
    agent = _agent_with_plan(_RR("Step 1: [WRITE_FILE a]\nStep 2: [WRITE_FILE b]"))
    agent._execute_step = AsyncMock(return_value="ok")

    result = await agent.plan_and_run("do the thing")
    assert result.success is True

    tracer = get_tracer()
    # Find the plan trace (the most recent one tagged kind=plan).
    plan_traces = [t for t in tracer.get_recent(10) if t["attrs"].get("kind") == "plan"]
    assert plan_traces, "no plan trace recorded"
    tr = plan_traces[-1]
    stages = [s["stage"] for s in tr["spans"]]
    assert "plan" in stages
    assert stages.count("step") == 2
    assert "plan_done" in stages
    # plan_done carries the outcome.
    done = [s for s in tr["spans"] if s["stage"] == "plan_done"][0]
    assert done["attrs"]["success"] is True
    assert done["attrs"]["steps"] == 2


async def test_descendant_span_attaches_to_run_tree_via_contextvar():
    """A span recorded with NO explicit trace_id during a step lands in the SAME
    trace — this is the mechanism that makes ModelRouter.infer spans attach."""
    agent = _agent_with_plan(_RR("Step 1: [READ_FILE x]"))

    async def _exec(step):
        # Simulate a deep-layer span (as ModelRouter.infer does) — no trace_id.
        get_tracer().record_span("inference", model="fake", backend="ollama")
        return "ok"

    agent._execute_step = _exec
    await agent.plan_and_run("read it")

    tracer = get_tracer()
    tr = [t for t in tracer.get_recent(10) if t["attrs"].get("kind") == "plan"][-1]
    stages = [s["stage"] for s in tr["spans"]]
    assert "inference" in stages   # descendant span joined the run tree
    assert "plan" in stages and "step" in stages


async def test_plan_error_path_still_closes_trace():
    agent = _agent_with_plan(_RR("", ok=False))   # planner errors
    result = await agent.plan_and_run("bad goal")
    assert result.error is not None

    tracer = get_tracer()
    tr = [t for t in tracer.get_recent(10) if t["attrs"].get("kind") == "plan"][-1]
    stages = [s["stage"] for s in tr["spans"]]
    assert "plan" in stages
    assert "plan_done" in stages   # reset/closed even on the early-return path
    # Current trace was reset (no leak into the next operation).
    assert tracer.get_current() is None


async def test_zero_cost_when_disabled():
    """With tracing off, plan_and_run records nothing and get_current stays None."""
    tracer = get_tracer()
    tracer.set_enabled(False)
    agent = _agent_with_plan(_RR("Step 1: [WRITE_FILE a]"))
    agent._execute_step = AsyncMock(return_value="ok")
    before = len(tracer.get_recent(50))
    await agent.plan_and_run("x")
    assert len(tracer.get_recent(50)) == before   # nothing added
    assert tracer.get_current() is None
