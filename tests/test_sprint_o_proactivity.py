"""Sprint O — proactivity & storage correctness regression tests.

Covers the 2026-06-14 audit findings:
  #1  enqueue_goal revives a terminal row (recurring goals not swallowed)
  #2  recurrence re-lay carries a deterministic idempotency_key (no storm)
  #3  interval recurrence advances from intended time, not promotion time
  #7  email watcher dedup survives a restart (no dropped mail)
  #8  _migrate leaves user_version unbumped on a genuine ALTER failure
  #9  requeue_stale_running honors the claim lease (no concurrent-instance clobber)
  #10 event-rule predicate distinguishes a missing field from JSON null
  #16 event-rule cooldown suppresses a same-tick burst

Run:
    python -m pytest tests/test_sprint_o_proactivity.py -q
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from storage.db import AgentDB
from storage.repositories.common import _pid_alive
from core.proactive_scheduler import ProactiveScheduler, next_occurrence
from core.event_rule_engine import EventRuleEngine, _eval_predicate, _get_path, _MISSING
from core.email_watcher import EmailWatcher


@pytest.fixture
async def db(tmp_path):
    d = AgentDB()
    await d.open(tmp_path / "agent.db")
    if not d.available:
        pytest.skip("aiosqlite unavailable")
    yield d
    await d.close()


# ---------------------------------------------------------------------------
# #1 — enqueue_goal revives terminal rows
# ---------------------------------------------------------------------------

async def test_enqueue_revives_terminal_row(db):
    gid = await db.goals.enqueue_goal("recurring", idempotency_key="r1")
    await db.goals.claim_next_goal()
    await db.goals.complete_goal(gid, "done")
    assert await db.goals.claim_next_goal() is None          # terminal, not claimable

    # Same key re-enqueued (e.g. the goal is due again) → revived, same row id.
    gid2 = await db.goals.enqueue_goal("recurring", idempotency_key="r1")
    assert gid2 == gid
    again = await db.goals.claim_next_goal()
    assert again is not None and again["goal"] == "recurring"
    assert again["attempts"] == 1                      # attempts reset on revival


async def test_enqueue_active_row_still_dedupes(db):
    """A still-queued row must NOT be revived/duplicated — that's genuine dedup."""
    a = await db.goals.enqueue_goal("dup", idempotency_key="d1")
    b = await db.goals.enqueue_goal("dup", idempotency_key="d1")
    assert a == b
    assert await db.goals.claim_next_goal() is not None
    assert await db.goals.claim_next_goal() is None           # only one row ever existed


async def test_enqueue_revives_cancelled(db):
    gid = await db.goals.enqueue_goal("g", idempotency_key="c1")
    await db.goals.complete_goal(gid, "cancelled")
    gid2 = await db.goals.enqueue_goal("g", idempotency_key="c1")
    assert gid2 == gid
    assert (await db.goals.claim_next_goal())["goal"] == "g"


# ---------------------------------------------------------------------------
# #3 / #2 — interval drift + re-lay idempotency
# ---------------------------------------------------------------------------

def test_interval_no_prev_is_now_plus_every():
    # Backward-compatible signature: without prev it advances from now.
    assert next_occurrence({"kind": "interval", "every_s": 3600}, 1000.0) == 4600.0


def test_interval_advances_from_intended_not_promotion():
    # Due at 1000, promoted late at 1005 → next is 1000+3600, NOT 1005+3600.
    nxt = next_occurrence({"kind": "interval", "every_s": 3600}, 1005.0,
                          prev_execute_at=1000.0)
    assert nxt == 4600.0


def test_interval_skips_missed_slots_without_storm():
    # Down for ~2.5 intervals: jump to the first slot strictly after now, once.
    nxt = next_occurrence({"kind": "interval", "every_s": 100}, 1250.0,
                          prev_execute_at=1000.0)
    assert nxt == 1300.0                                # 1000 + 3*100, first > 1250


async def test_relay_carries_idempotency_key():
    captured = {}

    async def _enqueue(goal, **kw):
        captured.update(kw)
        captured["goal"] = goal
        return 1

    dbm = MagicMock()
    dbm.goals.promote_due_goals = AsyncMock(return_value=[{
        "goal": "brief", "recurrence": '{"kind":"interval","every_s":60}',
        "domain": "plan", "source_trigger": "schedule", "execute_at": 1000.0}])
    dbm.goals.enqueue_scheduled_goal = AsyncMock(side_effect=_enqueue)
    ps = ProactiveScheduler(dbm, dev_agent=MagicMock(drain_goal_queue=AsyncMock()))
    await ps._tick(1000.0)
    assert captured["idempotency_key"] == "schedule:brief:1060"
    assert captured["execute_at"] == 1060.0


# ---------------------------------------------------------------------------
# #8 — migration finalize-on-fail
# ---------------------------------------------------------------------------

async def test_migrate_does_not_finalize_on_failure(tmp_path, monkeypatch):
    import storage.db as dbmod
    # Inject a migration that targets a non-existent table → genuine failure.
    bad = dbmod._AGENT_DB_MIGRATIONS + (("no_such_table", "col", "TEXT"),)
    monkeypatch.setattr(dbmod, "_AGENT_DB_MIGRATIONS", bad)
    monkeypatch.setattr(dbmod, "_AGENT_DB_SCHEMA_VERSION",
                        dbmod._AGENT_DB_SCHEMA_VERSION + 1)
    d = AgentDB()
    await d.open(tmp_path / "agent.db")
    try:
        cur = await d._conn.execute("PRAGMA user_version")
        version = (await cur.fetchone())[0]
        # The failed ALTER must leave the version unbumped so it retries next boot.
        assert version < dbmod._AGENT_DB_SCHEMA_VERSION
    finally:
        await d.close()


async def test_migrate_finalizes_on_clean_run(db):
    import storage.db as dbmod
    cur = await db._conn.execute("PRAGMA user_version")
    assert (await cur.fetchone())[0] == dbmod._AGENT_DB_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# #9 — requeue lease guard
# ---------------------------------------------------------------------------

def test_pid_alive_self_and_dead():
    assert _pid_alive(os.getpid()) is True
    assert _pid_alive(2_000_000_000) is False           # not a live pid
    assert _pid_alive(None) is False
    assert _pid_alive(-1) is False


async def test_requeue_skips_live_concurrent_owner(db):
    """A 'running' goal owned by a different, live pid is a concurrent instance —
    requeue must leave it alone."""
    gid = await db.goals.enqueue_goal("g", idempotency_key="k")
    await db.goals.claim_next_goal()                           # owner_pid = our pid
    # Re-stamp owner to another LIVE pid (simulate a second instance's claim).
    other = os.getppid() or os.getpid()
    await db._conn.execute(
        "UPDATE goal_queue SET owner_pid=? WHERE id=?", (other, gid))
    await db._conn.commit()
    if other == os.getpid():
        pytest.skip("no distinct live pid available")
    n = await db.runs.requeue_stale_running()
    assert n == 0                                        # left for the live owner
    assert await db.goals.claim_next_goal() is None            # still 'running'


async def test_requeue_recovers_dead_owner(db):
    gid = await db.goals.enqueue_goal("g", idempotency_key="k")
    await db.goals.claim_next_goal()
    await db._conn.execute(
        "UPDATE goal_queue SET owner_pid=? WHERE id=?", (2_000_000_000, gid))
    await db._conn.commit()
    n = await db.runs.requeue_stale_running()
    assert n == 1                                        # dead owner → recovered
    assert (await db.goals.claim_next_goal())["goal"] == "g"


# ---------------------------------------------------------------------------
# #10 — missing vs null
# ---------------------------------------------------------------------------

def test_get_path_missing_vs_null():
    assert _get_path({"a": None}, "a") is None           # present-null → None
    assert _get_path({}, "a") is _MISSING                # absent → sentinel


def test_predicate_exists_distinguishes_null():
    # present-but-null satisfies `exists`; absent does not.
    assert _eval_predicate('[{"path":"x","op":"exists"}]', {"x": None}) is True
    assert _eval_predicate('[{"path":"x","op":"exists"}]', {}) is False


def test_predicate_eq_null_requires_present():
    assert _eval_predicate('[{"path":"x","op":"eq","value":null}]', {"x": None}) is True
    assert _eval_predicate('[{"path":"x","op":"eq","value":null}]', {}) is False


# ---------------------------------------------------------------------------
# #16 — cooldown burst suppression
# ---------------------------------------------------------------------------

async def test_cooldown_suppresses_same_tick_burst():
    notifier = MagicMock(); notifier.notify = AsyncMock()
    dbm = MagicMock()
    rule = {"id": 1, "topic_pattern": "email.arrived", "predicate": None,
            "goal_template": "x", "action_kind": "notify",
            "cooldown_s": 3600, "last_fired_at": None, "name": "n"}
    dbm.events.list_event_rules = AsyncMock(return_value=[rule])
    dbm.events.touch_rule_fired = AsyncMock()
    eng = EventRuleEngine(dbm, MagicMock(), notifier=notifier)
    ev = {"topic": "email.arrived", "payload": {}}
    # Two events in the same tick, before last_fired_at is persisted.
    assert await eng.on_event(ev) == 1
    assert await eng.on_event(ev) == 0                   # cooldown blocks the burst
    assert notifier.notify.await_count == 1


# ---------------------------------------------------------------------------
# #7 — email watcher restart durability
# ---------------------------------------------------------------------------

def _registry(payloads):
    reg = MagicMock()
    reg.has_skills = MagicMock(return_value=True)
    reg._skills = {"google_pim": object()}
    it = iter(payloads)

    async def _call(sid, tool, args):
        return {"status": "ok", "text": json.dumps(next(it))}

    reg.call = _call
    return reg


async def test_email_dedup_survives_restart(tmp_path):
    state = tmp_path / "seen.json"
    bus = MagicMock(); bus.publish = AsyncMock()

    # First process: baseline establishes id "1", persists state.
    reg1 = _registry([[{"id": "1", "from": "a", "subject": "s1"}]])
    w1 = EmailWatcher(reg1, bus, state_path=state)
    assert await w1._tick() == 0                          # baseline, no fire
    bus.publish.assert_not_awaited()
    assert state.exists()

    # Restart: mail "2" arrived during downtime. A fresh watcher must NOT
    # re-baseline — it loads the persisted set and fires for the new id.
    reg2 = _registry([[{"id": "1", "from": "a", "subject": "s1"},
                       {"id": "2", "from": "b", "subject": "s2"}]])
    w2 = EmailWatcher(reg2, bus, state_path=state)
    assert await w2._tick() == 1                          # "2" is new → fired
    bus.publish.assert_awaited_once()
    assert bus.publish.await_args.args[1]["id"] == "2"


async def test_email_no_state_path_is_in_memory_only(tmp_path):
    # Default (no state_path): behaves exactly as before, writes no file.
    bus = MagicMock(); bus.publish = AsyncMock()
    reg = _registry([[{"id": "1"}], [{"id": "1"}, {"id": "2"}]])
    w = EmailWatcher(reg, bus)
    assert await w._tick() == 0
    assert await w._tick() == 1
    assert not list(tmp_path.iterdir())                  # no sidecar written
