"""Tests for inference/workflow.py — multi-agent orchestration (Phase 4).

Spec: specs/workflow-orchestration/requirements.md. No real model — a fake
router records calls so we can assert the fresh-context (AGENTS.md #6) and
fan-out behavior. The agent_workflows migration is exercised against a real
temp AgentDB to prove it is additive + backward-compatible.
"""

from __future__ import annotations

import pytest

from inference.workflow import SubTask, WorkflowRunner
from storage.db import AgentDB, _AGENT_DB_SCHEMA_VERSION

pytestmark = pytest.mark.asyncio


class _Res:
    def __init__(self, text="ok", ok=True):
        self.text = text
        self.ok = ok
        self.error = "" if ok else "boom"


class _FakeRouter:
    """Records (domain, context, user_text); replies are deterministic by content."""

    def __init__(self, *, fail_names=(), verify_yes_for=("good",)):
        self.calls = []
        self.loaded = []                 # any model-load would append here
        self._fail_names = set(fail_names)
        self._verify_yes_for = verify_yes_for

    async def infer(self, domain, user_text, context):
        self.calls.append({"domain": domain, "context": context, "text": user_text})
        # Verify pass: prompt contains "strict reviewer".
        if "strict reviewer" in user_text:
            yes = any(tok in user_text for tok in self._verify_yes_for)
            return _Res("YES" if yes else "NO")
        # A sub-task whose prompt names a failing task raises.
        for fn in self._fail_names:
            if fn in user_text:
                raise RuntimeError(f"infer failed for {fn}")
        return _Res(f"answer:{user_text[:20]}")


# --- enablement (ship-dark) ---------------------------------------------------

async def test_disabled_returns_disabled_status():
    wf = WorkflowRunner(_FakeRouter(), enabled=False)
    res = await wf.fan_out([SubTask("a", "q")])
    assert res.status == "disabled" and res.success_count == 0


# --- fan-out ------------------------------------------------------------------

async def test_fan_out_runs_all_subtasks():
    r = _FakeRouter()
    wf = WorkflowRunner(r, enabled=True)
    res = await wf.fan_out([SubTask("a", "q1"), SubTask("b", "q2"),
                            SubTask("c", "q3")], name="wf")
    assert res.status == "completed"
    assert res.subtask_count == 3 and res.success_count == 3
    assert {sr.name for sr in res.results} == {"a", "b", "c"}


async def test_fan_out_isolates_a_failed_subtask():
    r = _FakeRouter(fail_names=("BANG",))
    wf = WorkflowRunner(r, enabled=True)
    res = await wf.fan_out([SubTask("ok1", "fine"), SubTask("bad", "BANG here"),
                            SubTask("ok2", "fine2")])
    assert res.success_count == 2
    bad = next(sr for sr in res.results if sr.name == "bad")
    assert bad.ok is False and "failed" in bad.error
    # The other two still completed.
    assert all(sr.ok for sr in res.results if sr.name != "bad")


async def test_empty_subtasks_is_completed_noop():
    wf = WorkflowRunner(_FakeRouter(), enabled=True)
    res = await wf.fan_out([])
    assert res.status == "completed" and res.subtask_count == 0


# --- fresh context / no new VRAM (AGENTS.md #6) -------------------------------

async def test_subagents_use_fresh_context_and_load_no_model():
    r = _FakeRouter()
    wf = WorkflowRunner(r, enabled=True)
    await wf.fan_out([SubTask("a", "q1"), SubTask("b", "q2")])
    # Every inference call passed context="" (no generator trajectory leaked in).
    assert r.calls and all(c["context"] == "" for c in r.calls)
    # No model was loaded by orchestration (reuses the resident model).
    assert r.loaded == []


# --- adversarial verify -------------------------------------------------------

async def test_verify_pass_counts_only_confirmed():
    # The reviewer says YES only when the sub-task prompt contained "good".
    r = _FakeRouter(verify_yes_for=("good",))
    wf = WorkflowRunner(r, enabled=True)
    res = await wf.fan_out(
        [SubTask("g", "a good answer"), SubTask("b", "a weak one")],
        name="wf", verify_criterion="is it correct?",
    )
    assert res.verified_count == 1
    verified = {sr.name: sr.verified for sr in res.results}
    assert verified == {"g": True, "b": False}


async def test_verify_is_failsafe_on_reviewer_error():
    class _Boom(_FakeRouter):
        async def infer(self, domain, user_text, context):
            if "strict reviewer" in user_text:
                raise RuntimeError("reviewer down")
            return _Res("answer")
    wf = WorkflowRunner(_Boom(), enabled=True)
    res = await wf.fan_out([SubTask("a", "q")], verify_criterion="ok?")
    # Reviewer error → NOT verified (fail-safe), never a false confirmation.
    assert res.verified_count == 0 and res.results[0].verified is False


# --- skip-on-flare (AGENTS.md #5) ---------------------------------------------

async def test_skips_on_flare():
    wf = WorkflowRunner(_FakeRouter(), enabled=True, flare_check=lambda: True)
    res = await wf.fan_out([SubTask("a", "q")], name="wf")
    assert res.status == "skipped_flare"


async def test_flare_check_error_fails_safe_to_skip():
    def boom():
        raise RuntimeError("cant tell")
    wf = WorkflowRunner(_FakeRouter(), enabled=True, flare_check=boom)
    res = await wf.fan_out([SubTask("a", "q")])
    assert res.status == "skipped_flare"


# --- scheduler integration ----------------------------------------------------

async def test_uses_scheduler_fan_out_when_present():
    class _Sched:
        def __init__(self): self.used = False
        async def fan_out(self, coros, label="", return_exceptions=True):
            self.used = True
            import asyncio
            return await asyncio.gather(*coros, return_exceptions=return_exceptions)
    sched = _Sched()
    wf = WorkflowRunner(_FakeRouter(), enabled=True, scheduler=sched)
    res = await wf.fan_out([SubTask("a", "q1"), SubTask("b", "q2")])
    assert sched.used is True and res.success_count == 2


# --- DB migration: additive + backward-compatible -----------------------------

async def test_agent_workflows_table_is_additive(tmp_path):
    db = AgentDB()
    await db.open(tmp_path / "wf.db")
    try:
        # The new table exists and a row round-trips.
        wid = await db.workflows.insert_workflow(
            name="research", goal="find X", mode="fan_out",
            subtask_count=4, success_count=3, status="completed",
            verified_count=2, latency_ms=123.4,
        )
        assert wid > 0
        cur = await db._conn.execute(
            "SELECT name, subtask_count, success_count, verified_count, status "
            "FROM agent_workflows WHERE id=?", (wid,))
        row = await cur.fetchone()
        assert tuple(row) == ("research", 4, 3, 2, "completed")
        # The new table did NOT require a user_version bump (it's a CREATE TABLE
        # IF NOT EXISTS, not an ALTER) — existing DBs stay backward-compatible.
        cur = await db._conn.execute("PRAGMA user_version")
        assert (await cur.fetchone())[0] == _AGENT_DB_SCHEMA_VERSION
    finally:
        await db.close()


async def test_workflow_run_is_journaled_to_db(tmp_path):
    db = AgentDB()
    await db.open(tmp_path / "wf2.db")
    try:
        wf = WorkflowRunner(_FakeRouter(), enabled=True, agent_db=db)
        await wf.fan_out([SubTask("a", "q1"), SubTask("b", "q2")], name="journaled")
        cur = await db._conn.execute(
            "SELECT name, subtask_count, success_count, status FROM agent_workflows")
        rows = await cur.fetchall()
        assert [tuple(r) for r in rows] == [("journaled", 2, 2, "completed")]
    finally:
        await db.close()
