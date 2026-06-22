"""Self-skilling (rung 2 / macros) — commit 1: DB-layer helpers.

Covers the candidate `kind` filter and the successful-runs-with-steps trajectory
reader that MacroDetector (commit 2) mines. No schema change — these reuse the
existing self_evolution_candidates / agent_runs / agent_steps tables.

Criterion refs are to specs/self-skilling/requirements.md.
"""
import os
import tempfile
import time

from storage.db import AgentDB


async def _open_db():
    d = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    db = AgentDB()
    await db.open(os.path.join(d.name, "agent.db"))
    return db, d


async def _add_run(db, *, goal, success, steps, domain="command"):
    """Insert a run plus its ordered steps; step_count matches len(steps)."""
    run_id = await db.insert_agent_run(
        None, goal, domain, "llama3.1:8b", len(steps), success, 12.0,
    )
    for i, (action, args) in enumerate(steps):
        await db.insert_agent_step(
            run_id, i, action, args, None, "ok", True, 1.0,
        )
    return run_id


# ---------------------------------------------------------------------- #
# Candidate kind filter (R1.4 — macros reuse the staging table)
# ---------------------------------------------------------------------- #

async def test_macro_insert_is_idempotent_R1_4():
    db, d = await _open_db()
    try:
        sig = "OPEN(app) → CLICK(target) → TYPE(text)"
        id1 = await db.insert_evolution_candidate(
            "macro", "open scheduler and start a note", sig, domain="command",
            reason="recurring_plan", source_refs='{"run_ids":[1,2,3,4]}',
        )
        id2 = await db.insert_evolution_candidate(
            "macro", "open scheduler and start a note", sig, domain="command",
        )
        assert id1 and id1 == id2, "re-detecting the same macro must not duplicate"
        rows = await db.get_evolution_candidates(status="proposed", kind="macro")
        assert len(rows) == 1
    finally:
        await db.close()
        d.cleanup()


async def test_get_candidates_kind_filter_isolates_macros():
    db, d = await _open_db()
    try:
        await db.insert_evolution_candidate(
            "macro", "do the thing", "OPEN(app) → CLICK(x)", domain="command",
        )
        await db.insert_evolution_candidate(
            "example", "scroll down", "SCROLL down", domain="command",
        )
        macros = await db.get_evolution_candidates(status="proposed", kind="macro")
        assert [c["kind"] for c in macros] == ["macro"]
        # kind=None preserves the legacy "all kinds" behavior
        every = await db.get_evolution_candidates(status="proposed")
        assert {c["kind"] for c in every} == {"macro", "example"}
    finally:
        await db.close()
        d.cleanup()


# ---------------------------------------------------------------------- #
# Trajectory reader (feeds MacroDetector — spec §2.1)
# ---------------------------------------------------------------------- #

async def test_runs_reader_returns_only_successful_multistep_ordered():
    db, d = await _open_db()
    try:
        good = await _add_run(
            db, goal="open and type", success=True,
            steps=[("OPEN", "app=notepad"), ("CLICK", "target=body"),
                   ("TYPE", "text=hi")],
        )
        await _add_run(db, goal="failed one", success=False,
                       steps=[("OPEN", "app=x"), ("CLICK", "target=y")])
        await _add_run(db, goal="trivial", success=True,
                       steps=[("CLICK", "target=z")])  # 1 step → excluded

        runs = await db.get_successful_runs_with_steps(min_steps=2)
        assert [r["id"] for r in runs] == [good], "only the successful 2+ step run"
        steps = runs[0]["steps"]
        assert [s["step_num"] for s in steps] == [0, 1, 2], "ordered by step_num"
        assert [s["action"] for s in steps] == ["OPEN", "CLICK", "TYPE"]
    finally:
        await db.close()
        d.cleanup()


async def test_runs_reader_min_steps_threshold():
    db, d = await _open_db()
    try:
        await _add_run(db, goal="two step", success=True,
                       steps=[("OPEN", "a"), ("CLICK", "b")])
        assert len(await db.get_successful_runs_with_steps(min_steps=2)) == 1
        assert len(await db.get_successful_runs_with_steps(min_steps=3)) == 0
    finally:
        await db.close()
        d.cleanup()


async def test_runs_reader_since_filter():
    db, d = await _open_db()
    try:
        await _add_run(db, goal="recent", success=True,
                       steps=[("OPEN", "a"), ("CLICK", "b")])
        # a future watermark excludes everything already recorded
        assert await db.get_successful_runs_with_steps(since=time.time() + 1000) == []
        assert len(await db.get_successful_runs_with_steps(since=0.0)) == 1
    finally:
        await db.close()
        d.cleanup()
