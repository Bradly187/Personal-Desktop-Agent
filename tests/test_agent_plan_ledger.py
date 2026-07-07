"""Tests for the durable, resumable plan ledger (B3).

- AgentDB run lifecycle: start (running) → finalize (terminal); crash reconcile.
- DevAgent.plan_and_run persists a terminal status (not left 'running').
- DevAgent.resume_pending_plan is gated on explicit voice confirmation.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from storage.db import AgentDB
from inference.dev_agent import DevAgent
from core.goal_session import GoalSessionStore


class _RR:
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


async def _open_db(tmp_path) -> AgentDB:
    db = AgentDB()
    await db.open(tmp_path / "ledger.db")
    return db


# ---------------------------------------------------------------------------
# AgentDB run lifecycle
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_lifecycle_and_crash_recovery(tmp_path):
    db = await _open_db(tmp_path)
    try:
        rid = await db.runs.start_agent_run("goal x", "plan", "model-y")
        assert rid > 0

        # A run left 'running' is reconciled to 'interrupted' on recovery.
        assert await db.runs.mark_interrupted_runs() == 1
        runs = await db.runs.get_interrupted_runs()
        assert runs and runs[0]["goal"] == "goal x"

        # A finalized run is NOT picked up by recovery.
        rid2 = await db.runs.start_agent_run("goal z", "plan", "m")
        await db.runs.update_agent_run(rid2, "completed", step_count=3,
                                  success=True, total_latency_ms=12.0)
        assert await db.runs.mark_interrupted_runs() == 0
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# DevAgent persists a terminal status (never left 'running')
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_plan_and_run_finalizes_status(tmp_path):
    db = await _open_db(tmp_path)
    try:
        router = MagicMock()
        router.infer = AsyncMock(return_value=_RR("Step 1: [EXPLAIN hello world]"))
        agent = DevAgent(router=router, agent_db=db)
        agent._approve_plan_upfront = AsyncMock(return_value=True)
        agent._rag_context = AsyncMock(return_value="")
        agent._git_context = AsyncMock(return_value="")
        agent._format_context = lambda: ""
        agent._reflect = AsyncMock(return_value="done")
        agent._speak_plan_completion = AsyncMock()

        async def exec_step(step):
            return "ok"
        agent._execute_step = exec_step

        result = await agent.plan_and_run("explain something")
        assert result.success is True

        # Recovery finds nothing → the run was finalized, not left 'running'.
        assert await db.runs.mark_interrupted_runs() == 0
        cur = await db._conn.execute("SELECT status, step_count FROM agent_runs")
        rows = await cur.fetchall()
        assert len(rows) == 1
        assert rows[0]["status"] == "completed"
        assert rows[0]["step_count"] >= 1
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# resume_pending_plan — gated on explicit confirmation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resume_declined_does_not_run(tmp_path):
    db = await _open_db(tmp_path)
    try:
        await db.runs.start_agent_run("old goal", "plan", "m")
        await db.runs.mark_interrupted_runs()

        agent = DevAgent(router=MagicMock(), agent_db=db)
        agent._confirm_destructive_op = AsyncMock(return_value=False)
        agent.plan_and_run = AsyncMock()

        out = await agent.resume_pending_plan()
        assert out is None
        agent.plan_and_run.assert_not_awaited()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_resume_approved_runs_plan(tmp_path):
    db = await _open_db(tmp_path)
    try:
        await db.runs.start_agent_run("old goal", "plan", "m")
        await db.runs.mark_interrupted_runs()

        agent = DevAgent(router=MagicMock(), agent_db=db)
        agent._confirm_destructive_op = AsyncMock(return_value=True)
        agent.plan_and_run = AsyncMock()

        out = await agent.resume_pending_plan()
        assert out is not None and out["goal"] == "old goal"
        agent.plan_and_run.assert_awaited_once()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_resume_noop_when_nothing_interrupted(tmp_path):
    db = await _open_db(tmp_path)
    try:
        agent = DevAgent(router=MagicMock(), agent_db=db)
        agent._confirm_destructive_op = AsyncMock(return_value=True)
        agent.plan_and_run = AsyncMock()

        assert await agent.resume_pending_plan() is None
        agent._confirm_destructive_op.assert_not_awaited()  # nothing to confirm
        agent.plan_and_run.assert_not_awaited()
    finally:
        await db.close()
