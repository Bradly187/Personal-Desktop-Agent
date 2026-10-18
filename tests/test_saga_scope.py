"""Phase 1 saga compensation scope-denial regressions."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.execution_policy import ExecutionScope
from inference.dev_agent import DevAgent
from storage.db import AgentDB


async def _open_db(tmp_path: Path) -> AgentDB:
    db = AgentDB()
    await db.open(tmp_path / "saga_scope.db")
    return db


@pytest.mark.parametrize("compensation_action", ["DELETE_FILE", "RESTORE_FILE"])
async def test_out_of_scope_compensation_fails_closed_and_escalates(
    tmp_path, compensation_action
):
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    target = outside / "must-remain.py"
    target.write_text("keep", encoding="utf-8")

    scope = ExecutionScope(allowed, [allowed])
    db = await _open_db(tmp_path)
    agent = DevAgent(
        router=MagicMock(), agent_db=db, execution_scope=scope
    )
    await db.sessions.insert_session(mode="test")
    run_id = await db.runs.start_agent_run("scope rollback", "plan", "model")

    comp_args = str(target)
    if compensation_action == "RESTORE_FILE":
        comp_args = json.dumps({
            "path": str(target), "existed": False, "backup": None,
        })
    step_id = await db.runs.insert_agent_step(
        run_id, 1, "WRITE_FILE", str(target), None, "ok", True, 1.0,
        compensation_action=compensation_action,
        compensation_args=comp_args,
    )
    comp_id = await db.sagas.insert_saga_compensation(
        run_id, step_id, compensation_action, comp_args
    )

    incomplete = await agent._run_compensations(
        run_id, triggered_by="user_cancel"
    )

    assert incomplete == 1
    assert target.read_text(encoding="utf-8") == "keep"
    async with db._conn.execute(
        "SELECT status, error FROM saga_compensations WHERE id = ?", (comp_id,)
    ) as cursor:
        row = await cursor.fetchone()
    assert row["status"] == "failed"
    assert "outside" in row["error"]
    assert agent._rollback_summary["incomplete"] == 1

    async with db._conn.execute(
        "SELECT reason, failed_action FROM dev_escalations"
    ) as cursor:
        escalations = await cursor.fetchall()
    assert len(escalations) == 1
    assert escalations[0]["reason"] == "compensation_failed"
    assert escalations[0]["failed_action"] == compensation_action
    await db.close()


async def test_delete_compensation_authorization_and_filesystem_run_off_loop(tmp_path):
    import threading

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    target = allowed / "delete-me.py"
    target.write_text("temporary", encoding="utf-8")
    scope = ExecutionScope(allowed, [allowed])
    original_authorize = scope.authorize_path
    loop_thread = threading.get_ident()
    worker_threads = []

    def authorize(path, *, operation):
        worker_threads.append(threading.get_ident())
        return original_authorize(path, operation=operation)

    scope.authorize_path = authorize
    db = await _open_db(tmp_path)
    try:
        agent = DevAgent(router=MagicMock(), agent_db=db, execution_scope=scope)
        await db.sessions.insert_session(mode="test")
        run_id = await db.runs.start_agent_run("scope rollback", "plan", "model")
        step_id = await db.runs.insert_agent_step(
            run_id, 1, "WRITE_FILE", str(target), None, "ok", True, 1.0,
            compensation_action="DELETE_FILE", compensation_args=str(target),
        )
        await db.sagas.insert_saga_compensation(
            run_id, step_id, "DELETE_FILE", str(target)
        )

        incomplete = await agent._run_compensations(run_id)

        assert incomplete == 0
        assert not target.exists()
        assert worker_threads and worker_threads[0] != loop_thread
    finally:
        await db.close()
