"""Self-skilling (rung 2 / macros) — commit 1: DB-layer helpers.

Covers the candidate `kind` filter and the successful-runs-with-steps trajectory
reader that MacroDetector (commit 2) mines. No schema change — these reuse the
existing self_evolution_candidates / agent_runs / agent_steps tables.

Criterion refs are to specs/self-skilling/requirements.md.
"""
import json
import os
import tempfile
import time

from storage.db import AgentDB
from adaptive.macro_detector import (
    MacroDetector,
    classify_arg,
    plan_signature,
)


_VERBS = {"OPEN", "CLICK", "TYPE", "HOTKEY", "SCROLL", "WRITE_FILE", "RUN_TERMINAL"}


def _det(db, **kw):
    """A detector with the test verb set and exact (similarity=1.0) clustering
    unless a test overrides it."""
    kw.setdefault("known_verbs", set(_VERBS))
    kw.setdefault("similarity", 1.0)
    kw.setdefault("min_occurrences", 3)
    return MacroDetector(db, **kw)


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


# ---------------------------------------------------------------------- #
# MacroDetector (spec §3 Requirement 1)
# ---------------------------------------------------------------------- #

class _FakeTwin:
    def __init__(self, pain_day_active=False):
        self._p = pain_day_active

    async def get_snapshot(self):
        class _S:
            pain_day_active = self._p
        return _S()


def test_canonicalize_abstracts_literals():
    assert classify_arg(None) == "∅"
    assert classify_arg("https://x.com") == "URL"
    assert classify_arg("src/foo.py") == "PATH"
    assert classify_arg("notepad.exe") == "PATH"
    assert classify_arg("42") == "INT"
    assert classify_arg("notepad") == "STR"
    sig = plan_signature([{"action": "OPEN", "args": "chrome"},
                          {"action": "click", "args": "menu"}])
    assert sig == "OPEN(STR) → CLICK(STR)"


async def _add_n(db, n, goal, steps):
    for _ in range(n):
        await _add_run(db, goal=goal, success=True, steps=steps)


async def test_detects_recurring_plan_R1_1_R1_2():
    db, d = await _open_db()
    try:
        # 3 runs, same shape, different args → one OPEN(STR)→CLICK(STR)→TYPE(STR)
        await _add_run(db, goal="start a note", success=True,
                       steps=[("OPEN", "notepad"), ("CLICK", "body"), ("TYPE", "hi")])
        await _add_run(db, goal="start a note", success=True,
                       steps=[("OPEN", "chrome"), ("CLICK", "bar"), ("TYPE", "yo")])
        await _add_run(db, goal="start a note", success=True,
                       steps=[("OPEN", "vscode"), ("CLICK", "panel"), ("TYPE", "ok")])
        staged = await _det(db).run_once()
        assert len(staged) == 1
        rows = await db.get_evolution_candidates(status="proposed", kind="macro")
        assert len(rows) == 1
        assert rows[0]["action_or_wrong"] == "OPEN(STR) → CLICK(STR) → TYPE(STR)"
        refs = json.loads(rows[0]["source_refs"])
        assert refs["occurrences"] == 3 and len(refs["run_ids"]) == 3
    finally:
        await db.close()
        d.cleanup()


async def test_below_threshold_not_staged():
    db, d = await _open_db()
    try:
        await _add_n(db, 2, "rare", [("OPEN", "a"), ("CLICK", "b")])
        assert await _det(db, min_occurrences=3).run_once() == []
    finally:
        await db.close()
        d.cleanup()


async def test_skips_when_referenced_tool_missing_R1_3():
    db, d = await _open_db()
    try:
        await _add_n(db, 3, "uses unknown verb",
                     [("OPEN", "a"), ("FROBNICATE", "b")])
        assert await _det(db).run_once() == [], "a missing verb must block staging"
        assert await db.get_evolution_candidates(status="proposed", kind="macro") == []
    finally:
        await db.close()
        d.cleanup()


async def test_restage_is_idempotent_R1_4():
    db, d = await _open_db()
    try:
        await _add_n(db, 3, "repeat", [("OPEN", "a"), ("CLICK", "b")])
        det = _det(db)
        first = await det.run_once()
        second = await det.run_once()
        assert first == second, "re-detecting the same macro returns the same row"
        assert len(await db.get_evolution_candidates(status="proposed", kind="macro")) == 1
    finally:
        await db.close()
        d.cleanup()


async def test_flare_skips_run_R1_5():
    db, d = await _open_db()
    try:
        await _add_n(db, 4, "flare day", [("OPEN", "a"), ("CLICK", "b")])
        det = _det(db, twin_state=_FakeTwin(pain_day_active=True))
        assert await det.run_once() == []
        assert await db.get_evolution_candidates(status="proposed", kind="macro") == []
        # same data, no flare → detected
        ok = _det(db, twin_state=_FakeTwin(pain_day_active=False))
        assert len(await ok.run_once()) == 1
    finally:
        await db.close()
        d.cleanup()


async def test_fuzzy_merge_single_slot_difference():
    db, d = await _open_db()
    try:
        # two shapes differing only in step-1's slot type (STR vs PATH)
        await _add_n(db, 2, "merge me", [("OPEN", "notepad"), ("CLICK", "body")])
        await _add_n(db, 2, "merge me", [("OPEN", "src/x.py"), ("CLICK", "body")])
        # exact clustering: two clusters of 2 → neither meets min_occ=3
        assert await _det(db, similarity=1.0, min_occurrences=3).run_once() == []
        # fuzzy: the single-slot diff merges them into one cluster of 4
        staged = await _det(db, similarity=0.5, min_occurrences=3).run_once()
        assert len(staged) == 1
    finally:
        await db.close()
        d.cleanup()
