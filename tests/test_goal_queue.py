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
    a = await db.enqueue_goal("first", idempotency_key="k1")
    b = await db.enqueue_goal("second", idempotency_key="k2")
    assert a > 0 and b > 0 and a != b

    g1 = await db.claim_next_goal()
    assert g1["goal"] == "first"            # oldest first
    assert g1["status"] == "running"
    assert g1["attempts"] == 1
    g2 = await db.claim_next_goal()
    assert g2["goal"] == "second"
    assert await db.claim_next_goal() is None   # queue drained


async def test_enqueue_dedupes_on_idempotency_key(db):
    a = await db.enqueue_goal("same", idempotency_key="dup")
    b = await db.enqueue_goal("same", idempotency_key="dup")
    assert a == b                            # second is a no-op, same row id
    # Only one claimable goal exists.
    assert await db.claim_next_goal() is not None
    assert await db.claim_next_goal() is None


async def test_complete_goal_terminal(db):
    gid = await db.enqueue_goal("g", idempotency_key="k")
    await db.claim_next_goal()
    await db.complete_goal(gid, "done")
    # Done goals are not re-claimed.
    assert await db.claim_next_goal() is None
    assert await db.get_queued_goals() == []


async def test_requeue_stale_running_recovers_crash(db):
    gid = await db.enqueue_goal("crashy", idempotency_key="k")
    claimed = await db.claim_next_goal()     # → running, attempts=1
    assert claimed is not None
    # Simulate a crash: the row is left 'running'. Startup requeues it.
    n = await db.requeue_stale_running()
    assert n == 1
    again = await db.claim_next_goal()       # claimable again
    assert again["goal"] == "crashy"
    assert again["attempts"] == 2            # attempt counter advanced


async def test_poison_goal_capped_at_max_attempts(db):
    gid = await db.enqueue_goal("poison", idempotency_key="k", max_attempts=2)
    # attempt 1
    await db.claim_next_goal()
    await db.requeue_stale_running()
    # attempt 2
    await db.claim_next_goal()
    n = await db.requeue_stale_running()     # attempts(2) >= max(2) → failed, not requeued
    assert n == 0
    assert await db.claim_next_goal() is None
    assert await db.get_queued_goals() == []


# ---------------------------------------------------------------------------
# DevAgent.drain_goal_queue
# ---------------------------------------------------------------------------

def _agent(db):
    agent = DevAgent(router=None, agent_db=db)
    return agent


async def test_drain_runs_all_queued_goals(db, monkeypatch):
    await db.enqueue_goal("g1", idempotency_key="k1")
    await db.enqueue_goal("g2", idempotency_key="k2")
    agent = _agent(db)
    ran = []

    async def _fake_plan(goal):
        ran.append(goal)
        return AgentResult(goal=goal, domain="plan", model_used="m", success=True)

    monkeypatch.setattr(agent, "plan_and_run", _fake_plan)
    n = await agent.drain_goal_queue()
    assert n == 2
    assert ran == ["g1", "g2"]
    assert await db.get_queued_goals() == []   # all consumed


async def test_drain_marks_failed_on_exception(db, monkeypatch):
    await db.enqueue_goal("boom", idempotency_key="k")
    agent = _agent(db)

    async def _raise(goal):
        raise RuntimeError("plan blew up")

    monkeypatch.setattr(agent, "plan_and_run", _raise)
    n = await agent.drain_goal_queue()
    assert n == 1
    # Goal is terminal (failed), not stuck queued/running.
    assert await db.claim_next_goal() is None


async def test_drain_respects_max_goals(db, monkeypatch):
    for i in range(3):
        await db.enqueue_goal(f"g{i}", idempotency_key=f"k{i}")
    agent = _agent(db)
    monkeypatch.setattr(agent, "plan_and_run",
                        AsyncMock(return_value=AgentResult(goal="x", domain="plan",
                                                           model_used="m", success=True)))
    n = await agent.drain_goal_queue(max_goals=2)
    assert n == 2
    assert len(await db.get_queued_goals()) == 1   # one left


async def test_drain_stops_on_cancel(db, monkeypatch):
    await db.enqueue_goal("g", idempotency_key="k")
    agent = _agent(db)
    agent._cancel_event.set()
    monkeypatch.setattr(agent, "plan_and_run", AsyncMock())
    n = await agent.drain_goal_queue()
    assert n == 0
    assert len(await db.get_queued_goals()) == 1   # untouched
