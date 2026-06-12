"""N+2 — EventRuleEngine: predicate eval, render, on_event (notify / enqueue /
cooldown / predicate-filter).

Run:
    python -m pytest tests/test_event_rule_engine.py -q
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.event_rule_engine import EventRuleEngine, _eval_predicate, _render


def test_eval_predicate_ops():
    contains = '[{"path":"from","op":"contains","value":"boss"}]'
    assert _eval_predicate(contains, {"from": "The BOSS <b@x>"}) is True
    assert _eval_predicate(contains, {"from": "alice@x"}) is False
    assert _eval_predicate('[{"path":"label","op":"eq","value":"urgent"}]', {"label": "urgent"}) is True
    assert _eval_predicate('[{"path":"x","op":"exists"}]', {"x": 1}) is True
    assert _eval_predicate('[{"path":"x","op":"exists"}]', {}) is False
    assert _eval_predicate(None, {}) is True   # no predicate → match any


def test_render():
    assert _render("From {from}: {subject}", {"from": "bob", "subject": "hi"}) == "From bob: hi"


def _engine(rules, *, notifier=None, dev=None):
    db = MagicMock()
    db.list_event_rules = AsyncMock(return_value=rules)
    db.touch_rule_fired = AsyncMock()
    db.enqueue_goal = AsyncMock(return_value=1)
    return EventRuleEngine(db, MagicMock(), notifier=notifier, dev_agent=dev), db


async def test_notify_rule_fires_on_match():
    notifier = MagicMock(); notifier.notify = AsyncMock()
    eng, db = _engine([{
        "id": 1, "topic_pattern": "email.arrived",
        "predicate": '[{"path":"from","op":"contains","value":"boss"}]',
        "goal_template": "Email from {from}", "action_kind": "notify",
        "cooldown_s": 0, "last_fired_at": None, "name": "boss mail"}], notifier=notifier)
    assert await eng.on_event({"topic": "email.arrived", "payload": {"from": "boss@x.com"}}) == 1
    notifier.notify.assert_awaited()
    db.touch_rule_fired.assert_awaited()


async def test_predicate_filters_non_match():
    notifier = MagicMock(); notifier.notify = AsyncMock()
    eng, db = _engine([{
        "id": 1, "topic_pattern": "email.arrived",
        "predicate": '[{"path":"from","op":"contains","value":"boss"}]',
        "goal_template": "x", "action_kind": "notify",
        "cooldown_s": 0, "last_fired_at": None, "name": "n"}], notifier=notifier)
    assert await eng.on_event({"topic": "email.arrived", "payload": {"from": "news@x"}}) == 0
    notifier.notify.assert_not_awaited()


async def test_topic_must_match():
    notifier = MagicMock(); notifier.notify = AsyncMock()
    eng, _ = _engine([{
        "id": 1, "topic_pattern": "email.arrived", "predicate": None,
        "goal_template": "x", "action_kind": "notify",
        "cooldown_s": 0, "last_fired_at": None, "name": "n"}], notifier=notifier)
    assert await eng.on_event({"topic": "command.executed", "payload": {}}) == 0


async def test_cooldown_blocks_repeat():
    notifier = MagicMock(); notifier.notify = AsyncMock()
    eng, _ = _engine([{
        "id": 1, "topic_pattern": "email.arrived", "predicate": None,
        "goal_template": "x", "action_kind": "notify",
        "cooldown_s": 3600, "last_fired_at": time.time(), "name": "n"}], notifier=notifier)
    assert await eng.on_event({"topic": "email.arrived", "payload": {}}) == 0


async def test_enqueue_goal_action():
    dev = MagicMock(); dev.drain_goal_queue = AsyncMock()
    eng, db = _engine([{
        "id": 2, "topic_pattern": "build.failed", "predicate": None,
        "goal_template": "fix the build", "action_kind": "enqueue_goal",
        "cooldown_s": 0, "last_fired_at": None, "name": "b"}], dev=dev)
    assert await eng.on_event({"topic": "build.failed", "payload": {"id": 99}}) == 1
    db.enqueue_goal.assert_awaited()
