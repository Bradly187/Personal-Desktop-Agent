"""GAP-4 — durable command traces.

TraceRecorder.persist_trace flushes a completed trace's spans to AgentDB
(command_traces), and they round-trip back via get_trace_spans. Persistence is
fire-and-forget and must never raise — disabled tracing / unknown trace / no DB
all return 0.

Run:
    python -m pytest tests/test_trace_persist.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from storage.db import AgentDB
from monitoring.trace import TraceRecorder


async def _open_db(tmp_path) -> AgentDB:
    db = AgentDB()
    await db.open(tmp_path / "trace.db")
    return db


async def test_persist_and_read_back(tmp_path):
    db = await _open_db(tmp_path)
    if not db.available:
        pytest.skip("aiosqlite unavailable")

    t = TraceRecorder(enabled=True)
    tid = t.new_trace(source="voice")
    t.record_span("enqueue", trace_id=tid, source="voice")
    t.record_span("route_decision", trace_id=tid, route="local", dur_ms=12.3)
    t.record_span("execute", trace_id=tid, verb="CLICK")

    n = await t.persist_trace(tid, db, session_id=7)
    assert n == 3

    spans = await db.logs.get_trace_spans(tid)
    assert [s["stage"] for s in spans] == ["enqueue", "route_decision", "execute"]
    assert spans[0]["seq"] == 0 and spans[2]["seq"] == 2
    # attrs round-trip as JSON
    assert spans[2]["attrs"]["verb"] == "CLICK"
    assert spans[1]["dur_ms"] == pytest.approx(12.3)


async def test_disabled_tracer_persists_nothing(tmp_path):
    db = await _open_db(tmp_path)
    if not db.available:
        pytest.skip("aiosqlite unavailable")
    t = TraceRecorder(enabled=False)
    tid = "deadbeef"
    # record_span is a no-op when disabled, so there are no spans to write.
    assert await t.persist_trace(tid, db, session_id=1) == 0
    assert await db.logs.get_trace_spans(tid) == []


async def test_unknown_trace_and_no_db_are_safe(tmp_path):
    db = await _open_db(tmp_path)
    if not db.available:
        pytest.skip("aiosqlite unavailable")
    t = TraceRecorder(enabled=True)
    # unknown trace id → 0, no raise
    assert await t.persist_trace("nope", db, session_id=1) == 0
    # missing DB → 0, no raise
    tid = t.new_trace()
    t.record_span("execute", trace_id=tid, verb="OPEN")
    assert await t.persist_trace(tid, None, session_id=1) == 0
