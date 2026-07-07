"""E0 — regression tests for the LIVE few-shot correction loop.

The loop (whisper "no/wait/actually …" -> HybridCoordinator._on_correction ->
ContinuousTrainer.record_correction -> ranked few-shot retrieval -> prompt
injection) was fully wired but had NO test against a real AgentDB — the existing
test_correction_flow.py only checks the mock plumbing. These tests drive a real
AgentDB end to end: a correction must retire the wrong mapping, inject the
corrected one, surface the wrong action as a user_correction counterexample, and
both must land in the inference prompt. Also covers get_few_shot_examples ranking.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.command_executor import Command
from storage.db import AgentDB
from adaptive.continuous_trainer import ContinuousTrainer
from inference.local_inference import _build_prompt


async def _open_db():
    d = tempfile.mkdtemp()
    db = AgentDB()
    await db.open(os.path.join(d, "agent.db"))
    return db


@pytest.mark.asyncio
async def test_correction_injects_corrected_and_retires_wrong():
    db = await _open_db()
    try:
        trainer = ContinuousTrainer(agent_db=db)
        cmd = Command(text="oh pen notepad", action="", source="voice")
        await trainer.record_correction(cmd, "TYPE pen notepad", "OPEN notepad")

        examples = await db.memory.get_few_shot_examples(cmd, domain="command")
        actions = {e["action_text"] for e in examples}
        assert "OPEN notepad" in actions                 # corrected mapping injected
        assert "TYPE pen notepad" not in actions          # wrong positive retired

        counter = await db.memory.get_few_shot_counterexamples(cmd, domain="command")
        wrongs = {c["wrong_action"] for c in counter}
        assert "TYPE pen notepad" in wrongs               # surfaced as counterexample
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_user_correction_counterexample_injects_immediately():
    """A user_correction counterexample needs no usage_count>=2 (direct evidence)."""
    db = await _open_db()
    try:
        trainer = ContinuousTrainer(agent_db=db)
        cmd = Command(text="close the lid", action="", source="voice")
        await trainer.record_correction(cmd, "CLICK lid", "CLOSE window")
        counter = await db.memory.get_few_shot_counterexamples(cmd, domain="command")
        assert any(c["wrong_action"] == "CLICK lid" for c in counter)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_correction_lands_in_inference_prompt():
    db = await _open_db()
    try:
        trainer = ContinuousTrainer(agent_db=db)
        cmd = Command(text="oh pen notepad", action="", source="voice")
        await trainer.record_correction(cmd, "TYPE pen notepad", "OPEN notepad")

        examples = await db.memory.get_few_shot_examples(cmd, domain="command")
        counter = await db.memory.get_few_shot_counterexamples(cmd, domain="command")
        prompt = _build_prompt(cmd, few_shot_examples=examples, counterexamples=counter)

        assert "OPEN notepad" in prompt                   # corrected example shown
        assert "TYPE pen notepad" in prompt               # counterexample shown
        assert "Do NOT produce" in prompt
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_few_shot_ranking_prefers_more_similar():
    """get_few_shot_examples ranks by similarity x recency x usage."""
    db = await _open_db()
    try:
        # Two stored mappings; query is lexically closest to the 'scroll' one.
        await db.memory.upsert_few_shot_example(
            Command(text="scroll down the page", action="", source="voice"),
            "SCROLL down", "command")
        await db.memory.upsert_few_shot_example(
            Command(text="open the settings menu", action="", source="voice"),
            "OPEN settings", "command")

        q = Command(text="scroll down please", action="", source="voice")
        ranked = await db.memory.get_few_shot_examples(q, n=2, domain="command")
        assert ranked, "expected at least one ranked example"
        assert ranked[0]["action_text"] == "SCROLL down"   # most similar ranks first
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_correction_marks_command_row():
    db = await _open_db()
    try:
        trainer = ContinuousTrainer(agent_db=db)
        cmd = Command(text="oh pen notepad", action="", source="voice")
        # seed a command row so the command_id>0 path runs mark_command_corrected
        sid = await db.sessions.insert_session(mode="test")
        cid = await db.commands.insert_command(
            session_id=sid, cmd=cmd, action="TYPE pen notepad", route="local",
            gate_that_decided="gate1", latency_ms=1.0, success=False)
        await trainer.record_correction(cmd, "TYPE pen notepad", "OPEN notepad",
                                        command_id=cid)
        cur = await db._conn.execute(
            "SELECT corrected_to FROM commands WHERE id=?", (cid,))
        row = await cur.fetchone()
        assert row is not None and row[0] == "OPEN notepad"
    finally:
        await db.close()
