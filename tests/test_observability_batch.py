"""Observability batch (2026-06-19) — tests for the five synthesis improvements.

Covers:
  1. Always-on trace persistence — DA_TRACE defaults to enabled, opt-out via =0.
  2. replay_trace(trace_id)       — assembles commands + spans + events + inferences
                                    + audit into one ordered timeline.
  3. Cross-session trend report   — recent-vs-older deltas per metric, polarity-aware.
  4. Cloud/token cost ledger      — rolls up Bedrock spend by model; local excluded.
  5. Silent background-work events — new EventBus topics + CircuitBreaker on_open hook.

Run:
    python -m pytest tests/test_observability_batch.py -q
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from storage.db import AgentDB
from storage.audit_log import AuditLog
from monitoring import replay, trends, cost_ledger
from monitoring.trace import _enabled_from_env
import core.events as events
from core.circuit_breaker import CircuitBreaker


# --------------------------------------------------------------------------- #
# 1. Always-on trace persistence
# --------------------------------------------------------------------------- #

class TestTraceDefaultOn:
    def test_unset_means_enabled(self, monkeypatch):
        monkeypatch.delenv("DA_TRACE", raising=False)
        assert _enabled_from_env() is True

    def test_explicit_off_disables(self, monkeypatch):
        for val in ("0", "false", "no", "off", "OFF", "False"):
            monkeypatch.setenv("DA_TRACE", val)
            assert _enabled_from_env() is False, val

    def test_truthy_enables(self, monkeypatch):
        for val in ("1", "true", "yes", "on"):
            monkeypatch.setenv("DA_TRACE", val)
            assert _enabled_from_env() is True, val


# --------------------------------------------------------------------------- #
# Shared DB helpers
# --------------------------------------------------------------------------- #

async def _open_db(tmp_path, name="agent.db") -> AgentDB:
    db = AgentDB()
    await db.open(tmp_path / name)
    return db


async def _insert_command(db: AgentDB, *, session_id, trace_id, ts,
                          source="voice", action="CLICK", route="local",
                          gate="gate1_confidence", latency=120.0, success=1) -> int:
    async with db._conn.execute(
        "INSERT INTO commands (session_id, ts, source, text, action, route, "
        "gate_that_decided, latency_ms, success, trace_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (session_id, ts, source, "do the thing", action, route, gate,
         latency, success, trace_id),
    ) as cur:
        cmd_id = cur.lastrowid
    await db._conn.commit()
    return cmd_id


# --------------------------------------------------------------------------- #
# 2. replay_trace
# --------------------------------------------------------------------------- #

async def test_replay_trace_assembles_all_layers(tmp_path):
    db = await _open_db(tmp_path)
    if not db.available:
        pytest.skip("aiosqlite unavailable")
    sid = await db.sessions.insert_session(mode="test")
    tid = "trace123"
    cmd_id = await _insert_command(db, session_id=sid, trace_id=tid, ts=1000.0)

    await db.logs.insert_trace_spans(tid, sid, [
        {"stage": "enqueue", "ts": 1000.1, "dur_ms": 1.0},
        {"stage": "route_decision", "ts": 1000.2, "dur_ms": 12.0, "attrs": {"route": "local"}},
        {"stage": "execute", "ts": 1000.3, "dur_ms": 5.0, "attrs": {"verb": "CLICK"}},
    ])
    await db.events.insert_event("command.executed", json.dumps({"action": "CLICK"}),
                          "coordinator", session_id=sid, command_id=cmd_id, trace_id=tid)
    await db.inferences.insert_inference(command_id=cmd_id, model="claude-haiku-4-5",
                             domain="command", prompt=None, response=None,
                             tokens_in=100, tokens_out=40, latency_ms=200.0,
                             backend="bedrock")

    audit = AuditLog()
    await audit.open(tmp_path / "audit.db")
    if audit.available:
        audit.set_session_id(sid)
        await audit.log("mcp_call", tool="mouse_click", command_id=cmd_id)
        await audit.close()
    await db.close()

    result = replay.replay_trace(tid, str(tmp_path / "agent.db"),
                                 str(tmp_path / "audit.db"))
    s = result["summary"]
    assert s["found"] is True
    assert s["session_id"] == sid
    assert s["route"] == "local"
    assert s["n_spans"] == 3
    assert s["n_events"] == 1
    assert s["n_inferences"] == 1
    assert s["tokens_in"] == 100 and s["tokens_out"] == 40
    if audit.available:
        assert s["n_audit"] == 1
    # Timeline includes every layer and is ts-sorted.
    kinds = {e["kind"] for e in result["timeline"]}
    assert {"command", "span", "event", "inference"} <= kinds
    tvals = [e["t"] for e in result["timeline"]]
    assert tvals == sorted(tvals)
    # format_timeline must not raise and mentions the trace id.
    assert tid in replay.format_timeline(result)


async def test_replay_trace_unknown_id_is_safe(tmp_path):
    db = await _open_db(tmp_path)
    if not db.available:
        pytest.skip("aiosqlite unavailable")
    await db.close()
    result = replay.replay_trace("nope", str(tmp_path / "agent.db"),
                                 str(tmp_path / "audit.db"))
    assert result["summary"]["found"] is False
    assert result["timeline"] == []
    assert "No command found" in replay.format_timeline(result)


async def test_recent_traces_lists_newest_first(tmp_path):
    db = await _open_db(tmp_path)
    if not db.available:
        pytest.skip("aiosqlite unavailable")
    sid = await db.sessions.insert_session(mode="test")
    await _insert_command(db, session_id=sid, trace_id="old", ts=1000.0)
    await _insert_command(db, session_id=sid, trace_id="new", ts=2000.0)
    await db.close()
    rows = replay.recent_traces(str(tmp_path / "agent.db"), limit=10)
    assert [r["trace_id"] for r in rows] == ["new", "old"]


# --------------------------------------------------------------------------- #
# 3. Cross-session trend report
# --------------------------------------------------------------------------- #

async def test_session_trends_detects_direction(tmp_path):
    db = await _open_db(tmp_path)
    if not db.available:
        pytest.skip("aiosqlite unavailable")
    # 4 sessions: cloud rate rises over time (worsening), success rate rises (improving).
    rows = [
        dict(session_id=1, ts=1000.0, total_commands=10, success_rate=0.80,
             cloud_escalation_rate=0.10, latency_p50_ms=200, latency_p95_ms=500,
             pain_day_pct=0.0, corrections_count=1),
        dict(session_id=2, ts=2000.0, total_commands=12, success_rate=0.82,
             cloud_escalation_rate=0.12, latency_p50_ms=210, latency_p95_ms=520,
             pain_day_pct=0.0, corrections_count=1),
        dict(session_id=3, ts=3000.0, total_commands=11, success_rate=0.95,
             cloud_escalation_rate=0.40, latency_p50_ms=205, latency_p95_ms=515,
             pain_day_pct=0.0, corrections_count=0),
        dict(session_id=4, ts=4000.0, total_commands=13, success_rate=0.97,
             cloud_escalation_rate=0.45, latency_p50_ms=205, latency_p95_ms=515,
             pain_day_pct=0.0, corrections_count=0),
    ]
    for r in rows:
        await db.sessions.insert_session_summary(r)
    await db.close()

    result = trends.session_trends(str(tmp_path / "agent.db"), limit=30)
    assert result["n_sessions"] == 4
    # Display order oldest→newest.
    assert [s["session_id"] for s in result["sessions"]] == [1, 2, 3, 4]
    d = result["deltas"]
    assert d["cloud_escalation_rate"]["verdict"] == "worsening"  # lower is better, it rose
    assert d["success_rate"]["verdict"] == "improving"            # higher is better, it rose
    assert "worsening" in trends.format_trends(result)


# --------------------------------------------------------------------------- #
# 4. Cloud/token cost ledger
# --------------------------------------------------------------------------- #

async def test_cost_rollup_counts_cloud_only(tmp_path):
    db = await _open_db(tmp_path)
    if not db.available:
        pytest.skip("aiosqlite unavailable")
    sid = await db.sessions.insert_session(mode="test")
    cmd_id = await _insert_command(db, session_id=sid, trace_id="t", ts=1000.0)
    # Opus: 1M in + 1M out → 1*5 + 1*25 = $30 at list price.
    await db.inferences.insert_inference(command_id=cmd_id, model="claude-opus-4-8",
                             domain="code", prompt=None, response=None,
                             tokens_in=1_000_000, tokens_out=1_000_000,
                             latency_ms=0.0, backend="bedrock")
    # Local llama — has tokens but is unpriced → excluded from the cloud ledger.
    await db.inferences.insert_inference(command_id=cmd_id, model="llama3.1:8b",
                             domain="command", prompt=None, response=None,
                             tokens_in=500, tokens_out=200, latency_ms=30.0,
                             backend="ollama")
    await db.close()

    result = cost_ledger.cost_rollup(str(tmp_path / "agent.db"), days=None)
    assert result["n_cloud_inferences"] == 1
    assert result["totals"]["cost"] == pytest.approx(30.0)
    assert "claude-opus-4-8" in result["by_model"]
    assert "llama3.1:8b" not in result["by_model"]
    # Session attribution via the linked command.
    assert sid in result["by_session"]
    assert "claude-opus-4-8" in cost_ledger.format_rollup(result)


async def test_cost_rollup_price_override(tmp_path, monkeypatch):
    db = await _open_db(tmp_path)
    if not db.available:
        pytest.skip("aiosqlite unavailable")
    sid = await db.sessions.insert_session(mode="test")
    cmd_id = await _insert_command(db, session_id=sid, trace_id="t", ts=1000.0)
    await db.inferences.insert_inference(command_id=cmd_id, model="claude-opus-4-8",
                             domain="code", prompt=None, response=None,
                             tokens_in=1_000_000, tokens_out=0,
                             latency_ms=0.0, backend="bedrock")
    await db.close()
    monkeypatch.setenv("DA_BEDROCK_PRICES", json.dumps({"claude-opus-4-8": [10.0, 99.0]}))
    result = cost_ledger.cost_rollup(str(tmp_path / "agent.db"), days=None)
    assert result["totals"]["cost"] == pytest.approx(10.0)  # 1M in × $10/MTok


# --------------------------------------------------------------------------- #
# 5. Silent background-work events
# --------------------------------------------------------------------------- #

def test_new_event_topics_are_distinct():
    topics = [
        events.TOPIC_GOAL_DEQUEUED, events.TOPIC_GOAL_COMPLETED,
        events.TOPIC_VRAM_EVICTED, events.TOPIC_VRAM_RESTORED,
        events.TOPIC_BREAKER_OPENED, events.TOPIC_INFERENCE_STALLED,
    ]
    assert len(set(topics)) == 6
    assert all(isinstance(t, str) and "." in t for t in topics)


def test_circuit_breaker_on_open_fires_once_per_transition():
    seen: list[dict] = []
    cb = CircuitBreaker(name="ollama", fail_threshold=1, cooldown_s=30.0,
                        on_open=lambda s: seen.append(s))
    cb.record_failure()                       # closed → open: fires
    assert len(seen) == 1
    assert seen[0]["name"] == "ollama" and seen[0]["state"] == "open"
    assert "reason" in seen[0]
    cb.record_failure()                       # already open: must NOT fire again
    assert len(seen) == 1


def test_circuit_breaker_on_open_optional():
    # No callback → no crash on open.
    cb = CircuitBreaker(name="x", fail_threshold=1, cooldown_s=5.0)
    cb.record_failure()
    assert cb.state == "open"


async def test_eventbus_publishes_background_topic(tmp_path):
    db = await _open_db(tmp_path)
    if not db.available:
        pytest.skip("aiosqlite unavailable")
    bus = events.EventBus(db)
    await bus.publish(events.TOPIC_GOAL_DEQUEUED,
                      {"goal_id": 7, "goal": "tidy up", "source_trigger": "manual"},
                      source="dev_agent")
    async with db._conn.execute(
        "SELECT topic, payload FROM event_log WHERE topic = ?",
        (events.TOPIC_GOAL_DEQUEUED,),
    ) as cur:
        row = await cur.fetchone()
    await db.close()
    assert row is not None
    assert json.loads(row[1])["goal_id"] == 7
