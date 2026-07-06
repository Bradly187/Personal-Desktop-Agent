"""N+2 — proactive scheduling DB layer: scheduled goals + event rules.

Run:
    python -m pytest tests/test_proactive_db.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from storage.db import AgentDB


async def _db(tmp_path) -> AgentDB:
    db = AgentDB()
    await db.open(tmp_path / "agent.db")
    return db


async def test_scheduled_goal_invisible_until_promoted(tmp_path):
    db = await _db(tmp_path)
    try:
        gid = await db.goals.enqueue_scheduled_goal("brief me", execute_at=1000.0,
                                              source_trigger="schedule")
        assert gid > 0
        # The hot claim path must NOT see a scheduled goal.
        assert await db.goals.claim_next_goal() is None
        # Not due yet.
        assert await db.goals.promote_due_goals(999.0) == []
        # Due → promoted → claimable.
        promoted = await db.goals.promote_due_goals(1001.0)
        assert len(promoted) == 1 and promoted[0]["goal"] == "brief me"
        claimed = await db.goals.claim_next_goal()
        assert claimed is not None and claimed["goal"] == "brief me"
    finally:
        await db.close()


async def test_list_and_cancel_schedule(tmp_path):
    db = await _db(tmp_path)
    try:
        gid = await db.goals.enqueue_scheduled_goal("water plants", execute_at=5000.0)
        assert any(s["id"] == gid for s in await db.goals.list_schedules())
        assert await db.goals.cancel_schedule(gid) is True
        assert all(s["id"] != gid for s in await db.goals.list_schedules())
        assert await db.goals.cancel_schedule(gid) is False  # already cancelled
    finally:
        await db.close()


async def test_recurrence_carried_on_promote(tmp_path):
    db = await _db(tmp_path)
    try:
        rec = '{"kind":"daily","at":"08:00"}'
        await db.goals.enqueue_scheduled_goal("morning brief", execute_at=1.0, recurrence=rec)
        promoted = await db.goals.promote_due_goals(2.0)
        assert promoted and promoted[0]["recurrence"] == rec
    finally:
        await db.close()


async def test_event_rules_crud(tmp_path):
    db = await _db(tmp_path)
    try:
        rid = await db.events.insert_event_rule(
            topic_pattern="email.arrived", goal_template="Email from {from}",
            name="boss mail",
            predicate='[{"path":"from","op":"contains","value":"boss"}]',
            action_kind="notify", cooldown_s=60.0)
        assert rid > 0
        rules = await db.events.list_event_rules()
        assert any(r["id"] == rid and r["topic_pattern"] == "email.arrived" for r in rules)
        await db.events.touch_rule_fired(rid, 1234.0)
        r = next(r for r in await db.events.list_event_rules() if r["id"] == rid)
        assert r["fire_count"] == 1 and r["last_fired_at"] == 1234.0
        assert await db.events.cancel_event_rule(rid) is True
        assert all(r["id"] != rid for r in await db.events.list_event_rules(enabled_only=True))
    finally:
        await db.close()
