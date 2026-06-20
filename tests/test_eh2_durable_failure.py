"""EH-2 — durable-failure integrity on the destructive dev-agent path.

- E4 An escalation the DB can't accept (DB down, or insert_escalation returns
     None) is written to a durable sidecar and reconciled into dev_escalations on
     the next healthy boot — never silently lost, and the TTS flag is only set
     when it was actually persisted.
- E5 A RESTORE_FILE that can't complete (overwritten file, no backup) is recorded
     'skipped' (not a misleading 'done') and counted as incomplete; the count
     rides in the escalation detail.
- E6 A WRITE_FILE that FAILED but captured a pre-write snapshot still registers a
     RESTORE compensation, so a partial write is rolled back.

(Note: E3 'compensation marked done when rollback threw' and E19 'silent DAG
cycle' were found already-correct in the live code and are not changed here.)

Run:
    python -m pytest tests/test_eh2_durable_failure.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from inference.dev_agent import AgentStep, DevAgent


async def _open_db(tmp_path):
    from storage.db import AgentDB
    db = AgentDB()
    await db.open(tmp_path / "eh2.db")
    if not db.available:
        pytest.skip("aiosqlite unavailable")
    return db


def _agent(db):
    return DevAgent(router=MagicMock(), agent_db=db)


async def _seed_run(db) -> int:
    await db.insert_session(mode="test")
    return await db.start_agent_run("eh2 test", "plan", "model")


def _step(action, args="", body="", success=True, comp_args=None) -> AgentStep:
    s = AgentStep(action=action, args=args, body=body)
    s.success = success
    s.result = "ok" if success else "ERROR: boom"
    s.latency_ms = 1.0
    s.comp_args = comp_args
    return s


# ===========================================================================
# E6 — partial-write failure registers a rollback
# ===========================================================================

async def test_failed_write_with_snapshot_registers_restore(tmp_path):
    db = await _open_db(tmp_path)
    run_id = await _seed_run(db)
    agent = _agent(db)

    snap = json.dumps({"path": str(tmp_path / "f.py"), "existed": True,
                       "backup": str(tmp_path / "f.bak")})
    step = _step("WRITE_FILE", args=str(tmp_path / "f.py"), success=False, comp_args=snap)
    await agent._persist_step(run_id, 1, step)

    comps = await db.get_pending_compensations(run_id)
    assert len(comps) == 1
    assert comps[0]["compensation_action"] == "RESTORE_FILE"
    await db.close()


async def test_failed_write_without_snapshot_registers_nothing(tmp_path):
    db = await _open_db(tmp_path)
    run_id = await _seed_run(db)
    agent = _agent(db)

    step = _step("WRITE_FILE", args=str(tmp_path / "f.py"), success=False, comp_args=None)
    await agent._persist_step(run_id, 1, step)

    assert await db.get_pending_compensations(run_id) == []   # nothing to roll back
    await db.close()


# ===========================================================================
# E5 — un-restorable rollback is 'skipped' + counted, surfaced in detail
# ===========================================================================

def test_restore_file_returns_false_when_no_backup(tmp_path):
    target = tmp_path / "huge.py"
    target.write_text("OVERWRITTEN", encoding="utf-8")
    snap = json.dumps({"path": str(target), "existed": True, "backup": None})
    assert DevAgent._restore_file(snap) is False          # could not restore
    assert target.exists()                                # but did NOT delete


def test_restore_file_returns_true_when_restored(tmp_path):
    target = tmp_path / "doc.py"
    target.write_text("ORIGINAL", encoding="utf-8")
    snap = DevAgent._snapshot_for_write(str(target))
    target.write_text("CHANGED", encoding="utf-8")
    assert DevAgent._restore_file(json.dumps(snap)) is True
    assert target.read_text(encoding="utf-8") == "ORIGINAL"


async def test_escalation_detail_carries_incomplete_count(tmp_path):
    db = await _open_db(tmp_path)
    run_id = await _seed_run(db)
    agent = _agent(db)

    await agent._record_escalation(run_id, "goal", "max_replans", "WRITE_FILE", 2,
                                   incomplete=1)
    items = await db.get_pending_escalations()
    assert len(items) == 1
    detail = json.loads(items[0]["detail"])
    assert detail["incomplete_compensations"] == 1
    await db.close()


# ===========================================================================
# E4 — durable escalation sidecar + reconcile
# ===========================================================================

async def test_reconcile_drains_sidecar_into_db(tmp_path):
    db = await _open_db(tmp_path)
    agent = _agent(db)
    sidecar = tmp_path / "esc.jsonl"
    agent._escalation_sidecar_path = sidecar
    # Two escalations stranded from a prior DB-down run.
    rows = [
        {"run_id": -1, "goal": "g1", "reason": "max_steps", "failed_action": None,
         "replans": 0, "detail": "{}", "ts": 1.0},
        {"run_id": -1, "goal": "g2", "reason": "max_replans", "failed_action": "WRITE_FILE",
         "replans": 2, "detail": "{}", "ts": 2.0},
    ]
    sidecar.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    n = await agent.reconcile_pending_escalations()
    assert n == 2
    items = await db.get_pending_escalations()
    assert {i["goal"] for i in items} == {"g1", "g2"}
    assert not sidecar.exists()                            # fully drained → removed
    await db.close()


async def test_reconcile_keeps_rows_that_still_fail(tmp_path):
    db = MagicMock()
    db.available = True
    db.insert_escalation = AsyncMock(return_value=None)    # DB still refusing
    agent = _agent(db)
    sidecar = tmp_path / "esc.jsonl"
    agent._escalation_sidecar_path = sidecar
    sidecar.write_text(json.dumps(
        {"run_id": -1, "goal": "g", "reason": "max_steps", "failed_action": None,
         "replans": 0, "detail": "{}", "ts": 1.0}) + "\n", encoding="utf-8")

    n = await agent.reconcile_pending_escalations()
    assert n == 0
    assert sidecar.exists()                                # kept for next attempt
    assert len(sidecar.read_text("utf-8").splitlines()) == 1


async def test_reconcile_noop_without_sidecar(tmp_path):
    db = await _open_db(tmp_path)
    agent = _agent(db)
    agent._escalation_sidecar_path = tmp_path / "absent.jsonl"
    assert await agent.reconcile_pending_escalations() == 0
    await db.close()


# ===========================================================================
# E3/E5 — a failed/skipped rollback during a USER CANCEL still escalates
# ===========================================================================
# A user cancel is deliberate and does not itself escalate. But the budget-halt
# paths (max_steps / max_replans) already escalate carrying the incomplete count,
# while the user_cancel path previously ran compensations and returned silently —
# so a rollback that FAILED or was SKIPPED during a cancel never reached the
# review queue. These tests pin the fix: an incomplete cancel-rollback escalates
# with reason 'compensation_failed'; a clean one does not.

class _RR:
    """Minimal RouterResult stand-in (ok/text/model/error)."""
    def __init__(self, text, ok=True, model="test-model"):
        self.text = text
        self.ok = ok
        self.model = model
        self.error = None if ok else "err"


def _plan_agent(db, plan_text):
    """DevAgent driven by a fixed two-step plan, side-channels stubbed, real DB."""
    router = MagicMock()
    router.infer = AsyncMock(return_value=_RR(plan_text))
    agent = DevAgent(router=router, agent_db=db)
    agent._approve_plan_upfront = AsyncMock(return_value=True)
    agent._rag_context = AsyncMock(return_value="")
    agent._git_context = AsyncMock(return_value="")
    agent._format_context = lambda: ""
    agent._reflect = AsyncMock(return_value="summary")
    agent._persist_run = AsyncMock()
    agent._speak_plan_completion = AsyncMock()
    return agent


async def test_skipped_rollback_on_user_cancel_escalates(tmp_path):
    db = await _open_db(tmp_path)
    await db.insert_session(mode="test")
    # Two steps so the top-of-loop cancel check fires AFTER step 1 runs.
    agent = _plan_agent(db, "Step 1: [WRITE_FILE out.py]\nStep 2: [EXPLAIN done]")

    target = tmp_path / "out.py"
    target.write_text("ORIGINAL", encoding="utf-8")       # the file EXISTED pre-write

    async def exec_step(step):
        if step.action.upper() == "WRITE_FILE":
            # Simulate the WRITE_FILE handler capturing a snapshot with NO backup
            # (file too large) → rollback is 'skipped', i.e. incomplete.
            step.comp_args = json.dumps(
                {"path": str(target), "existed": True, "backup": None})
            agent.request_cancel()                         # cancel after the destructive step
        return "ok:" + step.action

    agent._execute_step = exec_step
    result = await agent.plan_and_run("write then cancel")

    assert result.success is False                         # cancelled
    assert target.exists()                                 # overwritten file left in place, not deleted
    escs = await db.get_pending_escalations()
    matched = [e for e in escs if e["reason"] == "compensation_failed"]
    assert len(matched) == 1                               # the skipped rollback reached the queue
    assert json.loads(matched[0]["detail"])["incomplete_compensations"] == 1
    await db.close()


async def test_clean_rollback_on_user_cancel_does_not_escalate(tmp_path):
    db = await _open_db(tmp_path)
    await db.insert_session(mode="test")
    agent = _plan_agent(db, "Step 1: [WRITE_FILE new.py]\nStep 2: [EXPLAIN done]")

    target = tmp_path / "new.py"                           # does NOT exist pre-write

    async def exec_step(step):
        if step.action.upper() == "WRITE_FILE":
            target.write_text("CREATED BY PLAN", encoding="utf-8")
            step.comp_args = json.dumps(
                {"path": str(target), "existed": False, "backup": None})
            agent.request_cancel()
        return "ok:" + step.action

    agent._execute_step = exec_step
    result = await agent.plan_and_run("write then cancel")

    assert result.success is False                         # cancelled
    assert not target.exists()                             # plan-created file cleanly rolled back
    escs = await db.get_pending_escalations()
    assert not any(e["reason"] == "compensation_failed" for e in escs)   # clean unwind → no escalation
    await db.close()
