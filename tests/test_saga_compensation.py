"""Tests for DevAgent saga compensation logic.

Covers:
  - _compensation_for(): verb → (action, args) mapping
  - _run_compensations(): DELETE_FILE actually removes the file
  - _run_compensations(): REVERT_TERMINAL logs and marks done (no file ops)
  - _run_compensations(): handles empty pending list
  - _run_compensations(): handles DELETE_FILE for non-existent path (no crash)
  - _run_compensations(): compensation failure is caught, row marked 'failed'
  - _persist_step(): registers saga row on successful reversible step
  - _persist_step(): no saga row for non-reversible verbs
  - _persist_step(): no saga row when step failed
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from inference.dev_agent import AgentStep, DevAgent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_step(action: str, args: str = "", body: str = "",
               success: bool = True, result: str = "ok") -> AgentStep:
    s = AgentStep(action=action, args=args, body=body)
    s.success = success
    s.result = result
    s.latency_ms = 1.0
    return s


async def _open_db(tmp_path):
    from storage.db import AgentDB
    db = AgentDB()
    await db.open(tmp_path / "saga.db")
    return db


async def _make_agent(db) -> DevAgent:
    router = MagicMock()
    agent = DevAgent(router=router, agent_db=db)
    return agent


async def _seed_run(db) -> int:
    await db.insert_session(mode="test")
    return await db.start_agent_run("saga test", "plan", "model")


# ---------------------------------------------------------------------------
# _compensation_for
# ---------------------------------------------------------------------------

def test_compensation_write_file():
    step = _make_step("WRITE_FILE", args="/tmp/hello.py")
    action, args = DevAgent._compensation_for(step)
    assert action == "DELETE_FILE"
    assert args == "/tmp/hello.py"


def test_compensation_write_file_strips_whitespace():
    step = _make_step("WRITE_FILE", args="  /tmp/hello.py  ")
    _, args = DevAgent._compensation_for(step)
    assert args == "/tmp/hello.py"


def test_compensation_run_terminal():
    step = _make_step("RUN_TERMINAL", args="git commit -m 'msg'")
    action, args = DevAgent._compensation_for(step)
    assert action == "REVERT_TERMINAL"
    assert args == "git commit -m 'msg'"


def test_compensation_run_terminal_falls_back_to_body():
    step = _make_step("RUN_TERMINAL", args="", body="echo hi")
    action, args = DevAgent._compensation_for(step)
    assert action == "REVERT_TERMINAL"
    assert args == "echo hi"


def test_compensation_explain_is_none():
    step = _make_step("EXPLAIN", body="some explanation")
    action, args = DevAgent._compensation_for(step)
    assert action is None
    assert args is None


@pytest.mark.parametrize("verb", ["READ_SCREEN", "SEARCH_WEB", "READ_FILE",
                                   "GREP", "GIT_DIFF", "CLICK", "TYPE"])
def test_compensation_non_reversible_verbs(verb):
    step = _make_step(verb, args="whatever")
    action, args = DevAgent._compensation_for(step)
    assert action is None


# ---------------------------------------------------------------------------
# _run_compensations: DELETE_FILE
# ---------------------------------------------------------------------------

async def test_run_compensations_deletes_file(tmp_path):
    db = await _open_db(tmp_path)
    agent = await _make_agent(db)
    run_id = await _seed_run(db)

    target = tmp_path / "created.py"
    target.write_text("content")
    assert target.exists()

    step_id = await db.insert_agent_step(
        run_id, 1, "WRITE_FILE", str(target), None, "ok", True, 1.0,
        compensation_action="DELETE_FILE", compensation_args=str(target),
    )
    await db.insert_saga_compensation(run_id, step_id, "DELETE_FILE", str(target))

    await agent._run_compensations(run_id)

    assert not target.exists()

    pending = await db.get_pending_compensations(run_id)
    assert pending == []  # all compensated

    async with db._conn.execute(
        "SELECT status FROM saga_compensations WHERE run_id = ?", (run_id,)
    ) as cur:
        rows = await cur.fetchall()
    assert all(r["status"] == "done" for r in rows)
    await db.close()


async def test_run_compensations_delete_file_nonexistent_path(tmp_path):
    db = await _open_db(tmp_path)
    agent = await _make_agent(db)
    run_id = await _seed_run(db)

    ghost_path = str(tmp_path / "ghost.py")  # does NOT exist
    step_id = await db.insert_agent_step(
        run_id, 1, "WRITE_FILE", ghost_path, None, "ok", True, 1.0,
        compensation_action="DELETE_FILE", compensation_args=ghost_path,
    )
    comp_id = await db.insert_saga_compensation(run_id, step_id, "DELETE_FILE", ghost_path)

    # Should not raise even though file doesn't exist
    await agent._run_compensations(run_id)

    async with db._conn.execute(
        "SELECT status FROM saga_compensations WHERE id = ?", (comp_id,)
    ) as cur:
        row = await cur.fetchone()
    assert row["status"] == "done"
    await db.close()


# ---------------------------------------------------------------------------
# _run_compensations: REVERT_TERMINAL
# ---------------------------------------------------------------------------

async def test_run_compensations_revert_terminal_marks_done(tmp_path):
    db = await _open_db(tmp_path)
    agent = await _make_agent(db)
    run_id = await _seed_run(db)

    step_id = await db.insert_agent_step(
        run_id, 1, "RUN_TERMINAL", "rm -rf important", None, "ok", True, 1.0,
        compensation_action="REVERT_TERMINAL", compensation_args="rm -rf important",
    )
    comp_id = await db.insert_saga_compensation(
        run_id, step_id, "REVERT_TERMINAL", "rm -rf important"
    )

    await agent._run_compensations(run_id)

    async with db._conn.execute(
        "SELECT status FROM saga_compensations WHERE id = ?", (comp_id,)
    ) as cur:
        row = await cur.fetchone()
    assert row["status"] == "done"  # logged warning + marked done
    await db.close()


# ---------------------------------------------------------------------------
# _run_compensations: edge cases
# ---------------------------------------------------------------------------

async def test_run_compensations_empty_list_is_noop(tmp_path):
    db = await _open_db(tmp_path)
    agent = await _make_agent(db)
    run_id = await _seed_run(db)
    # No compensations registered — should complete without error
    await agent._run_compensations(run_id)
    await db.close()


async def test_run_compensations_compensation_failure_marks_failed(tmp_path):
    db = await _open_db(tmp_path)
    agent = await _make_agent(db)
    run_id = await _seed_run(db)

    step_id = await db.insert_agent_step(
        run_id, 1, "WRITE_FILE", "/root/protected.py", None, "ok", True, 1.0,
        compensation_action="DELETE_FILE", compensation_args="/root/protected.py",
    )
    comp_id = await db.insert_saga_compensation(
        run_id, step_id, "DELETE_FILE", "/root/protected.py"
    )

    # Sabotage Path.unlink to raise PermissionError
    with patch("inference.dev_agent.Path") as mock_path_cls:
        mock_path_instance = MagicMock()
        mock_path_instance.exists.return_value = True
        mock_path_instance.unlink.side_effect = PermissionError("access denied")
        mock_path_cls.return_value = mock_path_instance

        await agent._run_compensations(run_id)

    async with db._conn.execute(
        "SELECT status, error FROM saga_compensations WHERE id = ?", (comp_id,)
    ) as cur:
        row = await cur.fetchone()
    assert row["status"] == "failed"
    assert "access denied" in (row["error"] or "")
    await db.close()


async def test_run_compensations_no_db_is_noop(tmp_path):
    db = await _open_db(tmp_path)
    agent = DevAgent(router=MagicMock())  # no agent_db
    # Must not raise even with no DB
    await agent._run_compensations(run_id=1)
    await db.close()


# ---------------------------------------------------------------------------
# _persist_step integration: saga registration
# ---------------------------------------------------------------------------

async def test_persist_step_registers_saga_for_successful_reversible_step(tmp_path):
    db = await _open_db(tmp_path)
    agent = await _make_agent(db)
    run_id = await _seed_run(db)

    step = _make_step("WRITE_FILE", args="/tmp/out.py", success=True)
    await agent._persist_step(run_id, 1, step)

    compensations = await db.get_pending_compensations(run_id)
    assert len(compensations) == 1
    assert compensations[0]["compensation_action"] == "DELETE_FILE"
    await db.close()


async def test_persist_step_no_saga_for_failed_step(tmp_path):
    db = await _open_db(tmp_path)
    agent = await _make_agent(db)
    run_id = await _seed_run(db)

    step = _make_step("WRITE_FILE", args="/tmp/out.py", success=False)
    await agent._persist_step(run_id, 1, step)

    compensations = await db.get_pending_compensations(run_id)
    assert len(compensations) == 0
    await db.close()


async def test_persist_step_no_saga_for_non_reversible_verb(tmp_path):
    db = await _open_db(tmp_path)
    agent = await _make_agent(db)
    run_id = await _seed_run(db)

    step = _make_step("EXPLAIN", body="some explanation", success=True)
    await agent._persist_step(run_id, 1, step)

    compensations = await db.get_pending_compensations(run_id)
    assert len(compensations) == 0
    await db.close()
