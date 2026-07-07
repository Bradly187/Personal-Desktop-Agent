"""Tests for the durable goal backlog (gap D).

AgentDB goal_queue: enqueue+dedupe, atomic claim, complete, requeue-stale,
max_attempts poison cap. Plus DevAgent.drain_goal_queue end-to-end with a fake
plan_and_run.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from storage.db import AgentDB
from inference.dev_agent import DevAgent, AgentResult


@pytest.fixture
async def db(tmp_path):
    d = AgentDB()
    await d.open(tmp_path / "agent.db")
    if not d.available:
        pytest.skip("aiosqlite unavailable")
    yield d
    await d.close()


# ---------------------------------------------------------------------------
# AgentDB goal_queue
# ---------------------------------------------------------------------------

async def test_enqueue_and_claim_order(db):
    a = await db.goals.enqueue_goal("first", idempotency_key="k1")
    b = await db.goals.enqueue_goal("second", idempotency_key="k2")
    assert a > 0 and b > 0 and a != b

    g1 = await db.goals.claim_next_goal()
    assert g1["goal"] == "first"            # oldest first
    assert g1["status"] == "running"
    assert g1["attempts"] == 1
    g2 = await db.goals.claim_next_goal()
    assert g2["goal"] == "second"
    assert await db.goals.claim_next_goal() is None   # queue drained


async def test_enqueue_dedupes_on_idempotency_key(db):
    a = await db.goals.enqueue_goal("same", idempotency_key="dup")
    b = await db.goals.enqueue_goal("same", idempotency_key="dup")
    assert a == b                            # second is a no-op, same row id
    # Only one claimable goal exists.
    assert await db.goals.claim_next_goal() is not None
    assert await db.goals.claim_next_goal() is None


async def test_complete_goal_terminal(db):
    gid = await db.goals.enqueue_goal("g", idempotency_key="k")
    await db.goals.claim_next_goal()
    await db.goals.complete_goal(gid, "done")
    # Done goals are not re-claimed.
    assert await db.goals.claim_next_goal() is None
    assert await db.goals.get_queued_goals() == []


async def test_requeue_stale_running_recovers_crash(db):
    gid = await db.goals.enqueue_goal("crashy", idempotency_key="k")
    claimed = await db.goals.claim_next_goal()     # → running, attempts=1
    assert claimed is not None
    # Simulate a crash: the row is left 'running'. Startup requeues it.
    n = await db.runs.requeue_stale_running()
    assert n == 1
    again = await db.goals.claim_next_goal()       # claimable again
    assert again["goal"] == "crashy"
    assert again["attempts"] == 2            # attempt counter advanced


async def test_poison_goal_capped_at_max_attempts(db):
    gid = await db.goals.enqueue_goal("poison", idempotency_key="k", max_attempts=2)
    # attempt 1
    await db.goals.claim_next_goal()
    await db.runs.requeue_stale_running()
    # attempt 2
    await db.goals.claim_next_goal()
    n = await db.runs.requeue_stale_running()     # attempts(2) >= max(2) → failed, not requeued
    assert n == 0
    assert await db.goals.claim_next_goal() is None
    assert await db.goals.get_queued_goals() == []


# ---------------------------------------------------------------------------
# DevAgent.drain_goal_queue
# ---------------------------------------------------------------------------

def _agent(db):
    agent = DevAgent(router=None, agent_db=db)
    return agent


async def test_drain_runs_all_queued_goals(db, monkeypatch):
    await db.goals.enqueue_goal("g1", idempotency_key="k1")
    await db.goals.enqueue_goal("g2", idempotency_key="k2")
    agent = _agent(db)
    ran = []

    async def _fake_plan(goal):
        ran.append(goal)
        return AgentResult(goal=goal, domain="plan", model_used="m", success=True)

    monkeypatch.setattr(agent, "plan_and_run", _fake_plan)
    n = await agent.drain_goal_queue()
    assert n == 2
    assert ran == ["g1", "g2"]
    assert await db.goals.get_queued_goals() == []   # all consumed


async def test_drain_marks_failed_on_exception(db, monkeypatch):
    await db.goals.enqueue_goal("boom", idempotency_key="k")
    agent = _agent(db)

    async def _raise(goal):
        raise RuntimeError("plan blew up")

    monkeypatch.setattr(agent, "plan_and_run", _raise)
    n = await agent.drain_goal_queue()
    assert n == 1
    # Goal is terminal (failed), not stuck queued/running.
    assert await db.goals.claim_next_goal() is None


async def test_drain_respects_max_goals(db, monkeypatch):
    for i in range(3):
        await db.goals.enqueue_goal(f"g{i}", idempotency_key=f"k{i}")
    agent = _agent(db)
    monkeypatch.setattr(agent, "plan_and_run",
                        AsyncMock(return_value=AgentResult(goal="x", domain="plan",
                                                           model_used="m", success=True)))
    n = await agent.drain_goal_queue(max_goals=2)
    assert n == 2
    assert len(await db.goals.get_queued_goals()) == 1   # one left


async def test_drain_stops_on_cancel(db, monkeypatch):
    await db.goals.enqueue_goal("g", idempotency_key="k")
    agent = _agent(db)
    agent._cancel_event.set()
    monkeypatch.setattr(agent, "plan_and_run", AsyncMock())
    n = await agent.drain_goal_queue()
    assert n == 0
    assert len(await db.goals.get_queued_goals()) == 1   # untouched


# ---------------------------------------------------------------------------
# Single-flight drainer + flare gate (audit fix 2026-06-09)
# ---------------------------------------------------------------------------

async def test_concurrent_drain_is_single_flight_and_no_goal_abandoned(db, monkeypatch):
    """A second drain call while one is active returns 0 immediately, and the
    active drainer picks up a goal enqueued mid-run (re-check signal)."""
    import asyncio

    await db.goals.enqueue_goal("g1", idempotency_key="k1")
    agent = _agent(db)
    ran: list[str] = []
    release = asyncio.Event()

    async def _slow_plan(goal):
        ran.append(goal)
        await release.wait()                # hold the drainer mid-goal
        return AgentResult(goal=goal, domain="plan", model_used="m", success=True)

    monkeypatch.setattr(agent, "plan_and_run", _slow_plan)

    drain1 = asyncio.create_task(agent.drain_goal_queue())
    await asyncio.sleep(0.05)               # let drainer claim g1 and block

    # New goal arrives + second drain call (the coordinator "authorize" path)
    await db.goals.enqueue_goal("g2", idempotency_key="k2")
    n2 = await agent.drain_goal_queue()
    assert n2 == 0                          # single-flight: did not run anything

    release.set()
    n1 = await drain1
    assert n1 == 2                          # active drainer processed BOTH
    assert ran == ["g1", "g2"]              # g2 was not abandoned
    assert await db.goals.get_queued_goals() == []


async def test_drain_waits_for_flare_gate(db, monkeypatch):
    """With dev admission paused, the drainer must not claim until resume."""
    import asyncio

    await db.goals.enqueue_goal("g", idempotency_key="k")
    agent = _agent(db)
    resume = asyncio.Event()

    class _FakeScheduler:
        @property
        def dev_paused(self):
            return not resume.is_set()

        async def wait_dev_admission(self):
            await resume.wait()

    agent._scheduler = _FakeScheduler()
    ran: list[str] = []

    async def _plan(goal):
        ran.append(goal)
        return AgentResult(goal=goal, domain="plan", model_used="m", success=True)

    monkeypatch.setattr(agent, "plan_and_run", _plan)

    drain = asyncio.create_task(agent.drain_goal_queue())
    await asyncio.sleep(0.05)
    assert ran == []                                    # parked at the gate
    assert (await db.goals.get_queued_goals())[0]["goal"] == "g"  # not yet claimed

    resume.set()                                        # flare ends
    n = await drain
    assert n == 1
    assert ran == ["g"]


async def test_plan_and_run_is_serialized():
    """Two concurrent plan_and_run calls must not interleave (shared plan state)."""
    import asyncio
    from unittest.mock import MagicMock

    agent = DevAgent(router=MagicMock())
    order: list[str] = []

    async def _locked_body(goal, trace_id="", seed_context="", extra_context=""):
        order.append(f"start:{goal}")
        await asyncio.sleep(0.05)
        order.append(f"end:{goal}")
        return AgentResult(goal=goal, domain="plan", model_used="m", success=True)

    agent._plan_and_run_locked = _locked_body
    await asyncio.gather(agent.plan_and_run("a"), agent.plan_and_run("b"))
    # Strict serialization: no interleaving of start/end pairs.
    assert order in (
        ["start:a", "end:a", "start:b", "end:b"],
        ["start:b", "end:b", "start:a", "end:a"],
    )
