"""Tests for orchestration-layer AgentDB methods added in schema v3.

Covers: insert_event, poll_events, upsert_event_consumer, update_consumer_cursor,
        insert_saga_compensation, update_saga_compensation, get_pending_compensations,
        insert_tool_call (idempotency UNIQUE INDEX), get_tool_call_by_idempotency,
        get_tool_timeout, get_cache_config, get_rate_limit_config, insert_rate_limit_event,
        insert_agent_step (returns row id, compensation columns).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from storage.db import AgentDB


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

async def _open(tmp_path, name="test.db") -> AgentDB:
    db = AgentDB()
    await db.open(tmp_path / name)
    return db


async def _seed_run(db: AgentDB) -> int:
    sid = await db.sessions.insert_session(mode="test")
    return await db.runs.start_agent_run("test goal", "plan", "test-model",
                                    command_id=None)


# ---------------------------------------------------------------------------
# insert_event / poll_events
# ---------------------------------------------------------------------------

async def test_insert_and_poll_event(tmp_path):
    db = await _open(tmp_path)
    row_id = await db.events.insert_event(
        "command.executed", '{"action":"CLICK"}', "coordinator",
        session_id=1, command_id=5, trace_id="t1",
    )
    assert row_id is not None and row_id > 0

    events = await db.events.poll_events("command.executed", last_event_id=0)
    assert len(events) == 1
    assert events[0]["source"] == "coordinator"
    assert events[0]["trace_id"] == "t1"
    await db.close()


async def test_poll_events_cursor_excludes_seen(tmp_path):
    db = await _open(tmp_path)
    id1 = await db.events.insert_event("command.executed", "{}", "c")
    id2 = await db.events.insert_event("command.executed", "{}", "c")

    after = await db.events.poll_events("command.executed", last_event_id=id1)
    assert len(after) == 1
    assert after[0]["id"] == id2
    await db.close()


async def test_poll_events_topic_filter(tmp_path):
    db = await _open(tmp_path)
    await db.events.insert_event("command.executed", "{}", "c")
    await db.events.insert_event("gate.decided", "{}", "c")

    cmd = await db.events.poll_events("command.executed", last_event_id=0)
    gate = await db.events.poll_events("gate.decided", last_event_id=0)

    assert len(cmd) == 1
    assert len(gate) == 1
    await db.close()


# ---------------------------------------------------------------------------
# upsert_event_consumer / update_consumer_cursor
# ---------------------------------------------------------------------------

async def test_consumer_cursor_round_trip(tmp_path):
    db = await _open(tmp_path)
    await db.events.upsert_event_consumer("watcher", "command.%")
    await db.events.update_consumer_cursor("watcher", 99)

    # Second upsert should not reset cursor
    await db.events.upsert_event_consumer("watcher", "command.%")

    # Verify via direct query
    async with db._conn.execute(
        "SELECT last_event_id FROM event_consumers WHERE consumer_name = 'watcher'"
    ) as cur:
        row = await cur.fetchone()
    assert row["last_event_id"] == 99
    await db.close()


# ---------------------------------------------------------------------------
# Saga compensation
# ---------------------------------------------------------------------------

async def test_insert_and_get_pending_compensations(tmp_path):
    db = await _open(tmp_path)
    run_id = await _seed_run(db)

    # Insert two steps with compensations
    step1_id = await db.runs.insert_agent_step(
        run_id, 1, "WRITE_FILE", "/tmp/a.py", None, "ok", True, 10.0,
        compensation_action="DELETE_FILE", compensation_args="/tmp/a.py",
    )
    step2_id = await db.runs.insert_agent_step(
        run_id, 2, "WRITE_FILE", "/tmp/b.py", None, "ok", True, 10.0,
        compensation_action="DELETE_FILE", compensation_args="/tmp/b.py",
    )
    assert step1_id is not None
    assert step2_id is not None

    await db.sagas.insert_saga_compensation(run_id, step1_id, "DELETE_FILE", "/tmp/a.py")
    await db.sagas.insert_saga_compensation(run_id, step2_id, "DELETE_FILE", "/tmp/b.py")

    pending = await db.sagas.get_pending_compensations(run_id)
    assert len(pending) == 2
    # Returned in reverse step order (highest step_num first)
    assert pending[0]["compensation_action"] == "DELETE_FILE"
    assert pending[0]["compensation_args"] == "/tmp/b.py"
    assert pending[1]["compensation_args"] == "/tmp/a.py"
    await db.close()


async def test_update_saga_compensation_status(tmp_path):
    db = await _open(tmp_path)
    run_id = await _seed_run(db)
    step_id = await db.runs.insert_agent_step(
        run_id, 1, "WRITE_FILE", "/f", None, None, None, 0.0,
        compensation_action="DELETE_FILE",
    )
    comp_id = await db.sagas.insert_saga_compensation(run_id, step_id, "DELETE_FILE", "/f")

    await db.sagas.update_saga_compensation(comp_id, "running", triggered_by="max_replans")
    await db.sagas.update_saga_compensation(comp_id, "done", finished=True)

    async with db._conn.execute(
        "SELECT status, triggered_by, finished_at FROM saga_compensations WHERE id = ?",
        (comp_id,),
    ) as cur:
        row = await cur.fetchone()
    assert row["status"] == "done"
    assert row["triggered_by"] == "max_replans"
    assert row["finished_at"] is not None
    await db.close()


async def test_get_pending_compensations_excludes_done(tmp_path):
    db = await _open(tmp_path)
    run_id = await _seed_run(db)
    step_id = await db.runs.insert_agent_step(run_id, 1, "WRITE_FILE", "/f", None, None, None, 0.0,
                                          compensation_action="DELETE_FILE")
    comp_id = await db.sagas.insert_saga_compensation(run_id, step_id, "DELETE_FILE", "/f")
    await db.sagas.update_saga_compensation(comp_id, "done", finished=True)

    pending = await db.sagas.get_pending_compensations(run_id)
    assert pending == []
    await db.close()


# ---------------------------------------------------------------------------
# insert_agent_step returns row id + compensation columns persisted
# ---------------------------------------------------------------------------

async def test_insert_agent_step_returns_row_id(tmp_path):
    db = await _open(tmp_path)
    run_id = await _seed_run(db)
    step_id = await db.runs.insert_agent_step(
        run_id, 1, "WRITE_FILE", "/tmp/x", None, "ok", True, 5.0,
    )
    assert isinstance(step_id, int) and step_id > 0
    await db.close()


async def test_insert_agent_step_persists_compensation_columns(tmp_path):
    db = await _open(tmp_path)
    run_id = await _seed_run(db)
    step_id = await db.runs.insert_agent_step(
        run_id, 1, "WRITE_FILE", "/f", None, None, True, 0.0,
        compensation_action="DELETE_FILE", compensation_args="/f",
    )
    async with db._conn.execute(
        "SELECT compensation_action, compensation_args FROM agent_steps WHERE id = ?",
        (step_id,),
    ) as cur:
        row = await cur.fetchone()
    assert row["compensation_action"] == "DELETE_FILE"
    assert row["compensation_args"] == "/f"
    await db.close()


# ---------------------------------------------------------------------------
# insert_tool_call — idempotency UNIQUE INDEX
# ---------------------------------------------------------------------------

async def test_insert_tool_call_returns_row_id(tmp_path):
    db = await _open(tmp_path)
    row_id = await db.logs.insert_tool_call(
        "write_file",
        idempotency_key="key1",
        result_json='{"written": "/f"}',
        success=True,
        latency_ms=50.0,
        timeout_ms=15000,
        status="completed",
    )
    assert row_id is not None and row_id > 0
    await db.close()


async def test_insert_tool_call_idempotency_blocks_duplicate(tmp_path):
    db = await _open(tmp_path)
    key = "sha256-of-same-args"
    row1 = await db.logs.insert_tool_call("write_file", idempotency_key=key,
                                     result_json='{"n":1}', success=True, status="completed")
    row2 = await db.logs.insert_tool_call("write_file", idempotency_key=key,
                                     result_json='{"n":2}', success=True, status="completed")
    assert row1 is not None
    assert row2 is None  # INSERT OR IGNORE → duplicate key blocked
    await db.close()


async def test_insert_tool_call_non_idempotent_allows_duplicates(tmp_path):
    db = await _open(tmp_path)
    # NULL idempotency_key → no UNIQUE constraint → both inserts succeed
    row1 = await db.logs.insert_tool_call("run_terminal", result_json='{"out":"a"}',
                                     success=True, status="completed")
    row2 = await db.logs.insert_tool_call("run_terminal", result_json='{"out":"b"}',
                                     success=True, status="completed")
    assert row1 is not None
    assert row2 is not None
    assert row1 != row2
    await db.close()


async def test_get_tool_call_by_idempotency_returns_cached(tmp_path):
    db = await _open(tmp_path)
    key = "abc"
    await db.logs.insert_tool_call("write_file", idempotency_key=key,
                               result_json='{"written":"/f"}', success=True, status="completed")
    cached = await db.logs.get_tool_call_by_idempotency(key)
    assert cached is not None
    assert cached["tool_name"] == "write_file"
    import json
    assert json.loads(cached["result_json"])["written"] == "/f"
    await db.close()


async def test_get_tool_call_by_idempotency_returns_none_for_unknown(tmp_path):
    db = await _open(tmp_path)
    result = await db.logs.get_tool_call_by_idempotency("no-such-key")
    assert result is None
    await db.close()


# ---------------------------------------------------------------------------
# Config reads — seeded values
# ---------------------------------------------------------------------------

async def test_get_tool_timeout_returns_seeded_value(tmp_path):
    db = await _open(tmp_path)
    timeout_ms, max_retries = await db.logs.get_tool_timeout("write_file")
    assert timeout_ms == 15_000
    assert max_retries == 1
    await db.close()


async def test_get_tool_timeout_fallback_for_unknown(tmp_path):
    db = await _open(tmp_path)
    timeout_ms, max_retries = await db.logs.get_tool_timeout("nonexistent_tool")
    assert timeout_ms == 15_000  # default fallback
    assert max_retries == 0
    await db.close()


async def test_get_cache_config_returns_seeded_value(tmp_path):
    db = await _open(tmp_path)
    ttl_s, max_entries = await db.misc.get_cache_config("vision_grounder")
    assert ttl_s == pytest.approx(2.0)
    assert max_entries == 200
    await db.close()


async def test_get_cache_config_fallback_for_unknown(tmp_path):
    db = await _open(tmp_path)
    ttl_s, max_entries = await db.misc.get_cache_config("no_such_tool")
    assert ttl_s == pytest.approx(2.0)  # fallback
    assert max_entries == 200
    await db.close()


async def test_get_rate_limit_config_returns_seeded_value(tmp_path):
    db = await _open(tmp_path)
    max_rps, burst = await db.logs.get_rate_limit_config("cloud_api")
    assert max_rps == pytest.approx(2.0)
    assert burst == 5
    await db.close()


async def test_get_rate_limit_config_fallback_for_unknown(tmp_path):
    db = await _open(tmp_path)
    max_rps, burst = await db.logs.get_rate_limit_config("unknown_resource")
    assert max_rps > 0
    assert burst >= 1
    await db.close()


# ---------------------------------------------------------------------------
# insert_rate_limit_event
# ---------------------------------------------------------------------------

async def test_insert_rate_limit_event(tmp_path):
    db = await _open(tmp_path)
    await db.events.insert_rate_limit_event("cloud_api", command_id=None,
                                     wait_ms=250.0, was_dropped=False)
    async with db._conn.execute(
        "SELECT wait_ms, was_dropped FROM rate_limit_events WHERE resource = 'cloud_api'"
    ) as cur:
        row = await cur.fetchone()
    assert row["wait_ms"] == pytest.approx(250.0)
    assert row["was_dropped"] == 0
    await db.close()
