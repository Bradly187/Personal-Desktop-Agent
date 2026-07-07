"""Tests for the "resume task" voice phrase (post-crash recovery).

The crash notice spoken at startup (main.py) tells the user to say
"resume task" — these tests pin that the phrase actually exists and routes to
DevAgent.resume_pending_plan (which is itself voice-confirm-gated, so the
phrase alone can never re-run a plan with destructive steps).

Covers:
  - all resume phrases are registered system-control phrases (so the
    dev-agent pre-gate leaves them alone)
  - nothing interrupted -> spoken "no task" notice, resume never invoked
  - interrupted run present -> resume_pending_plan fired
  - no dev agent wired -> graceful "no task" path
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.command_executor import Command
from core.hybrid_coordinator import HybridCoordinator
from core.voice_system_control import _SYSTEM_CONTROL_PHRASES

_RESUME_PHRASES = (
    "resume task", "resume the task", "hey agent resume",
    "resume work", "resume interrupted task",
)


def _coord(agent_db=None, dev_agent=None) -> HybridCoordinator:
    c = HybridCoordinator()
    c._agent_db = agent_db
    c._dev_agent = dev_agent
    c._tts_speak = AsyncMock()
    return c


def _route(c: HybridCoordinator, text: str) -> dict:
    async def go():
        res = await c.route(Command(text=text, action="DICTATE", source="voice"))
        await asyncio.sleep(0)  # let fire-and-forget tasks start
        return res

    return asyncio.run(go())


def test_resume_phrases_are_system_control():
    for phrase in _RESUME_PHRASES:
        assert phrase in _SYSTEM_CONTROL_PHRASES, phrase


def test_resume_with_interrupted_run_fires_resume():
    db = MagicMock()
    db.available = True
    db.runs.get_interrupted_runs = AsyncMock(
        return_value=[{"id": 3, "goal": "refactor the indexer"}]
    )
    agent = MagicMock()
    agent.resume_pending_plan = AsyncMock(return_value={"id": 3})
    c = _coord(db, agent)

    res = _route(c, "Resume task.")  # trailing punctuation like live Whisper

    assert res["action"] == "AGENT_RESUME"
    assert res["offered"] is True
    assert res["goal"] == "refactor the indexer"
    agent.resume_pending_plan.assert_awaited_once()


def test_resume_with_nothing_interrupted_speaks_no_task():
    db = MagicMock()
    db.available = True
    db.runs.get_interrupted_runs = AsyncMock(return_value=[])
    agent = MagicMock()
    agent.resume_pending_plan = AsyncMock()
    c = _coord(db, agent)

    res = _route(c, "resume task")

    assert res["action"] == "AGENT_RESUME"
    assert res["offered"] is False
    agent.resume_pending_plan.assert_not_awaited()
    spoken = c._tts_speak.await_args.args[0]
    assert "no interrupted task" in spoken.lower()


def test_resume_without_dev_agent_is_graceful():
    db = MagicMock()
    db.available = True
    db.runs.get_interrupted_runs = AsyncMock(
        return_value=[{"id": 1, "goal": "anything"}]
    )
    c = _coord(db, dev_agent=None)

    res = _route(c, "resume the task")

    assert res["action"] == "AGENT_RESUME"
    assert res["offered"] is False


def test_resume_without_db_is_graceful():
    c = _coord(agent_db=None, dev_agent=MagicMock())

    res = _route(c, "hey agent resume")

    assert res["action"] == "AGENT_RESUME"
    assert res["offered"] is False
