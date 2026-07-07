"""Tests for the dev-agent per-step wall-clock timeout (#1).

A step with no internal timeout (vision inference, a wedged skill call, stalled
I/O) must not hold the single dev permit until the scheduler's 300s plan ceiling.
_run_step_with_retry wraps _execute_step in asyncio.wait_for(STEP_TIMEOUT_S); a
hung step fails fast and the loop replans. Skill calls get a tighter bound.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from inference.dev_agent import DevAgent, AgentStep


def _agent() -> DevAgent:
    return DevAgent(router=MagicMock())


async def test_hung_step_times_out_and_fails_fast():
    agent = _agent()
    agent.STEP_TIMEOUT_S = 0.1          # shrink the backstop for the test

    async def _hang(step):
        await asyncio.sleep(30)          # no internal timeout — would wedge forever
        return "never"

    agent._execute_step = _hang
    step = AgentStep(action="READ_SCREEN", args="what is on screen")

    t0 = asyncio.get_event_loop().time()
    ok = await agent._run_step_with_retry(step)
    elapsed = asyncio.get_event_loop().time() - t0

    assert ok is False
    assert step.success is False
    assert "timed out" in (step.result or "")
    assert elapsed < 5.0                 # returned at the 0.1s timeout, not 30s


async def test_fast_step_still_succeeds():
    agent = _agent()
    agent.STEP_TIMEOUT_S = 5.0

    async def _quick(step):
        return f"ok:{step.action}"

    agent._execute_step = _quick
    step = AgentStep(action="READ_FILE", args="x.py")
    ok = await agent._run_step_with_retry(step)
    assert ok is True
    assert step.success is True
    assert step.result == "ok:READ_FILE"


async def test_timeout_retries_for_retryable_verb():
    """A retryable (read-only) verb that times out is attempted twice."""
    agent = _agent()
    agent.STEP_TIMEOUT_S = 0.05
    attempts = {"n": 0}

    async def _hang(step):
        attempts["n"] += 1
        await asyncio.sleep(30)

    agent._execute_step = _hang
    step = AgentStep(action="READ_FILE", args="x.py")   # in _RETRYABLE_VERBS
    ok = await agent._run_step_with_retry(step)
    assert ok is False
    assert attempts["n"] == 2            # retried once after the first timeout


async def test_real_cancel_propagates_not_swallowed():
    """A genuine plan cancellation (CancelledError) must propagate, not be eaten
    by the timeout's except-Exception (CancelledError is BaseException)."""
    agent = _agent()
    agent.STEP_TIMEOUT_S = 30.0

    async def _cancelled(step):
        raise asyncio.CancelledError()

    agent._execute_step = _cancelled
    step = AgentStep(action="READ_FILE", args="x.py")
    with pytest.raises(asyncio.CancelledError):
        await agent._run_step_with_retry(step)


async def test_skill_call_times_out():
    """A wedged skill (MCP stdio) call is bounded by SKILL_CALL_TIMEOUT_S and
    returns a 'timed out' sentinel rather than hanging the step."""
    agent = _agent()
    agent.SKILL_CALL_TIMEOUT_S = 0.1

    registry = MagicMock()
    registry.is_send_tool = MagicMock(return_value=False)

    async def _hang_call(skill_id, tool, args):
        await asyncio.sleep(30)
        return {"text": "never"}

    registry.call = _hang_call
    agent._skill_registry = registry

    step = AgentStep(action="SKILL_QUERY", args="echo ping")
    t0 = asyncio.get_event_loop().time()
    out = await agent._execute_skill_step(step)
    elapsed = asyncio.get_event_loop().time() - t0

    assert "timed out" in out.lower()
    assert elapsed < 5.0
