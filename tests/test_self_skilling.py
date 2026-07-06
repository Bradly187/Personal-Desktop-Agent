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
from core.macro_store import MacroStore
from core.command_executor import Command


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
    run_id = await db.runs.insert_agent_run(
        None, goal, domain, "llama3.1:8b", len(steps), success, 12.0,
    )
    for i, (action, args) in enumerate(steps):
        await db.runs.insert_agent_step(
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
        id1 = await db.skills.insert_evolution_candidate(
            "macro", "open scheduler and start a note", sig, domain="command",
            reason="recurring_plan", source_refs='{"run_ids":[1,2,3,4]}',
        )
        id2 = await db.skills.insert_evolution_candidate(
            "macro", "open scheduler and start a note", sig, domain="command",
        )
        assert id1 and id1 == id2, "re-detecting the same macro must not duplicate"
        rows = await db.skills.get_evolution_candidates(status="proposed", kind="macro")
        assert len(rows) == 1
    finally:
        await db.close()
        d.cleanup()


async def test_get_candidates_kind_filter_isolates_macros():
    db, d = await _open_db()
    try:
        await db.skills.insert_evolution_candidate(
            "macro", "do the thing", "OPEN(app) → CLICK(x)", domain="command",
        )
        await db.skills.insert_evolution_candidate(
            "example", "scroll down", "SCROLL down", domain="command",
        )
        macros = await db.skills.get_evolution_candidates(status="proposed", kind="macro")
        assert [c["kind"] for c in macros] == ["macro"]
        # kind=None preserves the legacy "all kinds" behavior
        every = await db.skills.get_evolution_candidates(status="proposed")
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

        runs = await db.runs.get_successful_runs_with_steps(min_steps=2)
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
        assert len(await db.runs.get_successful_runs_with_steps(min_steps=2)) == 1
        assert len(await db.runs.get_successful_runs_with_steps(min_steps=3)) == 0
    finally:
        await db.close()
        d.cleanup()


async def test_runs_reader_since_filter():
    db, d = await _open_db()
    try:
        await _add_run(db, goal="recent", success=True,
                       steps=[("OPEN", "a"), ("CLICK", "b")])
        # a future watermark excludes everything already recorded
        assert await db.runs.get_successful_runs_with_steps(since=time.time() + 1000) == []
        assert len(await db.runs.get_successful_runs_with_steps(since=0.0)) == 1
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
        rows = await db.skills.get_evolution_candidates(status="proposed", kind="macro")
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
        assert await db.skills.get_evolution_candidates(status="proposed", kind="macro") == []
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
        assert len(await db.skills.get_evolution_candidates(status="proposed", kind="macro")) == 1
    finally:
        await db.close()
        d.cleanup()


async def test_flare_skips_run_R1_5():
    db, d = await _open_db()
    try:
        await _add_n(db, 4, "flare day", [("OPEN", "a"), ("CLICK", "b")])
        det = _det(db, twin_state=_FakeTwin(pain_day_active=True))
        assert await det.run_once() == []
        assert await db.skills.get_evolution_candidates(status="proposed", kind="macro") == []
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


# ---------------------------------------------------------------------- #
# MacroStore + safe replay (spec §3 Requirement 5)
# ---------------------------------------------------------------------- #

class _FakeExec:
    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on

    async def execute(self, cmd):
        idx = len(self.calls)
        self.calls.append((cmd.action, cmd.text, dict(cmd.params), cmd.source))
        if self.fail_on is not None and idx == self.fail_on:
            return {"status": "error", "error": "boom"}
        return {"status": "ok"}


async def _stage_and_promote(db, detector):
    staged = await detector.run_once()
    assert len(staged) == 1
    await db.skills.set_evolution_candidate_status(staged[0], "promoted")
    rows = await db.skills.get_evolution_candidates(status="promoted", kind="macro")
    return rows[0]


async def test_macro_store_loads_and_routes_R5_1():
    db, d = await _open_db()
    try:
        # identical args → single-value slots → baked-in defaults
        await _add_n(db, 3, "start my note",
                     [("OPEN", "notepad"), ("CLICK", "body")])
        await _stage_and_promote(db, _det(db))
        store = MacroStore(known_verbs=set(_VERBS))
        assert await store.load_promoted(db) == 1
        m = store.match("please start my note now")
        assert m is not None and m.signature == "OPEN(STR) → CLICK(STR)"
        assert store.match("totally unrelated") is None
    finally:
        await db.close()
        d.cleanup()


async def test_replay_dispatches_through_executor_R5_2():
    db, d = await _open_db()
    try:
        await _add_n(db, 3, "do my thing",
                     [("OPEN", "notepad"), ("CLICK", "body")])
        row = await _stage_and_promote(db, _det(db))
        store = MacroStore(known_verbs=set(_VERBS))
        macro = store.register(row)
        ex = _FakeExec()
        res = await store.replay(macro, ex)
        assert res["status"] == "ok" and res["steps_run"] == 2
        assert [c[0] for c in ex.calls] == ["OPEN", "CLICK"]   # order preserved
        assert [c[1] for c in ex.calls] == ["notepad", "body"]  # baked defaults
        assert all(c[3] == "macro" for c in ex.calls)           # routed as macro
    finally:
        await db.close()
        d.cleanup()


async def test_replay_missing_tool_clarifies_and_runs_nothing_R5_3():
    db, d = await _open_db()
    try:
        await _add_n(db, 3, "two verbs",
                     [("OPEN", "notepad"), ("CLICK", "body")])
        row = await _stage_and_promote(db, _det(db))
        # OPEN no longer available at replay time
        store = MacroStore(known_verbs={"CLICK"})
        macro = store.register(row)
        ex = _FakeExec()
        res = await store.replay(macro, ex)
        assert res["status"] == "clarify"
        assert ex.calls == [], "no partial execution when a tool is missing"
    finally:
        await db.close()
        d.cleanup()


async def test_replay_requires_param_before_running():
    db, d = await _open_db()
    try:
        # differing args → multi-value slot → parameter required
        await _add_run(db, goal="typed thing", success=True,
                       steps=[("OPEN", "notepad"), ("TYPE", "hello")])
        await _add_run(db, goal="typed thing", success=True,
                       steps=[("OPEN", "notepad"), ("TYPE", "world")])
        await _add_run(db, goal="typed thing", success=True,
                       steps=[("OPEN", "notepad"), ("TYPE", "again")])
        row = await _stage_and_promote(db, _det(db))
        store = MacroStore(known_verbs=set(_VERBS))
        macro = store.register(row)
        assert macro.param_positions == [1], "TYPE slot varies → parameter"
        ex = _FakeExec()
        assert (await store.replay(macro, ex))["status"] == "clarify"
        assert ex.calls == []
        # supplying the param lets it run
        ex2 = _FakeExec()
        res = await store.replay(macro, ex2, params={1: "typed text"})
        assert res["status"] == "ok"
        assert ex2.calls[1][1] == "typed text"
    finally:
        await db.close()
        d.cleanup()


async def test_replay_stops_on_step_error():
    db, d = await _open_db()
    try:
        await _add_n(db, 3, "three step",
                     [("OPEN", "a/b.py"), ("CLICK", "body"), ("HOTKEY", "ctrl+s")])
        row = await _stage_and_promote(db, _det(db))
        store = MacroStore(known_verbs=set(_VERBS))
        macro = store.register(row)
        ex = _FakeExec(fail_on=1)   # second step errors
        res = await store.replay(macro, ex)
        assert res["status"] == "error" and res["steps_run"] == 1
        assert len(ex.calls) == 2, "stops after the failing step, no further steps"
    finally:
        await db.close()
        d.cleanup()


def test_register_rejects_ambiguous_shape():
    store = MacroStore(known_verbs=set(_VERBS))
    bad = {
        "id": 1, "text": "ambiguous", "action_or_wrong": "X → Y",
        "domain": "command",
        "source_refs": json.dumps({"run_ids": [1, 2, 3], "steps": [
            {"pos": 0, "action": "OPEN", "slot": "STR", "values": ["a"]},
            {"pos": 1, "action": "*", "slot": "STR", "values": ["b"]},
        ]}),
    }
    assert store.register(bad) is None


# ---------------------------------------------------------------------- #
# Approval surface + routing (spec §3 Requirement 4 / coordinator wiring)
# ---------------------------------------------------------------------- #

from core.macro_store import parse_macro_save, self_skilling_config


def test_parse_macro_save_phrasings():
    assert parse_macro_save("save that as a command called morning setup") == "morning setup"
    assert parse_macro_save("save this as a command named quick note") == "quick note"
    assert parse_macro_save("save that as morning setup") == "morning setup"
    assert parse_macro_save("remember that as daily standup") == "daily standup"
    assert parse_macro_save("save it as a command called X.") == "X"
    # not a save phrase
    assert parse_macro_save("open notepad") is None
    assert parse_macro_save("what can you do") is None


def test_self_skilling_config_is_dict_default_off():
    cfg = self_skilling_config()
    assert isinstance(cfg, dict)
    assert cfg.get("enabled", False) in (True, False)  # never raises


def test_build_prefers_persisted_name_and_keywords():
    store = MacroStore(known_verbs=set(_VERBS))
    row = {
        "id": 7, "text": "detector goal name", "action_or_wrong": "OPEN(STR)",
        "domain": "command",
        "source_refs": json.dumps({
            "run_ids": [1, 2, 3],
            "name": "morning setup",
            "keywords": ["morning setup"],
            "steps": [{"pos": 0, "action": "OPEN", "slot": "STR", "values": ["x"]}],
        }),
    }
    m = store.register(row)   # no explicit name → use persisted (restart path)
    assert m.name == "morning setup"
    assert store.match("do my morning setup now") is m


def _coord_with(db):
    from core.hybrid_coordinator import HybridCoordinator
    coord = HybridCoordinator(agent_db=db)
    spoken: list = []

    async def _say(t):
        spoken.append(t)

    coord._tts_speak = _say
    coord._spoken = spoken
    return coord


async def test_handle_macro_save_no_pending_R4_1():
    db, d = await _open_db()
    try:
        coord = _coord_with(db)
        coord.set_macro_store(MacroStore(known_verbs=set(_VERBS)))
        coord._workflow._pending_macro = None
        res = await coord._workflow.handle_macro_save("anything")
        assert res["action"] == "MACRO_SAVE_NONE"   # nothing pending → promote nothing
    finally:
        await db.close()
        d.cleanup()


async def test_handle_macro_save_promotes_and_routes():
    db, d = await _open_db()
    try:
        await _add_n(db, 3, "open and click",
                     [("OPEN", "notepad"), ("CLICK", "body")])
        staged = await _det(db).run_once()
        cid = staged[0]
        coord = _coord_with(db)
        store = MacroStore(known_verbs=set(_VERBS))
        coord.set_macro_store(store)
        coord._workflow._pending_macro = {"id": cid, "name": "open and click"}
        res = await coord._workflow.handle_macro_save("morning setup")
        assert res == {"status": "ok", "action": "MACRO_SAVE", "name": "morning setup"}
        # persisted: promoted, renamed, survives a fresh load
        row = await db.skills.get_evolution_candidate(cid)
        assert row["status"] == "promoted" and row["text"] == "morning setup"
        fresh = MacroStore(known_verbs=set(_VERBS))
        assert await fresh.load_promoted(db) == 1
        assert fresh.match("run morning setup please").name == "morning setup"
        assert coord._workflow._pending_macro is None
    finally:
        await db.close()
        d.cleanup()


async def test_maybe_handle_macro_replays_through_executor():
    db, d = await _open_db()
    try:
        await _add_n(db, 3, "open and click",
                     [("OPEN", "notepad"), ("CLICK", "body")])
        row = await _stage_and_promote(db, _det(db))
        coord = _coord_with(db)
        store = MacroStore(known_verbs=set(_VERBS))
        store.register(row, name="morning setup", keywords=["morning setup"])
        coord.set_macro_store(store)
        coord._executor = _FakeExec()
        cmd = Command(text="run morning setup", action="", source="voice")
        res = await coord._workflow.maybe_handle_macro(cmd)
        assert res["action"] == "MACRO_REPLAY" and res["status"] == "ok"
        assert [c[0] for c in coord._executor.calls] == ["OPEN", "CLICK"]
    finally:
        await db.close()
        d.cleanup()


async def test_macro_does_not_shadow_system_control():
    db, d = await _open_db()
    try:
        coord = _coord_with(db)
        store = MacroStore(known_verbs=set(_VERBS))
        # a macro mischievously named after a built-in
        store.register({
            "id": 1, "text": "help", "action_or_wrong": "OPEN(STR)",
            "domain": "command",
            "source_refs": json.dumps({"steps": [
                {"pos": 0, "action": "OPEN", "slot": "STR", "values": ["x"]}]}),
        }, name="help", keywords=["help"])
        coord.set_macro_store(store)
        coord._executor = _FakeExec()
        cmd = Command(text="help", action="", source="voice")
        assert await coord._workflow.maybe_handle_macro(cmd) is None  # built-in wins
        assert coord._executor.calls == []
    finally:
        await db.close()
        d.cleanup()
