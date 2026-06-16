"""PR 3 (R-3) — SelfEvolutionPipeline: mine→stage (approval default), eval-gated
auto-promote, fail/skip revert, manual promote.
"""
import os
import tempfile
import time

from storage.db import AgentDB
from adaptive.self_evolution import SelfEvolutionPipeline


async def _open():
    d = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    db = AgentDB()
    await db.open(os.path.join(d.name, "agent.db"))
    return db, d


async def _add_correction(db, text, wrong, right):
    await db._conn.execute(
        "INSERT INTO commands (session_id, ts, source, text, action, corrected_to, success)"
        " VALUES (1, ?, 'voice', ?, ?, ?, 0)",
        (time.time(), text, wrong, right),
    )
    await db._conn.commit()


async def _count(db, table):
    async with db._conn.execute(f"SELECT COUNT(*) FROM {table}") as cur:
        return (await cur.fetchone())[0]


async def test_default_only_stages_no_apply():
    db, d = await _open()
    try:
        await _add_correction(db, "open the editor", "OPEN editor", "OPEN vscode")
        pipe = SelfEvolutionPipeline(db, auto_promote=False)
        report = await pipe.run()
        assert report["mined"] >= 2          # example + counterexample
        assert report["staged"] >= 2
        assert report["promoted"] == []
        # nothing applied to the active tables
        assert await _count(db, "few_shot_examples") == 0
        assert await _count(db, "few_shot_counterexamples") == 0
        # candidates are 'proposed'
        pending = await db.get_evolution_candidates("proposed")
        assert len(pending) >= 2
    finally:
        await db.close()
        d.cleanup()


async def test_auto_promote_on_pass_applies_and_logs():
    db, d = await _open()
    try:
        await _add_correction(db, "scroll the page", "SCROLL up", "SCROLL down")

        async def gate():
            return ("pass", 0.02)

        pipe = SelfEvolutionPipeline(db, auto_promote=True, eval_gate=gate)
        report = await pipe.run()
        assert report["verdict"] == "pass"
        assert report["promoted"]
        # applied to active tables
        assert await _count(db, "few_shot_examples") >= 1
        assert await _count(db, "few_shot_counterexamples") >= 1
        # adaptation_log records the self_evolve promotion
        async with db._conn.execute(
            "SELECT COUNT(*) FROM adaptation_log WHERE component='self_evolve'"
        ) as cur:
            assert (await cur.fetchone())[0] == 1
        assert await db.get_evolution_candidates("proposed") == []
        assert len(await db.get_evolution_candidates("promoted")) >= 1
    finally:
        await db.close()
        d.cleanup()


async def test_auto_promote_on_fail_reverts_and_rejects():
    db, d = await _open()
    try:
        await _add_correction(db, "close the window", "CLOSE all", "CLOSE active")

        async def gate():
            return ("fail", 0.0)

        pipe = SelfEvolutionPipeline(db, auto_promote=True, eval_gate=gate)
        report = await pipe.run()
        assert report["verdict"] == "fail"
        assert report["promoted"] == []
        # reverted — active tables empty again
        assert await _count(db, "few_shot_examples") == 0
        assert await _count(db, "few_shot_counterexamples") == 0
        assert len(await db.get_evolution_candidates("rejected")) >= 1
    finally:
        await db.close()
        d.cleanup()


async def test_auto_promote_on_skip_reverts_keeps_proposed():
    db, d = await _open()
    try:
        await _add_correction(db, "type my name", "TYPE Bob", "TYPE Brad")

        async def gate():
            return ("skip", 0.0)

        pipe = SelfEvolutionPipeline(db, auto_promote=True, eval_gate=gate)
        report = await pipe.run()
        assert report["verdict"] == "skip"
        assert await _count(db, "few_shot_examples") == 0
        # unverifiable → left for review
        assert len(await db.get_evolution_candidates("proposed")) >= 1
    finally:
        await db.close()
        d.cleanup()


async def test_manual_promote_path():
    db, d = await _open()
    try:
        await _add_correction(db, "open mail", "OPEN outlook", "OPEN gmail")
        pipe = SelfEvolutionPipeline(db, auto_promote=False)
        await pipe.run()
        pending = await pipe.list_pending()
        example = next(c for c in pending if c["kind"] == "example")
        assert await pipe.promote(example["id"]) is True
        assert await _count(db, "few_shot_examples") == 1
        assert len(await db.get_evolution_candidates("promoted")) == 1
    finally:
        await db.close()
        d.cleanup()


async def test_noop_correction_makes_no_counterexample():
    db, d = await _open()
    try:
        # wrong == right: no counterexample should be synthesized
        await _add_correction(db, "click ok", "CLICK ok", "CLICK ok")
        pipe = SelfEvolutionPipeline(db, auto_promote=False)
        await pipe.run()
        kinds = [c["kind"] for c in await db.get_evolution_candidates("proposed")]
        assert "counterexample" not in kinds
    finally:
        await db.close()
        d.cleanup()


async def test_escalation_mined_as_plan_counterexample():
    db, d = await _open()
    try:
        await db.insert_escalation(
            run_id=-1, goal="refactor the parser module", reason="max_replans",
            failed_action="RUN_TERMINAL", replans=2,
        )
        pipe = SelfEvolutionPipeline(db, auto_promote=False)
        await pipe.run()
        plan_cands = [c for c in await db.get_evolution_candidates("proposed")
                      if c["domain"] == "plan"]
        assert plan_cands and plan_cands[0]["kind"] == "counterexample"
    finally:
        await db.close()
        d.cleanup()
