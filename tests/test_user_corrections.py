"""GAP-9 — harvest user corrections as labeled failure data.

insert_correction/get_corrections round-trip through AgentDB, and the
coordinator's _harvest_correction schedules the write fire-and-forget from the
explicit-correction path.

Run:
    python -m pytest tests/test_user_corrections.py -q
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from storage.db import AgentDB
from core.hybrid_coordinator import HybridCoordinator
from core.command_executor import Command


async def _open(tmp_path, name) -> AgentDB:
    db = AgentDB()
    await db.open(tmp_path / name)
    return db


async def test_insert_and_get_roundtrip(tmp_path):
    db = await _open(tmp_path, "corr.db")
    if not db.available:
        pytest.skip("aiosqlite unavailable")
    rid = await db.logs.insert_correction(1, "tid7", "no I meant close the window",
                                     "OPEN window", "command")
    assert rid
    rows = await db.logs.get_corrections()
    assert len(rows) == 1
    r = rows[0]
    assert r["correction_text"] == "no I meant close the window"
    assert r["prior_action"] == "OPEN window"
    assert r["domain"] == "command"
    assert r["session_id"] == 1


async def test_empty_text_not_inserted(tmp_path):
    db = await _open(tmp_path, "corr2.db")
    if not db.available:
        pytest.skip("aiosqlite unavailable")
    assert await db.logs.insert_correction(1, None, "", "OPEN x", "command") is None
    assert await db.logs.get_corrections() == []


async def test_harvest_correction_schedules_write(tmp_path):
    db = await _open(tmp_path, "corr3.db")
    if not db.available:
        pytest.skip("aiosqlite unavailable")
    c = HybridCoordinator.__new__(HybridCoordinator)
    c._agent_db = db
    c._session_id = 3

    cmd = Command(source="voice", action="", text="no, close it instead")
    c._harvest_correction(cmd, "OPEN editor")
    # fire_and_log scheduled the insert on the running loop — let it run.
    await asyncio.sleep(0.05)

    rows = await db.logs.get_corrections()
    assert any("close it" in r["correction_text"] for r in rows)
    assert rows[0]["prior_action"] == "OPEN editor"


async def test_on_correction_harvests_without_trainer(tmp_path):
    # Regression: _on_correction must harvest even when no ContinuousTrainer is
    # wired (it used to early-return before the harvest call).
    db = await _open(tmp_path, "corr4.db")
    if not db.available:
        pytest.skip("aiosqlite unavailable")
    c = HybridCoordinator.__new__(HybridCoordinator)
    c._agent_db = db
    c._session_id = 5
    c._trainer = None
    c._last_executed_action = "OPEN editor"
    c._last_command_id = -1

    cmd = Command(source="voice", action="", text="no, I meant close it")
    await c._on_correction(cmd, "CLOSE editor")
    await asyncio.sleep(0.05)

    rows = await db.logs.get_corrections()
    assert any("close it" in r["correction_text"] for r in rows)
