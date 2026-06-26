"""Tests for resume working-memory snapshot (specs/resume-working-memory, Gap C).

One assertion per numbered acceptance criterion (cited in the test name).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from inference.working_memory import WorkingMemory, summarize_run, render_seed
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
    monkeypatch.delenv("DA_RESUME_MEMORY", raising=False)   # default OFF
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
