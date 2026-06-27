"""Tests for resume working-memory snapshot (specs/resume-working-memory, Gap C).

One assertion per numbered acceptance criterion (cited in the test name).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from inference.working_memory import (
    WorkingMemory,
    summarize_run,
    render_seed,
    score_relevance,
    select_related_runs,
    render_session_seed,
    session_memory_enabled,
)
from inference.dev_agent import DevAgent


def _steps():
    return [
        {"step_num": 1, "action": "READ_FILE", "args": "a.py", "result": "contents a", "success": 1},
        {"step_num": 2, "action": "WRITE_FILE", "args": "b.py", "result": "ok", "success": 1},
        {"step_num": 3, "action": "RUN_TERMINAL", "args": "pytest", "result": "2 failed", "success": 0},
    ]


# -- R1: derive snapshot from durable steps ---------------------------------- #

def test_r1_1_derives_files_notes_failure_from_steps():
    mem = summarize_run("fix the build", _steps())
    assert "a.py" in mem.files and "b.py" in mem.files     # path verbs → files
    assert mem.notes and any("READ_FILE" in n for n in mem.notes)
    assert mem.last_failure is not None and "2 failed" in mem.last_failure


def test_r1_2_last_failure_preserved():
    steps = [
        {"action": "RUN_TERMINAL", "args": "make", "result": "ERR: missing symbol foo", "success": 0},
        {"action": "EXPLAIN", "args": "", "result": "trying again", "success": 1},
    ]
    mem = summarize_run("g", steps)
    assert "missing symbol foo" in (mem.last_failure or "")


def test_r1_3_bounded_and_ordered():
    steps = [{"action": "READ_FILE", "args": f"f{i}.py", "result": "x" * 500, "success": 1}
             for i in range(20)]
    mem = summarize_run("g", steps, max_files=8, max_notes=5)
    assert len(mem.files) <= 8
    assert len(mem.notes) <= 5
    block = render_seed(mem, max_chars=400)
    assert len(block) <= 400 + 20            # bounded (+ marker slack)
    # ordering: files line precedes notes line precedes failure line
    assert block.index("files already touched") < block.index("recent steps")


def test_render_seed_empty_is_blank():
    assert render_seed(WorkingMemory(goal="g")) == ""   # empty memory → no injection


# -- R3: schema-free, degrades ----------------------------------------------- #

def test_r3_2_no_steps_returns_empty():
    mem = summarize_run("g", [])
    assert mem.is_empty()
    assert render_seed(mem) == ""


@pytest.mark.asyncio
async def test_r3_2_resume_seed_empty_when_no_steps(monkeypatch):
    monkeypatch.setenv("DA_RESUME_MEMORY", "1")
    agent = DevAgent(router=MagicMock())
    db = MagicMock()
    db.get_steps_for_run = AsyncMock(return_value=[])
    agent._db = MagicMock(return_value=db)
    seed = await agent._resume_seed_context(7, "goal")
    assert seed == ""


# -- R3.4 / R2.2: flag gating ------------------------------------------------ #

@pytest.mark.asyncio
async def test_r3_4_disabled_returns_empty_seed(monkeypatch):
    # DA_RESUME_MEMORY now defaults ON (R3.4, verified by integration test below),
    # so the disabled path is exercised with an explicit =0 rather than delenv.
    monkeypatch.setenv("DA_RESUME_MEMORY", "0")
    agent = DevAgent(router=MagicMock())
    db = MagicMock()
    db.get_steps_for_run = AsyncMock(return_value=_steps())
    agent._db = MagicMock(return_value=db)
    seed = await agent._resume_seed_context(7, "goal")
    assert seed == ""                       # off → byte-identical (no DB read used)
    db.get_steps_for_run.assert_not_called()


@pytest.mark.asyncio
async def test_r2_1_enabled_builds_seed(monkeypatch):
    monkeypatch.setenv("DA_RESUME_MEMORY", "1")
    agent = DevAgent(router=MagicMock())
    db = MagicMock()
    db.get_steps_for_run = AsyncMock(return_value=_steps())
    agent._db = MagicMock(return_value=db)
    seed = await agent._resume_seed_context(7, "fix build")
    assert "resumed-task-memory" in seed
    assert "a.py" in seed and "2 failed" in seed


@pytest.mark.asyncio
async def test_r3_2_db_error_degrades(monkeypatch):
    monkeypatch.setenv("DA_RESUME_MEMORY", "1")
    agent = DevAgent(router=MagicMock())
    db = MagicMock()
    db.get_steps_for_run = AsyncMock(side_effect=RuntimeError("db gone"))
    agent._db = MagicMock(return_value=db)
    seed = await agent._resume_seed_context(7, "goal")
    assert seed == ""                       # never raises into resume


# -- 5b: crash-resume integration (resume_pending_plan wiring) ---------------- #

@pytest.mark.asyncio
async def test_5b_resume_seeds_plan_when_enabled(monkeypatch):
    monkeypatch.setenv("DA_RESUME_MEMORY", "1")
    agent = DevAgent(router=MagicMock())
    db = MagicMock()
    db.available = True
    db.get_interrupted_runs = AsyncMock(return_value=[{"id": 7, "goal": "fix the build"}])
    db.get_steps_for_run = AsyncMock(return_value=_steps())
    agent._db = MagicMock(return_value=db)
    agent._confirm_destructive_op = AsyncMock(return_value=True)   # voice "yes"
    agent.plan_and_run = AsyncMock()

    run = await agent.resume_pending_plan()

    assert run is not None
    agent.plan_and_run.assert_awaited_once()
    seed = agent.plan_and_run.await_args.kwargs["seed_context"]
    assert "resumed-task-memory" in seed
    assert "a.py" in seed and "2 failed" in seed   # touched file + failure


@pytest.mark.asyncio
async def test_5b_resume_bare_goal_when_disabled(monkeypatch):
    monkeypatch.setenv("DA_RESUME_MEMORY", "0")     # off → regression guard
    agent = DevAgent(router=MagicMock())
    db = MagicMock()
    db.available = True
    db.get_interrupted_runs = AsyncMock(return_value=[{"id": 7, "goal": "fix the build"}])
    db.get_steps_for_run = AsyncMock(return_value=_steps())
    agent._db = MagicMock(return_value=db)
    agent._confirm_destructive_op = AsyncMock(return_value=True)
    agent.plan_and_run = AsyncMock()

    await agent.resume_pending_plan()

    assert agent.plan_and_run.await_args.kwargs["seed_context"] == ""   # bare goal


# -- R4: cross-session memory (pure helpers) --------------------------------- #

def test_r4_1_score_relevance_ranks_overlap():
    base = "fix the websocket reconnect in ipad bridge"
    related = "websocket reconnect drops on ipad bridge restart"
    unrelated = "render the kokoro tts voice table"
    assert score_relevance(base, related) > score_relevance(base, unrelated)
    assert score_relevance(base, base) == pytest.approx(1.0)   # identical content
    assert score_relevance("alpha beta", "gamma delta") == 0.0  # disjoint → 0


def test_r4_2_select_related_honors_threshold_topk_and_order():
    runs = [
        {"id": 1, "goal": "websocket reconnect ipad bridge", "ts": 100.0},
        {"id": 2, "goal": "websocket reconnect bridge timeout", "ts": 200.0},
        {"id": 3, "goal": "kokoro tts voice table", "ts": 300.0},   # unrelated
    ]
    picked = select_related_runs("websocket reconnect bridge", runs, top_k=2, min_score=0.2)
    ids = [r["id"] for r in picked]
    assert 3 not in ids                       # below min_score, dropped
    assert len(ids) <= 2                       # top_k honored
    assert ids[0] in (1, 2)                    # highest-scoring first
    assert select_related_runs("totally different words here", runs, min_score=0.5) == []


def test_r4_3_render_session_seed_bounded_ordered_and_tagged():
    mems = [
        ("fix websocket", WorkingMemory(goal="fix websocket", files=["bridge.py"],
                                        notes=["READ_FILE bridge.py → ok"],
                                        last_failure="timeout")),
    ]
    block = render_session_seed(mems, max_chars=2000)
    assert "<prior-session-memory" in block
    assert "resumed-task-memory" not in block        # distinct from crash-resume tag
    assert "bridge.py" in block and "timeout" in block
    # bounded
    assert len(render_session_seed(mems, max_chars=40)) <= 40 + 20


def test_r4_3_render_session_seed_empty_is_blank():
    assert render_session_seed([]) == ""
    assert render_session_seed([("g", WorkingMemory(goal="g"))]) == ""   # empty mem skipped


def test_r4_4_session_memory_enabled_default_off(monkeypatch):
    monkeypatch.delenv("DA_SESSION_MEMORY", raising=False)
    assert session_memory_enabled() is False
    monkeypatch.setenv("DA_SESSION_MEMORY", "1")
    assert session_memory_enabled() is True


# -- R4: cross-session memory (_session_seed_context) ------------------------- #

def _related_runs():
    return [
        {"id": 1, "goal": "fix websocket reconnect in bridge", "ts": 200.0,
         "success": 0, "status": "failed"},
        {"id": 2, "goal": "render kokoro tts voices", "ts": 100.0,
         "success": 1, "status": "completed"},   # unrelated
    ]


@pytest.mark.asyncio
async def test_r4_session_seed_built_when_enabled(monkeypatch):
    monkeypatch.setenv("DA_SESSION_MEMORY", "1")
    agent = DevAgent(router=MagicMock())
    db = MagicMock()
    db.get_recent_runs = AsyncMock(return_value=_related_runs())
    db.get_steps_for_run = AsyncMock(return_value=_steps())
    agent._db = MagicMock(return_value=db)
    seed = await agent._session_seed_context("fix websocket reconnect bridge")
    assert "prior-session-memory" in seed
    assert "a.py" in seed                       # file from the related run's steps
    # only the related run's steps were fetched (unrelated #2 filtered out)
    db.get_steps_for_run.assert_awaited_once_with(1)


@pytest.mark.asyncio
async def test_r4_session_seed_empty_when_disabled(monkeypatch):
    monkeypatch.delenv("DA_SESSION_MEMORY", raising=False)   # default OFF
    agent = DevAgent(router=MagicMock())
    db = MagicMock()
    db.get_recent_runs = AsyncMock(return_value=_related_runs())
    agent._db = MagicMock(return_value=db)
    seed = await agent._session_seed_context("fix websocket reconnect bridge")
    assert seed == ""
    db.get_recent_runs.assert_not_called()      # off → no DB read


@pytest.mark.asyncio
async def test_r4_session_seed_empty_when_no_related(monkeypatch):
    monkeypatch.setenv("DA_SESSION_MEMORY", "1")
    agent = DevAgent(router=MagicMock())
    db = MagicMock()
    db.get_recent_runs = AsyncMock(return_value=_related_runs())
    db.get_steps_for_run = AsyncMock(return_value=_steps())
    agent._db = MagicMock(return_value=db)
    seed = await agent._session_seed_context("entirely unrelated quantum circuit topic")
    assert seed == ""                           # nothing clears min_score


@pytest.mark.asyncio
async def test_r4_session_seed_db_error_degrades(monkeypatch):
    monkeypatch.setenv("DA_SESSION_MEMORY", "1")
    agent = DevAgent(router=MagicMock())
    db = MagicMock()
    db.get_recent_runs = AsyncMock(side_effect=RuntimeError("db gone"))
    agent._db = MagicMock(return_value=db)
    seed = await agent._session_seed_context("fix websocket reconnect bridge")
    assert seed == ""                           # never raises into planning


@pytest.mark.asyncio
async def test_r4_plan_context_carries_session_seed(monkeypatch):
    """Integration: a fresh plan's planner context includes the cross-session
    block when enabled, and is unchanged when off (byte-identical regression
    guard that lets DA_RESUME_MEMORY ship default-ON)."""
    captured = {}

    def _make_agent():
        agent = DevAgent(router=MagicMock())
        # Neutralize the other context sources so we isolate the session seed.
        agent._format_context = MagicMock(return_value=None)
        agent._rag_context = AsyncMock(return_value="")
        agent._git_context = AsyncMock(return_value="")
        agent._workspace_context = MagicMock(return_value=None)
        agent._skill_registry = None
        # Router: structured-format probes return plain strings; infer captures ctx
        # and fails the plan so _plan_and_run_locked returns right after the call.
        agent._router.edit_format_for = MagicMock(return_value="whole_file")
        agent._router.select_profile = MagicMock(return_value=MagicMock(name="plan"))

        async def _infer(*, domain, user_text, context):
            captured["context"] = context
            return MagicMock(ok=False, model="m", error="stop")

        agent._router.infer = AsyncMock(side_effect=_infer)
        db = MagicMock()
        db.get_recent_runs = AsyncMock(return_value=_related_runs())
        db.get_steps_for_run = AsyncMock(return_value=_steps())
        agent._db = MagicMock(return_value=db)
        return agent

    monkeypatch.setenv("DA_SESSION_MEMORY", "1")
    await _make_agent()._plan_and_run_locked("fix websocket reconnect bridge")
    assert "<prior-session-memory" in captured["context"]

    captured.clear()
    monkeypatch.setenv("DA_SESSION_MEMORY", "0")
    await _make_agent()._plan_and_run_locked("fix websocket reconnect bridge")
    assert "prior-session-memory" not in (captured["context"] or "")
