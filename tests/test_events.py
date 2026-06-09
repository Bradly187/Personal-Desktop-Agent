"""Tests for core.events — EventBus publish/subscribe and topic matching."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.events import EventBus, _topic_matches, TOPIC_COMMAND_EXECUTED, TOPIC_GATE_DECIDED


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _make_bus(tmp_path):
    from storage.db import AgentDB
    db = AgentDB()
    await db.open(tmp_path / "events.db")
    return EventBus(db), db


# ---------------------------------------------------------------------------
# _topic_matches
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("topic,pattern,expected", [
    ("command.executed",  "command.executed",  True),
    ("command.executed",  "gate.decided",      False),
    ("command.executed",  "command.%",         True),
    ("gate.decided",      "command.%",         False),
    ("step.failed",       "%",                 True),
    ("voice.drift",       "voice.%",           True),
    ("voice.drift",       "voice.drift.%",     False),
    ("a.b.c",             "a.%",               True),
    ("a",                 "a.%",               False),
])
def test_topic_matches(topic, pattern, expected):
    assert _topic_matches(topic, pattern) is expected


# ---------------------------------------------------------------------------
# publish → SQLite
# ---------------------------------------------------------------------------

async def test_publish_writes_to_event_log(tmp_path):
    bus, db = await _make_bus(tmp_path)
    row_id = await bus.publish(
        TOPIC_COMMAND_EXECUTED,
        {"action": "CLICK", "success": True},
        source="coordinator",
        session_id=1, command_id=10, trace_id="abc123",
    )
    assert row_id is not None and row_id > 0
    # Read back via poll_events
    events = await db.poll_events(TOPIC_COMMAND_EXECUTED, last_event_id=0)
    assert len(events) == 1
    assert events[0]["topic"] == TOPIC_COMMAND_EXECUTED
    assert events[0]["source"] == "coordinator"
    assert events[0]["trace_id"] == "abc123"
    import json
    payload = json.loads(events[0]["payload"])
    assert payload["action"] == "CLICK"
    await db.close()


async def test_publish_multiple_topics_filtered_by_poll(tmp_path):
    bus, db = await _make_bus(tmp_path)
    await bus.publish(TOPIC_COMMAND_EXECUTED, {"a": 1}, source="c")
    await bus.publish(TOPIC_GATE_DECIDED, {"b": 2}, source="c")
    await bus.publish(TOPIC_COMMAND_EXECUTED, {"a": 3}, source="c")

    cmd_events = await db.poll_events(TOPIC_COMMAND_EXECUTED, last_event_id=0)
    assert len(cmd_events) == 2

    gate_events = await db.poll_events(TOPIC_GATE_DECIDED, last_event_id=0)
    assert len(gate_events) == 1

    await db.close()


async def test_poll_events_respects_cursor(tmp_path):
    bus, db = await _make_bus(tmp_path)
    id1 = await bus.publish(TOPIC_COMMAND_EXECUTED, {"n": 1}, source="c")
    id2 = await bus.publish(TOPIC_COMMAND_EXECUTED, {"n": 2}, source="c")
    id3 = await bus.publish(TOPIC_COMMAND_EXECUTED, {"n": 3}, source="c")

    all_events = await db.poll_events(TOPIC_COMMAND_EXECUTED, last_event_id=0)
    assert len(all_events) == 3

    # Cursor at id1 → only see id2 and id3
    after_first = await db.poll_events(TOPIC_COMMAND_EXECUTED, last_event_id=id1)
    assert len(after_first) == 2
    assert after_first[0]["id"] == id2

    await db.close()


# ---------------------------------------------------------------------------
# publish → in-process Queue fan-out
# ---------------------------------------------------------------------------

async def test_publish_fans_out_to_subscriber(tmp_path):
    bus, db = await _make_bus(tmp_path)
    received = []

    async def consume():
        async for event in bus.subscribe("watcher", TOPIC_COMMAND_EXECUTED):
            received.append(event)
            break  # stop after first

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)  # let subscriber register

    await bus.publish(TOPIC_COMMAND_EXECUTED, {"x": 1}, source="test")
    await asyncio.sleep(0.05)
    await task

    assert len(received) == 1
    assert received[0]["topic"] == TOPIC_COMMAND_EXECUTED
    assert received[0]["payload"] == {"x": 1}
    await db.close()


async def test_publish_only_fans_out_matching_pattern(tmp_path):
    bus, db = await _make_bus(tmp_path)
    gate_received = []
    cmd_received = []

    async def consume_gate():
        async for event in bus.subscribe("gate_watcher", "gate.%"):
            gate_received.append(event)
            break

    async def consume_cmd():
        async for event in bus.subscribe("cmd_watcher", "command.%"):
            cmd_received.append(event)
            break

    t1 = asyncio.create_task(consume_gate())
    t2 = asyncio.create_task(consume_cmd())
    await asyncio.sleep(0)

    await bus.publish(TOPIC_GATE_DECIDED, {"gate": "all_pass"}, source="test")
    await asyncio.sleep(0.05)

    await t1
    t2.cancel()

    assert len(gate_received) == 1
    assert len(cmd_received) == 0
    await db.close()


async def test_subscriber_cancelled_cleanly(tmp_path):
    bus, db = await _make_bus(tmp_path)

    async def consume():
        async for _ in bus.subscribe("cancellable", "command.%"):
            pass  # never reached

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # Queue should be removed from the bus after cancellation
    assert "command.%" not in bus._queues or not bus._queues.get("command.%")
    await db.close()


# ---------------------------------------------------------------------------
# DB failure — in-process delivery still happens
# ---------------------------------------------------------------------------

async def test_publish_delivers_in_process_even_on_db_failure(tmp_path):
    bus, db = await _make_bus(tmp_path)
    received = []

    # Sabotage the DB method
    db.insert_event = AsyncMock(side_effect=RuntimeError("db down"))

    async def consume():
        async for event in bus.subscribe("resilient", "command.%"):
            received.append(event)
            break

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)

    row_id = await bus.publish(TOPIC_COMMAND_EXECUTED, {"ok": True}, source="test")
    await asyncio.sleep(0.05)
    await task

    assert row_id is None          # DB write failed → no row id
    assert len(received) == 1      # but in-process delivery still happened
    await db.close()


# ---------------------------------------------------------------------------
# Consumer cursor tracking
# ---------------------------------------------------------------------------

async def test_upsert_and_update_consumer_cursor(tmp_path):
    _, db = await _make_bus(tmp_path)
    await db.upsert_event_consumer("test_consumer", "command.%")
    await db.update_consumer_cursor("test_consumer", last_event_id=42)

    # Upsert again — should update updated_at but not reset cursor
    await db.upsert_event_consumer("test_consumer", "command.%")

    await db.close()  # no assertion needed — confirms no DB errors


# ---------------------------------------------------------------------------
# Slow-consumer overflow: drop the EVENT, never the subscription
# (audit fix 2026-06-09 — removing the queue permanently hung the consumer)
# ---------------------------------------------------------------------------

async def test_queue_overflow_drops_event_not_subscription(tmp_path):
    bus, db = await _make_bus(tmp_path)
    received = []
    resume = asyncio.Event()

    async def slow_consume():
        async for event in bus.subscribe("slow", "command.%", maxsize=2):
            received.append(event["payload"]["n"])
            await resume.wait()        # consumer stalls after the first event

    task = asyncio.create_task(slow_consume())
    await asyncio.sleep(0)

    # Fill: consumer takes n=1 then stalls; n=2, n=3 fill the maxsize-2 queue;
    # n=4 and n=5 overflow and must be DROPPED (not the subscription).
    for n in range(1, 6):
        await bus.publish(TOPIC_COMMAND_EXECUTED, {"n": n}, source="test")
        await asyncio.sleep(0.01)

    assert bus.dropped_events == 2
    assert bus._queues.get("command.%")           # subscription still registered

    # Consumer resumes: it must receive the buffered events AND later ones —
    # the old behavior left it blocked on q.get() forever.
    resume.set()
    await asyncio.sleep(0.05)
    await bus.publish(TOPIC_COMMAND_EXECUTED, {"n": 6}, source="test")
    await asyncio.sleep(0.05)

    assert received == [1, 2, 3, 6]               # dropped 4,5; still alive for 6
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await db.close()


# ---------------------------------------------------------------------------
# _topic_matches — SQL LIKE parity (mid-pattern % must work like poll_events)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("topic,pattern,expected", [
    ("command.exec.failed", "command.%.failed", True),   # mid-pattern %
    ("command.exec.ok",     "command.%.failed", False),
    ("command.",            "command.%",        True),   # SQL: % matches empty
    ("gate.decided",        "%.decided",        True),
    ("gate.decided",        "%.executed",       False),
])
def test_topic_matches_sql_like_parity(topic, pattern, expected):
    assert _topic_matches(topic, pattern) is expected
