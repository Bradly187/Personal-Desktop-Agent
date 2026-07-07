"""N+2 — ProactiveScheduler: next_occurrence + _tick (promote / flare-skip /
recurrence re-lay) and lifecycle.

Run:
    python -m pytest tests/test_proactive_scheduler.py -q
"""

from __future__ import annotations

import datetime
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.proactive_scheduler import ProactiveScheduler, next_occurrence


def test_next_occurrence_interval():
    assert next_occurrence({"kind": "interval", "every_s": 3600}, 1000.0) == 4600.0


def test_next_occurrence_unknown_is_none():
    assert next_occurrence({"kind": "weekly"}, 1.0) is None
    assert next_occurrence({}, 1.0) is None


def test_next_occurrence_daily_today_then_tomorrow():
    before = datetime.datetime(2026, 6, 13, 6, 0, 0).timestamp()   # 6am, before 08:00
    nxt = datetime.datetime.fromtimestamp(next_occurrence({"kind": "daily", "at": "08:00"}, before))
    assert nxt.hour == 8 and nxt.minute == 0 and nxt.day == 13
    after = datetime.datetime(2026, 6, 13, 9, 0, 0).timestamp()    # 9am, past 08:00
    nxt2 = datetime.datetime.fromtimestamp(next_occurrence({"kind": "daily", "at": "08:00"}, after))
    assert nxt2.hour == 8 and nxt2.day == 14


async def test_tick_promotes_and_kicks_drainer():
    db = MagicMock()
    db.goals.promote_due_goals = AsyncMock(return_value=[
        {"goal": "brief", "recurrence": None, "domain": "plan", "source_trigger": "schedule"}])
    db.goals.enqueue_scheduled_goal = AsyncMock()
    dev = MagicMock(); dev.drain_goal_queue = AsyncMock()
    ps = ProactiveScheduler(db, dev_agent=dev)
    assert await ps._tick(1000.0) == 1
    db.goals.promote_due_goals.assert_awaited_with(1000.0)
    db.goals.enqueue_scheduled_goal.assert_not_awaited()   # one-shot: no re-lay


async def test_tick_relays_recurrence():
    db = MagicMock()
    db.goals.promote_due_goals = AsyncMock(return_value=[
        {"goal": "brief", "recurrence": '{"kind":"interval","every_s":60}',
         "domain": "plan", "source_trigger": "schedule"}])
    db.goals.enqueue_scheduled_goal = AsyncMock()
    dev = MagicMock(); dev.drain_goal_queue = AsyncMock()
    ps = ProactiveScheduler(db, dev_agent=dev)
    await ps._tick(1000.0)
    db.goals.enqueue_scheduled_goal.assert_awaited()
    assert db.goals.enqueue_scheduled_goal.await_args.kwargs["execute_at"] == 1060.0


async def test_tick_skips_during_flare():
    db = MagicMock(); db.goals.promote_due_goals = AsyncMock(return_value=[])
    sched = MagicMock(); sched.dev_paused = True
    ps = ProactiveScheduler(db, scheduler=sched)
    assert await ps._tick(1000.0) == 0
    db.goals.promote_due_goals.assert_not_awaited()   # flare: never even queried


async def test_lifecycle_and_health():
    db = MagicMock(); db.goals.promote_due_goals = AsyncMock(return_value=[])
    ps = ProactiveScheduler(db)
    assert not ps.is_healthy()
    await ps.start()
    assert ps.is_healthy()
    await ps.stop()
    assert not ps.is_healthy()
