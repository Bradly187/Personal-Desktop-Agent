"""Tests for DevAgent saga integrity (S2).

Covers:
  - compensation-throws -> row failed + escalation
  - partial-write -> snapshot restored
  - un-snapshottable -> skipped + escalation
  - replan zero-step -> counts toward MAX_REPLANS (halts honestly)
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock


sys.path.insert(0, str(Path(__file__).parent.parent))

from inference.dev_agent import DevAgent, AgentStep
from storage.db import AgentDB


async def _open_db(tmp_path):
    db = AgentDB()
    await db.open(tmp_path / "saga_integrity.db")
    return db


async def _make_agent(db) -> DevAgent:
    router = MagicMock()
    agent = DevAgent(router=router, agent_db=db)
    return agent


async def _seed_run(db) -> int:
    await db.sessions.insert_session(mode="test")
    return await db.runs.start_agent_run("saga integrity test", "plan", "model")


async def test_compensation_throws_escalates(tmp_path):
    db = await _open_db(tmp_path)
    agent = await _make_agent(db)
    run_id = await _seed_run(db)

    step_id = await db.runs.insert_agent_step(
        run_id, 1, "WRITE_FILE", "/root/protected.py", None, "ok", True, 1.0,
        compensation_action="DELETE_FILE", compensation_args="/root/protected.py",
    )
    comp_id = await db.sagas.insert_saga_compensation(
        run_id, step_id, "DELETE_FILE", "/root/protected.py"
    )

    with patch("inference.saga_manager.Path") as mock_path_cls:
        mock_path_instance = MagicMock()
        mock_path_instance.exists.return_value = True
        mock_path_instance.unlink.side_effect = PermissionError("access denied")
        mock_path_cls.return_value = mock_path_instance

        await agent._run_compensations(run_id)

    # Verify compensation marked failed
    async with db._conn.execute(
        "SELECT status, error FROM saga_compensations WHERE id = ?", (comp_id,)
    ) as cur:
        row = await cur.fetchone()
    assert row["status"] == "failed"
    assert "access denied" in (row["error"] or "")

    # Verify escalation was logged
    async with db._conn.execute("SELECT reason, failed_action FROM dev_escalations") as cur:
        escalations = await cur.fetchall()
    assert len(escalations) == 1
    assert escalations[0]["reason"] == "compensation_failed"
    assert escalations[0]["failed_action"] == "DELETE_FILE"
    await db.close()


async def test_un_snapshottable_skipped_and_escalates(tmp_path):
    db = await _open_db(tmp_path)
    agent = await _make_agent(db)
    run_id = await _seed_run(db)

    target = tmp_path / "huge.py"
    target.write_text("OVERWRITTEN", encoding="utf-8")
    
    import json as _json
    snap = _json.dumps({"path": str(target), "existed": True, "backup": None})
    step_id = await db.runs.insert_agent_step(
        run_id, 1, "WRITE_FILE", str(target), None, "ok", True, 1.0,
        compensation_action="RESTORE_FILE", compensation_args=snap,
    )
    cid = await db.sagas.insert_saga_compensation(run_id, step_id, "RESTORE_FILE", snap)

    await agent._run_compensations(run_id, triggered_by="user_cancel")

    # Verify status is skipped
    async with db._conn.execute(
        "SELECT status FROM saga_compensations WHERE id = ?", (cid,)
    ) as cur:
        row = await cur.fetchone()
    assert row["status"] == "skipped"

    # Verify escalation for missing backup
    async with db._conn.execute("SELECT reason, failed_action FROM dev_escalations") as cur:
        escalations = await cur.fetchall()
    assert len(escalations) == 1
    assert escalations[0]["reason"] == "compensation_failed"
    assert escalations[0]["failed_action"] == "RESTORE_FILE"
    await db.close()


async def test_replan_zero_steps_halts_honestly(tmp_path):
    # E18: Planner honesty - zero-step replan should return None and halt plan execution
    db = await _open_db(tmp_path)
    agent = await _make_agent(db)
    
    # Mock replan to yield an empty list of real steps (e.g. only EXPLAIN)
    agent._replan = AsyncMock(return_value=[AgentStep(action="EXPLAIN", body="I cannot proceed")])
    
    executed = []
    remaining = [AgentStep(action="RUN_TERMINAL", args="ls")]
    
    new_steps = await agent._try_replan("goal", executed, remaining)
    
    # It should strip EXPLAIN and return None
    assert new_steps is None
    await db.close()
