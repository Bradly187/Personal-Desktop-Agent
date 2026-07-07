"""Tests for sub-agent fan-out (orchestration gap #1).

Two layers:
  A. AccessibilityScheduler.fan_out — concurrency under a SEPARATE semaphore
     (_subagent_sem, N>1), distinct from _dev_sem; per-child timeout; order
     preserved; exceptions collected; deadlock-free when called while holding
     the dev permit.
  B. DevAgent._gather_readonly_prefix — fans out a leading run of read-only
     steps, with exact-preservation fallback to sequential on failure.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent))

from core.scheduler import AccessibilityScheduler, _MAX_CONCURRENT_SUBAGENTS


# ===========================================================================
# A. Scheduler.fan_out
# ===========================================================================

async def test_fanout_returns_results_in_order():
    sched = AccessibilityScheduler()

    async def _v(x):
        await asyncio.sleep(0.01)
        return x * 10

    out = await sched.fan_out([_v(1), _v(2), _v(3)], label="t")
    assert out == [10, 20, 30]


async def test_fanout_empty_is_noop():
    sched = AccessibilityScheduler()
    assert await sched.fan_out([]) == []


async def test_fanout_caps_concurrency_at_semaphore():
    """No more than _MAX_CONCURRENT_SUBAGENTS children run at once."""
    sched = AccessibilityScheduler()
    state = {"now": 0, "peak": 0}

    async def _track():
        state["now"] += 1
        state["peak"] = max(state["peak"], state["now"])
        await asyncio.sleep(0.02)
        state["now"] -= 1
        return True

    n = _MAX_CONCURRENT_SUBAGENTS + 3
    out = await sched.fan_out([_track() for _ in range(n)])
    assert out == [True] * n
    assert state["peak"] <= _MAX_CONCURRENT_SUBAGENTS
    assert state["peak"] == _MAX_CONCURRENT_SUBAGENTS   # actually saturates


async def test_fanout_collects_exceptions_without_aborting():
    sched = AccessibilityScheduler()

    async def _ok():
        return "ok"

    async def _boom():
        raise ValueError("nope")

    out = await sched.fan_out([_ok(), _boom(), _ok()], return_exceptions=True)
    assert out[0] == "ok"
    assert isinstance(out[1], ValueError)
    assert out[2] == "ok"


async def test_fanout_per_child_timeout():
    sched = AccessibilityScheduler()

    async def _hang():
        await asyncio.sleep(5.0)
        return "late"

    async def _quick():
        return "fast"

    out = await sched.fan_out([_quick(), _hang()], timeout_s=0.05, return_exceptions=True)
    assert out[0] == "fast"
    assert isinstance(out[1], asyncio.TimeoutError)


async def test_fanout_is_deadlock_free_under_dev_permit():
    """A dev task holding the single _dev_sem permit can fan out children that
    each take _subagent_sem — they do NOT contend for the parent's permit."""
    sched = AccessibilityScheduler()

    async def _child(i):
        await asyncio.sleep(0.01)
        return i

    async def _parent():
        # Hold the single dev permit, then fan out — must not deadlock.
        async with sched._dev_sem:
            return await sched.fan_out([_child(1), _child(2)], label="nested")

    out = await asyncio.wait_for(_parent(), timeout=2.0)
    assert out == [1, 2]
    # Permit fully released afterwards.
    assert sched._dev_sem._value == 1


def test_status_reports_subagent_fields():
    sched = AccessibilityScheduler()
    st = sched.get_status()
    assert st["max_concurrent_subagents"] == _MAX_CONCURRENT_SUBAGENTS
    assert st["subagent_inflight"] == 0


# ===========================================================================
# B. DevAgent._gather_readonly_prefix
# ===========================================================================

from inference.dev_agent import DevAgent, AgentStep


def _agent_with_scheduler():
    sched = AccessibilityScheduler()
    agent = DevAgent(router=None)          # router unused on these paths
    agent.set_scheduler(sched)
    return agent, sched


def _steps(*verbs) -> list[AgentStep]:
    return [AgentStep(action=v) for v in verbs]


async def test_prefix_detection_stops_at_first_action_verb():
    agent, _ = _agent_with_scheduler()
    rem = _steps("READ_FILE", "GREP", "WRITE_FILE", "READ_FILE")
    prefix = agent._leading_parallel_prefix(rem)
    assert [s.action for s in prefix] == ["READ_FILE", "GREP"]


async def test_gather_adopts_all_success_prefix(monkeypatch):
    agent, _ = _agent_with_scheduler()
    ran: list[str] = []

    async def _fake_retry(step):
        ran.append(step.action)
        step.success = True
        step.result = f"{step.action}-ok"
        return True

    monkeypatch.setattr(agent, "_run_step_with_retry", _fake_retry)
    monkeypatch.setattr(agent, "_persist_step", _async_noop)

    remaining = _steps("READ_FILE", "GREP", "WRITE_FILE")
    executed: list[AgentStep] = []
    await agent._gather_readonly_prefix(remaining, executed, run_id=1)

    # The 2 read-only steps were adopted; the action step remains.
    assert [s.action for s in executed] == ["READ_FILE", "GREP"]
    assert [s.action for s in remaining] == ["WRITE_FILE"]
    assert set(ran) == {"READ_FILE", "GREP"}


async def test_gather_falls_back_on_failure(monkeypatch):
    """On any failure the batch is discarded and remaining is left intact."""
    agent, _ = _agent_with_scheduler()

    async def _fake_retry(step):
        # GREP fails; READ_FILE succeeds.
        ok = step.action != "GREP"
        step.success = ok
        return ok

    monkeypatch.setattr(agent, "_run_step_with_retry", _fake_retry)
    monkeypatch.setattr(agent, "_persist_step", _async_noop)

    remaining = _steps("READ_FILE", "GREP", "WRITE_FILE")
    executed: list[AgentStep] = []
    await agent._gather_readonly_prefix(remaining, executed, run_id=1)

    # Nothing adopted — sequential loop will handle (idempotent re-run).
    assert executed == []
    assert [s.action for s in remaining] == ["READ_FILE", "GREP", "WRITE_FILE"]


async def test_gather_noop_without_scheduler(monkeypatch):
    agent = DevAgent(router=None)          # no scheduler wired
    remaining = _steps("READ_FILE", "GREP")
    executed: list[AgentStep] = []
    await agent._gather_readonly_prefix(remaining, executed, run_id=1)
    assert executed == []
    assert len(remaining) == 2


async def test_gather_noop_for_single_readonly_step(monkeypatch):
    agent, _ = _agent_with_scheduler()
    called = {"n": 0}

    async def _fake_retry(step):
        called["n"] += 1
        return True

    monkeypatch.setattr(agent, "_run_step_with_retry", _fake_retry)
    remaining = _steps("READ_FILE", "WRITE_FILE")  # only 1 leading read-only
    executed: list[AgentStep] = []
    await agent._gather_readonly_prefix(remaining, executed, run_id=1)
    assert called["n"] == 0                 # not worth fanning out a single step
    assert len(remaining) == 2


async def _async_noop(*a, **k):
    return None
