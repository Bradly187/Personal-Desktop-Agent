"""Tests for the dev-plan escalation queue (R-10 residual).

When a dev plan exhausts MAX_REPLANS or hits MAX_STEPS, the run is rolled back
(saga) and the failed goal is persisted to `dev_escalations` for human review
instead of evaporating. A user cancel is deliberate and never escalates.

Covers:
  - AgentDB: insert / get_pending (newest-first, limit) / count / resolve
    (single + all), and closed-connection no-ops
  - DevAgent._record_escalation: row written, flag set, db-unavailable no-op,
    insert failure swallowed
  - DevAgent._halt_and_compensate: records a max_replans escalation
  - DevAgent._speak_plan_completion: mentions the review queue only when the
    run actually escalated
  - HybridCoordinator: "review queue" / "clear review queue" voice phrases
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.command_executor import Command
from core.hybrid_coordinator import _SYSTEM_CONTROL_PHRASES, HybridCoordinator
from inference.dev_agent import AgentResult, AgentStep, DevAgent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _open_db(tmp_path):
    from storage.db import AgentDB
    db = AgentDB()
    await db.open(tmp_path / "escalation.db")
    return db


async def _seed_run(db) -> int:
    await db.insert_session(mode="test")
    return await db.start_agent_run("escalation test", "plan", "model")


def _make_agent(db) -> DevAgent:
    return DevAgent(router=MagicMock(), agent_db=db)


# ---------------------------------------------------------------------------
# AgentDB escalation methods
# ---------------------------------------------------------------------------

async def test_insert_and_get_pending(tmp_path):
    db = await _open_db(tmp_path)
    run_id = await _seed_run(db)
    esc_id = await db.insert_escalation(
        run_id, "fix the tests", "max_replans",
        failed_action="RUN_TERMINAL", replans=2, detail='{"current_step": 3}',
    )
    assert esc_id is not None

    items = await db.get_pending_escalations()
    assert len(items) == 1
    row = items[0]
    assert row["goal"] == "fix the tests"
    assert row["reason"] == "max_replans"
    assert row["failed_action"] == "RUN_TERMINAL"
    assert row["replans"] == 2
    assert row["run_id"] == run_id
    await db.close()


async def test_get_pending_newest_first_and_limit(tmp_path):
    db = await _open_db(tmp_path)
    run_id = await _seed_run(db)
    for i in range(4):
        await db.insert_escalation(run_id, f"goal {i}", "max_steps")
        await asyncio.sleep(0.01)  # distinct timestamps for ORDER BY ts

    items = await db.get_pending_escalations(limit=2)
    assert len(items) == 2
    assert items[0]["goal"] == "goal 3"
    assert items[1]["goal"] == "goal 2"
    await db.close()


async def test_count_pending(tmp_path):
    db = await _open_db(tmp_path)
    run_id = await _seed_run(db)
    assert await db.count_pending_escalations() == 0
    await db.insert_escalation(run_id, "g1", "max_replans")
    await db.insert_escalation(run_id, "g2", "max_steps")
    assert await db.count_pending_escalations() == 2
    await db.close()


async def test_resolve_all(tmp_path):
    db = await _open_db(tmp_path)
    run_id = await _seed_run(db)
    await db.insert_escalation(run_id, "g1", "max_replans")
    await db.insert_escalation(run_id, "g2", "max_steps")

    cleared = await db.resolve_escalations(status="acknowledged")
    assert cleared == 2
    assert await db.get_pending_escalations() == []
    assert await db.count_pending_escalations() == 0
    await db.close()


async def test_resolve_single_by_id(tmp_path):
    db = await _open_db(tmp_path)
    run_id = await _seed_run(db)
    first = await db.insert_escalation(run_id, "g1", "max_replans")
    await db.insert_escalation(run_id, "g2", "max_steps")

    cleared = await db.resolve_escalations(status="dismissed", escalation_id=first)
    assert cleared == 1
    remaining = await db.get_pending_escalations()
    assert len(remaining) == 1
    assert remaining[0]["goal"] == "g2"
    await db.close()


async def test_closed_db_noops():
    from storage.db import AgentDB
    db = AgentDB()  # never opened
    assert await db.insert_escalation(1, "g", "max_replans") is None
    assert await db.get_pending_escalations() == []
    assert await db.count_pending_escalations() == 0
    assert await db.resolve_escalations() == 0


# ---------------------------------------------------------------------------
# DevAgent._record_escalation
# ---------------------------------------------------------------------------

async def test_record_escalation_writes_row_and_sets_flag(tmp_path):
    db = await _open_db(tmp_path)
    agent = _make_agent(db)
    run_id = await _seed_run(db)

    assert agent._escalated_this_run is False
    await agent._record_escalation(run_id, "do the thing", "max_replans",
                                   "WRITE_FILE", 2)
    assert agent._escalated_this_run is True

    items = await db.get_pending_escalations()
    assert len(items) == 1
    assert items[0]["goal"] == "do the thing"
    assert items[0]["failed_action"] == "WRITE_FILE"
    await db.close()


async def test_record_escalation_db_unavailable_falls_back_to_sidecar(tmp_path):
    # E4: a DB-down halt must NOT silently lose the escalation. It is written to
    # the durable sidecar and the flag is set (it WAS persisted, durably).
    agent = _make_agent(None)
    agent._escalation_sidecar_path = tmp_path / "esc.jsonl"
    await agent._record_escalation(1, "goal", "max_steps", None, 0)  # no crash
    assert agent._escalated_this_run is True
    assert agent._escalation_sidecar_path.exists()
    assert len(agent._escalation_sidecar_path.read_text("utf-8").splitlines()) == 1


async def test_record_escalation_insert_failure_falls_back_to_sidecar(tmp_path):
    # insert_escalation swallows its own error and returns None — E4 detects the
    # non-persist (None) and falls back to the sidecar rather than lying.
    db = MagicMock()
    db.available = True
    db.insert_escalation = AsyncMock(side_effect=RuntimeError("disk full"))
    agent = _make_agent(db)
    agent._escalation_sidecar_path = tmp_path / "esc.jsonl"
    await agent._record_escalation(1, "goal", "max_replans", None, 2)  # no raise
    assert agent._escalated_this_run is True
    assert agent._escalation_sidecar_path.exists()


# ---------------------------------------------------------------------------
# DevAgent._halt_and_compensate → escalation
# ---------------------------------------------------------------------------

async def test_halt_and_compensate_records_escalation(tmp_path):
    db = await _open_db(tmp_path)
    agent = _make_agent(db)
    run_id = await _seed_run(db)

    await agent._halt_and_compensate(run_id, "broken goal", 2, "RUN_TERMINAL")

    items = await db.get_pending_escalations()
    assert len(items) == 1
    assert items[0]["reason"] == "max_replans"
    assert items[0]["failed_action"] == "RUN_TERMINAL"
    assert items[0]["replans"] == 2
    await db.close()


# ---------------------------------------------------------------------------
# DevAgent._speak_plan_completion review-queue mention
# ---------------------------------------------------------------------------

def _failed_result() -> AgentResult:
    step = AgentStep(action="RUN_TERMINAL", args="pytest")
    step.success = False
    step.result = "exit 1"
    return AgentResult(goal="g", domain="plan", model_used="m", steps=[step],
                       response_text="", success=False, total_latency_ms=1.0)


async def test_completion_speech_mentions_review_queue_when_escalated():
    agent = _make_agent(None)
    agent._escalated_this_run = True
    speak = AsyncMock()
    client = MagicMock()
    client.speak = speak
    with patch("tts.polly_stream.get_client", return_value=client):
        await agent._speak_plan_completion(_failed_result(), cancelled=False)
        await asyncio.sleep(0)  # let the fire-and-forget task start
    assert "review queue" in speak.call_args[0][0]


async def test_completion_speech_no_review_queue_when_not_escalated():
    agent = _make_agent(None)
    speak = AsyncMock()
    client = MagicMock()
    client.speak = speak
    with patch("tts.polly_stream.get_client", return_value=client):
        await agent._speak_plan_completion(_failed_result(), cancelled=False)
        await asyncio.sleep(0)
    assert "review queue" not in speak.call_args[0][0]


# ---------------------------------------------------------------------------
# HybridCoordinator voice phrases
# ---------------------------------------------------------------------------

REVIEW_PHRASES = ("review queue", "show review queue", "what needs review",
                  "hey agent review queue", "show escalations", "pending reviews")
CLEAR_PHRASES = ("clear review queue", "dismiss reviews", "clear escalations")


@pytest.mark.parametrize("phrase", REVIEW_PHRASES + CLEAR_PHRASES)
def test_phrases_registered_as_system_control(phrase):
    assert phrase in _SYSTEM_CONTROL_PHRASES


def _coord(agent_db=None):
    c = HybridCoordinator()
    c._agent_db = agent_db
    c._tts_speak = AsyncMock()
    return c


def _route(c, text):
    return asyncio.run(c.route(Command(text=text, action="DICTATE", source="voice")))


def test_review_queue_with_pending_items():
    db = MagicMock()
    db.available = True
    db.count_pending_escalations = AsyncMock(return_value=2)
    db.get_pending_escalations = AsyncMock(return_value=[
        {"id": 2, "goal": "fix the tests", "reason": "max_replans"},
        {"id": 1, "goal": "refactor", "reason": "max_steps"},
    ])
    c = _coord(db)
    res = _route(c, "Review queue.")  # trailing punctuation like live Whisper
    assert res["action"] == "AGENT_ESCALATIONS"
    assert res["count"] == 2
    assert len(res["items"]) == 2


def test_review_queue_empty():
    db = MagicMock()
    db.available = True
    db.count_pending_escalations = AsyncMock(return_value=0)
    db.get_pending_escalations = AsyncMock(return_value=[])
    c = _coord(db)
    res = _route(c, "what needs review")
    assert res["action"] == "AGENT_ESCALATIONS"
    assert res["count"] == 0
    assert res["items"] == []


def test_review_queue_no_db():
    c = _coord(agent_db=None)
    res = _route(c, "review queue")
    assert res["action"] == "AGENT_ESCALATIONS"
    assert res["count"] == 0


def test_clear_review_queue():
    db = MagicMock()
    db.available = True
    db.resolve_escalations = AsyncMock(return_value=3)
    c = _coord(db)
    res = _route(c, "Clear review queue.")
    assert res["action"] == "AGENT_ESCALATIONS_CLEAR"
    assert res["count"] == 3
    db.resolve_escalations.assert_awaited_once_with(status="acknowledged")
