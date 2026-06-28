"""Tests for WorkflowRunner.pipeline — per-item staged mode (Requirement 7).

Spec: specs/workflow-orchestration/requirements.md §3 R7. No real model — a fake
router records every (domain, context, prompt) so we can assert that stages run in
order, that each stage's PROMPT carries the prior stage's OUTPUT text (threaded via
prompt, never context — AGENTS.md #6), per-item fail-closed isolation, and that the
run journals with mode="pipeline" against a real temp AgentDB (additive, no
user_version bump).
"""

from __future__ import annotations

import pytest

from inference.workflow import Stage, WorkflowRunner
from storage.db import AgentDB, _AGENT_DB_SCHEMA_VERSION

pytestmark = pytest.mark.asyncio


class _Res:
    def __init__(self, text="ok", ok=True):
        self.text = text
        self.ok = ok
        self.error = "" if ok else "boom"


class _FakeRouter:
    """Echoes the prompt so stage-to-stage text threading is observable.

    A normal stage call returns ``out:<prompt>``. A prompt containing ``BOOM``
    raises (to exercise per-item fail-closed). A verify prompt ("strict reviewer")
    answers YES only when the candidate text contains a token in ``verify_yes_for``.
    """

    def __init__(self, *, verify_yes_for=("good",)):
        self.calls = []
        self.loaded = []                 # any model-load would append here
        self._verify_yes_for = verify_yes_for

    async def infer(self, domain, user_text, context):
        self.calls.append({"domain": domain, "context": context, "text": user_text})
        if "strict reviewer" in user_text:
            yes = any(tok in user_text for tok in self._verify_yes_for)
            return _Res("YES" if yes else "NO")
        if "BOOM" in user_text:
            raise RuntimeError("infer failed (BOOM)")
        return _Res(f"out:{user_text}")


# Three-stage refinement chain: each later stage builds from the prior stage text.
def _chain():
    return [
        Stage("s1", lambda item, prior: f"S1 about {item}"),
        Stage("s2", lambda item, prior: f"S2 refine: {prior[-1]}"),
        Stage("s3", lambda item, prior: f"S3 finalize: {prior[-1]}"),
    ]


# --- R7.1: stages run in order; prompt k carries output k-1; final text is result -

async def test_stages_run_in_order_and_thread_prior_text():
    r = _FakeRouter()
    wf = WorkflowRunner(r, enabled=True)
    res = await wf.pipeline(["X"], _chain(), name="p")
    assert res.status == "completed" and res.success_count == 1
    # One item → its three stage calls are sequential and in order.
    prompts = [c["text"] for c in r.calls]
    assert prompts[0] == "S1 about X"
    # Stage 2's prompt contains stage 1's OUTPUT ("out:S1 about X"), not just input.
    assert "out:S1 about X" in prompts[1]
    # Stage 3's prompt contains stage 2's OUTPUT.
    assert "out:S2 refine: out:S1 about X" in prompts[2]


async def test_final_stage_text_is_the_item_result():
    r = _FakeRouter()
    wf = WorkflowRunner(r, enabled=True)
    res = await wf.pipeline(["X"], _chain())
    # The public result text is the LAST stage's output.
    assert res.results[0].text == "out:S3 finalize: out:S2 refine: out:S1 about X"
    assert res.results[0].name == "item0"


# --- R7.2/R7.3: per-item isolation, fail-closed (skip remaining stages) ----------

async def test_per_item_isolation_fail_closed():
    r = _FakeRouter()
    wf = WorkflowRunner(r, enabled=True)
    res = await wf.pipeline(["X", "BOOM", "Y"], _chain(), name="p")
    assert res.subtask_count == 3 and res.success_count == 2
    by_name = {sr.name: sr for sr in res.results}
    # The bad item failed closed; siblings completed all three stages.
    assert by_name["item1"].ok is False and "BOOM" in by_name["item1"].error
    assert by_name["item0"].ok is True and by_name["item2"].ok is True
    # Fail-closed: the bad item only issued its FIRST stage (no s2/s3 for it).
    # 2 good items * 3 stages + 1 bad item * 1 stage = 7 inference calls.
    assert len(r.calls) == 7


# --- R7.1: fresh context, no model loaded (AGENTS.md #6) --------------------------

async def test_stages_use_fresh_context_and_load_no_model():
    r = _FakeRouter()
    wf = WorkflowRunner(r, enabled=True)
    await wf.pipeline(["X", "Y"], _chain())
    # Prior stage text rides the PROMPT, never the context — every call context="".
    assert r.calls and all(c["context"] == "" for c in r.calls)
    assert r.loaded == []


# --- R7.4: OFF-default / flare / empty envelopes ---------------------------------

async def test_disabled_returns_disabled_status():
    wf = WorkflowRunner(_FakeRouter(), enabled=False)
    res = await wf.pipeline(["X"], _chain())
    assert res.status == "disabled" and res.success_count == 0
    # Disabled → issued no inference at all.
    assert wf._router.calls == []


async def test_skips_on_flare():
    wf = WorkflowRunner(_FakeRouter(), enabled=True, flare_check=lambda: True)
    res = await wf.pipeline(["X"], _chain())
    assert res.status == "skipped_flare"
    assert wf._router.calls == []


async def test_flare_check_error_fails_safe_to_skip():
    def boom():
        raise RuntimeError("cant tell")
    wf = WorkflowRunner(_FakeRouter(), enabled=True, flare_check=boom)
    res = await wf.pipeline(["X"], _chain())
    assert res.status == "skipped_flare"


async def test_empty_items_is_completed_noop():
    wf = WorkflowRunner(_FakeRouter(), enabled=True)
    res = await wf.pipeline([], _chain())
    assert res.status == "completed" and res.subtask_count == 0
    assert wf._router.calls == []


async def test_empty_stages_is_completed_noop():
    wf = WorkflowRunner(_FakeRouter(), enabled=True)
    res = await wf.pipeline(["X"], [])
    assert res.status == "completed"
    assert wf._router.calls == []


# --- scheduler integration -------------------------------------------------------

async def test_uses_scheduler_fan_out_when_present():
    class _Sched:
        def __init__(self): self.used = False
        async def fan_out(self, coros, label="", return_exceptions=True):
            self.used = True
            import asyncio
            return await asyncio.gather(*coros, return_exceptions=return_exceptions)
    sched = _Sched()
    wf = WorkflowRunner(_FakeRouter(), enabled=True, scheduler=sched)
    res = await wf.pipeline(["X", "Y"], _chain())
    assert sched.used is True and res.success_count == 2


# --- R7.6: optional adversarial verify reuses the fail-safe judge -----------------

async def test_verify_reuses_failsafe_judge():
    r = _FakeRouter(verify_yes_for=("good",))
    wf = WorkflowRunner(r, enabled=True)
    res = await wf.pipeline(
        ["good answer", "weak one"], _chain(),
        verify_criterion="is it correct?",
    )
    # Final text of the "good answer" item carries "good"; reviewer confirms 1.
    assert res.verified_count == 1
    verified = {sr.name: sr.verified for sr in res.results}
    assert verified == {"item0": True, "item1": False}


async def test_no_verify_leaves_verified_none():
    wf = WorkflowRunner(_FakeRouter(), enabled=True)
    res = await wf.pipeline(["X"], _chain())
    assert res.verified_count is None and res.results[0].verified is None


# --- R7.5: durable ledger — mode="pipeline", additive, no user_version bump -------

async def test_pipeline_run_journaled_with_mode_pipeline(tmp_path):
    db = AgentDB()
    await db.open(tmp_path / "wfp.db")
    try:
        wf = WorkflowRunner(_FakeRouter(), enabled=True, agent_db=db)
        await wf.pipeline(["X", "Y"], _chain(), name="staged", goal="refine")
        cur = await db._conn.execute(
            "SELECT name, mode, subtask_count, success_count, status "
            "FROM agent_workflows")
        rows = [tuple(r) for r in await cur.fetchall()]
        assert rows == [("staged", "pipeline", 2, 2, "completed")]
        # The reserved 'pipeline' mode needed no ALTER — user_version unchanged.
        cur = await db._conn.execute("PRAGMA user_version")
        assert (await cur.fetchone())[0] == _AGENT_DB_SCHEMA_VERSION
    finally:
        await db.close()


async def test_pipeline_flare_skip_journaled(tmp_path):
    db = AgentDB()
    await db.open(tmp_path / "wfp2.db")
    try:
        wf = WorkflowRunner(_FakeRouter(), enabled=True, agent_db=db,
                            flare_check=lambda: True)
        await wf.pipeline(["X"], _chain(), name="staged")
        cur = await db._conn.execute(
            "SELECT mode, status FROM agent_workflows")
        rows = [tuple(r) for r in await cur.fetchall()]
        assert rows == [("pipeline", "skipped_flare")]
    finally:
        await db.close()
