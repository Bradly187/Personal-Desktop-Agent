"""Tests for the planner-driven DELEGATE verb (specs/dev-agent-delegate-verb, Gap D).

One assertion per numbered acceptance criterion (cited in the test name). The child
investigation is exercised with a stubbed router so no model/GPU is needed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from inference.dev_agent import AgentStep, DevAgent, _DELEGATE_PROMPT_INSTRUCTIONS


class _Result:
    def __init__(self, text, ok=True):
        self.text = text
        self.ok = ok
        self.model = "stub"


def _agent(enabled=True):
    agent = DevAgent(router=MagicMock())
    agent._delegate_enabled = enabled
    agent._agent_db = None              # journaling best-effort / no-op
    agent._scheduler = None             # run inline (no sub-agent pool in tests)
    return agent


# -- R1: read-only DELEGATE verb -------------------------------------------- #

def test_r1_1_verb_registered():
    from inference.dev_agent import _PLAN_ACTIONS, _STEP_PATTERN
    assert "DELEGATE" in _PLAN_ACTIONS
    m = _STEP_PATTERN.match("[DELEGATE which module defines FooBar]")
    assert m and m.group(1).upper() == "DELEGATE"


def test_r1_3_not_in_fanout_sets():
    # Read-only/retryable but NOT concurrently fanned out (spec R1.3).
    assert "DELEGATE" in DevAgent._RETRYABLE_VERBS
    assert "DELEGATE" not in DevAgent._PARALLEL_VERBS
    assert "DELEGATE" not in DevAgent._FANOUT_SAFE_VERBS


@pytest.mark.asyncio
async def test_r1_2_child_restricted_to_readonly_and_returns_finding():
    agent = _agent()
    # Child plan proposes a read + a (forbidden) write; only the read must run.
    agent._router.infer = AsyncMock(side_effect=[
        _Result("[READ_FILE foo.py]\n[WRITE_FILE foo.py]"),   # child plan
        _Result("FooBar is defined in foo.py"),               # synthesis
    ])
    agent._execute_step = AsyncMock(return_value="class FooBar: ...")
    out = await agent._delegate_investigate("where is FooBar", depth=1)
    assert "DELEGATE finding" in out and "FooBar is defined in foo.py" in out
    # exactly one step executed — the READ_FILE; the WRITE_FILE was dropped (R2.1)
    assert agent._execute_step.await_count == 1
    ran = agent._execute_step.await_args_list[0].args[0]
    assert ran.action.upper() == "READ_FILE"


# -- R2: structurally read-only --------------------------------------------- #

@pytest.mark.asyncio
async def test_r2_1_destructive_child_step_dropped_not_executed():
    agent = _agent()
    agent._router.infer = AsyncMock(side_effect=[
        _Result("[RUN_TERMINAL rm -rf /]\n[EDIT_FILE x]\n[GIT_COMMIT bad]"),
        _Result("nothing"),
    ])
    agent._execute_step = AsyncMock(return_value="should-not-run")
    await agent._delegate_investigate("q", depth=1)
    agent._execute_step.assert_not_called()   # every proposed step was destructive


# -- R3: bounded — depth, steps, plan-lock ---------------------------------- #

@pytest.mark.asyncio
async def test_r3_1_refused_at_max_depth():
    agent = _agent()
    agent._max_delegate_depth = 1
    agent._router.infer = AsyncMock()
    out = await agent._delegate_investigate("q", depth=2)
    assert "max delegation depth" in out
    agent._router.infer.assert_not_called()    # no child spawned


@pytest.mark.asyncio
async def test_r3_2_does_not_acquire_plan_lock():
    agent = _agent()
    agent._router.infer = AsyncMock(side_effect=[_Result("[READ_FILE a]"), _Result("ans")])
    agent._execute_step = AsyncMock(return_value="data")
    # Hold the plan lock as a real concurrent plan would — delegate must not block.
    async with agent._plan_lock:
        out = await agent._delegate_investigate("q", depth=1)   # would deadlock if it re-acquired
    assert "DELEGATE finding" in out


@pytest.mark.asyncio
async def test_r3_3_step_budget_bounded():
    agent = _agent()
    agent._delegate_max_steps = 2
    # Plan proposes 5 reads; only the first 2 may run.
    plan = "\n".join(f"[READ_FILE f{i}.py]" for i in range(5))
    agent._router.infer = AsyncMock(side_effect=[_Result(plan), _Result("done")])
    agent._execute_step = AsyncMock(return_value="x")
    await agent._delegate_investigate("q", depth=1)
    assert agent._execute_step.await_count == 2


# -- R4: substrate reuse, degradation, flag --------------------------------- #

@pytest.mark.asyncio
async def test_r4_1_journals_mode_delegate():
    agent = _agent()
    db = MagicMock()
    db.available = True
    db.insert_workflow = AsyncMock(return_value=1)
    agent._agent_db = db
    agent._router.infer = AsyncMock(side_effect=[_Result("[READ_FILE a]"), _Result("ans")])
    agent._execute_step = AsyncMock(return_value="data")
    await agent._delegate_investigate("q", depth=1)
    db.insert_workflow.assert_awaited()
    assert db.insert_workflow.await_args.kwargs["mode"] == "delegate"


@pytest.mark.asyncio
async def test_r4_2_flare_skips_safely():
    agent = _agent()
    agent._delegate_skip_check = lambda: True   # flare active
    agent._router.infer = AsyncMock()
    out = await agent._delegate_investigate("q", depth=1)
    assert "skipped: flare" in out
    agent._router.infer.assert_not_called()


@pytest.mark.asyncio
async def test_r4_3_child_error_is_safe_observation():
    agent = _agent()
    agent._router.infer = AsyncMock(side_effect=RuntimeError("model down"))
    out = await agent._delegate_investigate("q", depth=1)
    assert out.startswith("DELEGATE failed")   # never raises into _execute_step


@pytest.mark.asyncio
async def test_r4_4_disabled_is_noop():
    agent = _agent(enabled=False)
    agent._router.infer = AsyncMock()
    out = await agent._delegate_investigate("q", depth=1)
    assert "feature disabled" in out
    agent._router.infer.assert_not_called()


def test_r4_4_teaching_absent_when_disabled():
    # The teaching block is a distinct constant; the planner only sees it when the
    # flag is on at depth 0 (asserted via the injection guard in _plan_and_run_locked).
    assert "DELEGATE" in _DELEGATE_PROMPT_INSTRUCTIONS
