"""Tests for cross-layer tracing (monitoring/trace.py) + DA_TRACE gating."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from monitoring.trace import TraceRecorder, get_tracer
from core.command_executor import Command
from core.hybrid_coordinator import HybridCoordinator, CoordinatorConfig


# ---------------------------------------------------------------------------
# Disabled = zero-cost no-op
# ---------------------------------------------------------------------------

class TestDisabled:
    def test_disabled_new_trace_returns_empty(self):
        t = TraceRecorder(enabled=False)
        assert t.new_trace(source="voice") == ""

    def test_disabled_record_span_is_noop(self):
        t = TraceRecorder(enabled=False)
        t.record_span("route_decision", trace_id="abc", route="local")
        assert t.get_recent() == []
        assert t.get_trace("abc") is None

    def test_disabled_timed_is_noop(self):
        t = TraceRecorder(enabled=False)
        with t.timed("execute", trace_id="abc"):
            pass
        assert t.get_recent() == []


# ---------------------------------------------------------------------------
# Enabled recording
# ---------------------------------------------------------------------------

class TestEnabled:
    def test_new_trace_and_record_span(self):
        t = TraceRecorder(enabled=True)
        tid = t.new_trace(source="voice")
        assert tid
        t.record_span("route_decision", trace_id=tid, route="local", dur_ms=12.3)
        tr = t.get_trace(tid)
        assert tr["spans"][0]["stage"] == "route_decision"
        assert tr["spans"][0]["dur_ms"] == 12.3
        assert tr["spans"][0]["attrs"]["route"] == "local"

    def test_record_span_uses_contextvar_when_no_id(self):
        t = TraceRecorder(enabled=True)
        tid = t.new_trace()
        tok = t.set_current(tid)
        try:
            t.record_span("execute", verb="CLICK")     # no explicit trace_id
        finally:
            t.reset_current(tok)
        assert [s["stage"] for s in t.get_trace(tid)["spans"]] == ["execute"]

    def test_timed_records_duration(self):
        t = TraceRecorder(enabled=True)
        tid = t.new_trace()
        with t.timed("inference", trace_id=tid, route="cloud"):
            pass
        span = t.get_trace(tid)["spans"][0]
        assert span["stage"] == "inference"
        assert "dur_ms" in span and span["dur_ms"] >= 0

    def test_ring_buffer_evicts_oldest(self):
        t = TraceRecorder(enabled=True, maxlen=3)
        ids = [t.new_trace() for _ in range(5)]
        recent_ids = {tr["id"] for tr in t.get_recent(99)}
        assert len(recent_ids) == 3
        assert ids[0] not in recent_ids and ids[-1] in recent_ids

    def test_none_attrs_are_dropped(self):
        t = TraceRecorder(enabled=True)
        tid = t.new_trace()
        t.record_span("route_decision", trace_id=tid, route="local", domain=None)
        attrs = t.get_trace(tid)["spans"][0]["attrs"]
        assert "domain" not in attrs and attrs["route"] == "local"


# ---------------------------------------------------------------------------
# Command field
# ---------------------------------------------------------------------------

def test_command_has_trace_id_default():
    cmd = Command(text="x", action="CLICK", source="touch")
    assert cmd.trace_id == ""


# ---------------------------------------------------------------------------
# Integration: route() produces a linked trace when enabled
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_route_produces_trace_when_enabled():
    tracer = get_tracer()
    tracer.set_enabled(True)
    try:
        coord = HybridCoordinator(config=CoordinatorConfig())
        coord._executor.execute = AsyncMock(return_value={"status": "ok"})
        # Pre-supply a trace_id: route() is now immutable (uses _dc_replace
        # internally) so the caller's cmd object is not mutated.  Supplying
        # the id up-front lets us verify the tracer recorded spans under it.
        pre_tid = tracer.new_trace(source="touch")
        cmd = Command(text="click", action="CLICK", source="touch",
                      params={"x": 10, "y": 20}, trace_id=pre_tid)
        await coord.route(cmd)

        assert cmd.trace_id == pre_tid, "pre-supplied trace_id should be unchanged"
        tr = tracer.get_trace(pre_tid)
        assert tr is not None, "tracer should have recorded spans for the trace"
        stages = [s["stage"] for s in tr["spans"]]
        assert "execute" in stages
        assert "route_decision" in stages
    finally:
        tracer.set_enabled(False)


@pytest.mark.asyncio
async def test_route_no_trace_when_disabled():
    tracer = get_tracer()
    tracer.set_enabled(False)
    coord = HybridCoordinator(config=CoordinatorConfig())
    coord._executor.execute = AsyncMock(return_value={"status": "ok"})
    cmd = Command(text="click", action="CLICK", source="touch", params={"x": 1, "y": 2})
    await coord.route(cmd)
    assert cmd.trace_id == ""
    assert tracer.get_trace("") is None
