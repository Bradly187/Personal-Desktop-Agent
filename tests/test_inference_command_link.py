"""Tests for the inferences-commands link backfill + finetuning extractor scrub.

Audit 2026-06-09: inference rows were always written with command_id=NULL
(insert_command runs after inference returns) and nothing back-filled the
link, so the fine-tuning extraction JOIN matched zero rows forever. And once
linked, the extractor would have exported Gate-0-protected secrets verbatim
(local prompts are stored raw by design) - so the scrub pass is part of the
same fix.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from storage.db import AgentDB
from core.command_executor import Command


@pytest.fixture
async def db(tmp_path):
    d = AgentDB()
    await d.open(tmp_path / "agent.db")
    if not d.available:
        pytest.skip("aiosqlite unavailable")
    yield d
    await d.close()


async def _one(db, sql, params=()):
    async with db._conn.execute(sql, params) as cur:
        return await cur.fetchone()


# ---------------------------------------------------------------------------
# AgentDB.commands.link_inferences_to_command
# ---------------------------------------------------------------------------

async def test_backfill_links_inference_to_command(db):
    inf_id = await db.inferences.insert_inference(
        command_id=None, model="llama3.1:8b", domain="command",
        prompt="p", response="CLICK save", tokens_in=10, tokens_out=3,
        latency_ms=42.0,
    )
    assert inf_id > 0
    sid = await db.sessions.insert_session()
    cmd_id = await db.commands.insert_command(
        session_id=sid, cmd=Command(text="click save", action="CLICK", source="voice"),
        action="CLICK save", route="local", gate_that_decided="all_pass",
        latency_ms=50.0, success=True,
    )
    assert cmd_id > 0

    await db.commands.link_inferences_to_command([inf_id], cmd_id)

    row = await _one(
        db,
        "SELECT c.text FROM inferences i JOIN commands c ON i.command_id = c.id"
        " WHERE i.id = ?",
        (inf_id,),
    )
    assert row is not None                 # the extraction JOIN now matches
    assert row["text"] == "click save"


async def test_backfill_never_relinks_an_already_linked_row(db):
    sid = await db.sessions.insert_session()
    cmd_a = await db.commands.insert_command(
        session_id=sid, cmd=Command(text="a", action="CLICK", source="voice"),
        action="CLICK a", route="local", gate_that_decided="all_pass",
        latency_ms=1.0, success=True,
    )
    cmd_b = await db.commands.insert_command(
        session_id=sid, cmd=Command(text="b", action="CLICK", source="voice"),
        action="CLICK b", route="local", gate_that_decided="all_pass",
        latency_ms=1.0, success=True,
    )
    inf_id = await db.inferences.insert_inference(
        command_id=cmd_a, model="m", domain="command", prompt="p",
        response="r", tokens_in=None, tokens_out=None, latency_ms=1.0,
    )
    await db.commands.link_inferences_to_command([inf_id], cmd_b)
    row = await _one(db, "SELECT command_id FROM inferences WHERE id = ?", (inf_id,))
    assert row["command_id"] == cmd_a      # unchanged


async def test_backfill_ignores_invalid_ids(db):
    # Must not raise on empty/invalid input.
    await db.commands.link_inferences_to_command([], 1)
    await db.commands.link_inferences_to_command([-1, 0], 1)
    await db.commands.link_inferences_to_command([1], -1)


# ---------------------------------------------------------------------------
# Extractor scrub pass
# ---------------------------------------------------------------------------

def test_scrub_drops_rows_with_secret_in_target():
    from scripts.extract_finetuning_dataset import _scrub_rows
    rows = [
        {"cmd_text": "type my key", "prompt": None, "session_context": None,
         "response": "TYPE sk-ant-api03-" + "a" * 40, "corrected_to": None},
        {"cmd_text": "click save", "prompt": None, "session_context": None,
         "response": "CLICK save button", "corrected_to": None},
    ]
    kept, dropped, redacted = _scrub_rows(rows)
    assert dropped == 1
    assert len(kept) == 1
    assert kept[0]["response"] == "CLICK save button"


def test_scrub_redacts_secret_in_input_side():
    from scripts.extract_finetuning_dataset import _scrub_rows
    secret = "sk-ant-api03-" + "b" * 40
    rows = [
        {"cmd_text": f"my api key is {secret}", "prompt": f"prompt with {secret}",
         "session_context": None, "response": "CLARIFY which key?",
         "corrected_to": None},
    ]
    kept, dropped, redacted = _scrub_rows(rows)
    assert dropped == 0
    assert redacted == 1
    assert secret not in kept[0]["cmd_text"]
    assert secret not in kept[0]["prompt"]
    assert "REDACTED" in kept[0]["cmd_text"]

