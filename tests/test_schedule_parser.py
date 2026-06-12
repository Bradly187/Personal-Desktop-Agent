"""N+2 — schedule_parser: voice grammar for reminders / schedules / event rules.

Run:
    python -m pytest tests/test_schedule_parser.py -q
"""

from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.schedule_parser import parse, is_schedule_phrase

NOW = datetime.datetime(2026, 6, 13, 6, 0, 0).timestamp()   # 6am local, before 08:00


def test_every_morning_defaults_to_8am():
    r = parse("every morning brief me on my day", NOW)
    assert r["kind"] == "schedule" and r["goal"] == "brief me on my day"
    assert json.loads(r["recurrence"]) == {"kind": "daily", "at": "08:00"}
    assert datetime.datetime.fromtimestamp(r["execute_at"]).hour == 8


def test_every_day_at_explicit_time():
    r = parse("every day at 7:30 water the plants", NOW)
    assert json.loads(r["recurrence"]) == {"kind": "daily", "at": "07:30"}
    assert r["goal"] == "water the plants"


def test_every_n_minutes_interval():
    r = parse("every 15 minutes check the build", NOW)
    assert json.loads(r["recurrence"]) == {"kind": "interval", "every_s": 900}
    assert r["execute_at"] == NOW + 900


def test_in_n_minutes_is_one_shot():
    r = parse("in 10 minutes tell me to stretch", NOW)
    assert r["recurrence"] is None and r["execute_at"] == NOW + 600


def test_remind_me_at_pm():
    r = parse("remind me at 5pm to take medication", NOW)
    assert r["recurrence"] is None
    assert datetime.datetime.fromtimestamp(r["execute_at"]).hour == 17


def test_event_rule_email_from():
    r = parse("when an email from my boss arrives, tell me", NOW)
    assert r["kind"] == "event_rule" and r["topic_pattern"] == "email.arrived"
    assert json.loads(r["predicate"]) == [{"path": "from", "op": "contains", "value": "my boss"}]
    assert r["action_kind"] == "notify"


def test_list_and_cancel():
    assert parse("what are my reminders", NOW)["kind"] == "list"
    assert parse("cancel the morning brief reminder", NOW)["kind"] == "cancel"


def test_non_schedule_returns_none():
    assert parse("write a python function to train a model", NOW) is None
    assert parse("every transformer has attention layers", NOW) is None


def test_is_schedule_phrase_guard():
    assert is_schedule_phrase("every morning brief me")
    assert is_schedule_phrase("remind me at 8 to stand up")
    assert is_schedule_phrase("when an email from alice arrives tell me")
    assert is_schedule_phrase("what are my reminders")
    assert not is_schedule_phrase("write a python function")
    assert not is_schedule_phrase("every transformer has attention")
