"""N+2 follow-up — EmailWatcher: baseline-then-publish, dedup, skill-gating;
plus the Gmail server's structured `unread_messages`.

Run:
    python -m pytest tests/test_email_watcher.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.email_watcher import EmailWatcher
from skills.servers import google_pim_server as g


def test_unread_structured_shape():
    gmail = MagicMock()
    msgs = gmail.users.return_value.messages.return_value
    msgs.list.return_value.execute.return_value = {"messages": [{"id": "m1"}]}
    msgs.get.return_value.execute.return_value = {
        "snippet": "hi there", "threadId": "t1",
        "payload": {"headers": [
            {"name": "From", "value": "bob@x"}, {"name": "Subject", "value": "Hey"}]}}
    assert g._unread_structured(gmail, 5) == [
        {"id": "m1", "from": "bob@x", "subject": "Hey", "snippet": "hi there", "thread_id": "t1"}]


def _registry(payloads):
    reg = MagicMock()
    reg.has_skills = MagicMock(return_value=True)
    reg._skills = {"google_pim": object()}
    it = iter(payloads)

    async def _call(sid, tool, args):
        return {"status": "ok", "text": json.dumps(next(it))}

    reg.call = _call
    return reg


async def test_baseline_then_publish_new():
    bus = MagicMock(); bus.publish = AsyncMock()
    reg = _registry([
        [{"id": "1", "from": "a", "subject": "s1"}],
        [{"id": "1", "from": "a", "subject": "s1"}, {"id": "2", "from": "b", "subject": "s2"}],
    ])
    w = EmailWatcher(reg, bus)
    assert await w._tick() == 0          # baseline poll — never fires for existing unread
    bus.publish.assert_not_awaited()
    assert await w._tick() == 1          # the new message fires
    bus.publish.assert_awaited_once()
    assert bus.publish.await_args.args[0] == "email.arrived"
    payload = bus.publish.await_args.args[1]
    assert payload["from"] == "b" and payload["id"] == "2"


async def test_dedup_no_repeat():
    bus = MagicMock(); bus.publish = AsyncMock()
    reg = _registry([[{"id": "1"}], [{"id": "1"}, {"id": "2"}], [{"id": "1"}, {"id": "2"}]])
    w = EmailWatcher(reg, bus)
    await w._tick()                       # baseline
    await w._tick()                       # id 2 new
    assert await w._tick() == 0           # nothing new
    assert bus.publish.await_count == 1


async def test_runs_and_waits_when_skill_absent():
    # OAuth-lifecycle change: the loop ALWAYS runs (skill presence re-checked
    # per tick) so a google_pim hot-started by voice "connect Google" is picked
    # up within one poll — previously start() gave up and a restart was needed.
    reg = MagicMock(); reg.has_skills = MagicMock(return_value=False); reg._skills = {}
    reg.call = AsyncMock()
    bus = MagicMock(); bus.publish = AsyncMock()
    w = EmailWatcher(reg, bus)
    await w.start()
    assert w.is_healthy()                 # running, waiting for the skill
    assert await w._tick() == 0
    reg.call.assert_not_awaited()         # absent skill → no API call
    await w.stop()
