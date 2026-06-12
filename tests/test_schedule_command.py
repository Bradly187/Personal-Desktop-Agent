"""N+2 — coordinator voice scheduling: parsed spec -> AgentDB action.

Run:
    python -m pytest tests/test_schedule_command.py -q
"""

from __future__ import annotations

import datetime
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from core.hybrid_coordinator import HybridCoordinator
from core.schedule_parser import parse
from storage.db import AgentDB

NOW = datetime.datetime(2026, 6, 13, 6, 0, 0).timestamp()


@pytest.fixture(autouse=True)
def _silence_tts(monkeypatch):
    import tts.polly_stream as _ps
    monkeypatch.setattr(_ps, "get_client",
                        lambda *a, **k: MagicMock(speak_sync=lambda *_: True))


def _coord(db) -> HybridCoordinator:
    c = HybridCoordinator.__new__(HybridCoordinator)   # bypass heavy __init__
    c._agent_db = db
    return c


async def _db(tmp_path) -> AgentDB:
    db = AgentDB()
    await db.open(tmp_path / "a.db")
    return db


async def test_schedule_creates_scheduled_goal(tmp_path):
    db = await _db(tmp_path)
    try:
        c = _coord(db)
        res = await c._handle_schedule_command(parse("every morning brief me", NOW))
        assert res["action"] == "SCHEDULE_SET"
        assert any(s["goal"] == "brief me" for s in await db.list_schedules())
    finally:
        await db.close()


async def test_event_rule_command(tmp_path):
    db = await _db(tmp_path)
    try:
        c = _coord(db)
        res = await c._handle_schedule_command(
            parse("when an email from boss arrives, tell me", NOW))
        assert res["action"] == "EVENT_RULE_SET"
        assert any(r["topic_pattern"] == "email.arrived" for r in await db.list_event_rules())
    finally:
        await db.close()


async def test_list_and_cancel_all(tmp_path):
    db = await _db(tmp_path)
    try:
        c = _coord(db)
        await c._handle_schedule_command(parse("every morning brief me", NOW))
        lst = await c._handle_schedule_command(parse("what are my reminders", NOW))
        assert lst["action"] == "SCHEDULE_LIST" and lst["count"] >= 1
        canc = await c._handle_schedule_command(parse("cancel all reminders", NOW))
        assert canc["action"] == "SCHEDULE_CANCEL" and canc["count"] >= 1
        assert await db.list_schedules() == []
    finally:
        await db.close()


async def test_unparsed_is_graceful(tmp_path):
    db = await _db(tmp_path)
    try:
        res = await _coord(db)._handle_schedule_command(None)
        assert res["action"] == "SCHEDULE_UNPARSED"
    finally:
        await db.close()
