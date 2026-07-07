"""EH-3 — store-and-forward + backlog surfacing.

- E12 Notifier persists an iPad push when no client is connected and flushes it
      on (re)connect, instead of dropping it.
- E13 ProactiveScheduler fires a one-shot 'review queue' nudge the first tick it
      finds a non-empty dev-escalation backlog.
- E14 EmailWatcher emits a one-time 'alerts paused' notice when a previously
      working (baselined) watcher loses the google_pim skill; re-arms on return.

Run:
    python -m pytest tests/test_eh3_store_forward.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock


sys.path.insert(0, str(Path(__file__).parent.parent))

from core.notifier import Notifier


def _no_tts(monkeypatch):
    import tts.polly_stream as _ps
    monkeypatch.setattr(_ps, "get_client", lambda *a, **k: MagicMock(speak=AsyncMock()))


# ===========================================================================
# E12 — Notifier store-and-forward
# ===========================================================================

def _bridge(connected: bool):
    b = MagicMock()
    b.has_clients = MagicMock(return_value=connected)
    b.broadcast_json = AsyncMock()
    return b


async def test_offline_notification_is_queued_not_dropped(monkeypatch, tmp_path):
    _no_tts(monkeypatch)
    bridge = _bridge(connected=False)
    n = Notifier(bridge=bridge, queue_path=tmp_path / "q.json")

    await n.notify("Reminder", "Take a break")
    bridge.broadcast_json.assert_not_awaited()         # nobody to push to
    assert (tmp_path / "q.json").exists()              # persisted instead


async def test_connected_notification_pushes_and_does_not_queue(monkeypatch, tmp_path):
    _no_tts(monkeypatch)
    bridge = _bridge(connected=True)
    n = Notifier(bridge=bridge, queue_path=tmp_path / "q.json")

    await n.notify("Hi", "there")
    bridge.broadcast_json.assert_awaited_once()
    # Nothing left pending.
    assert n._load_sync() == []


async def test_flush_delivers_queued_on_reconnect(monkeypatch, tmp_path):
    _no_tts(monkeypatch)
    qp = tmp_path / "q.json"

    # Queue two while offline.
    offline = Notifier(bridge=_bridge(connected=False), queue_path=qp)
    await offline.notify("A", "first")
    await offline.notify("B", "second")

    # iPad reconnects → flush.
    bridge = _bridge(connected=True)
    online = Notifier(bridge=bridge, queue_path=qp)
    delivered = await online.flush_pending()

    assert delivered == 2
    assert bridge.broadcast_json.await_count == 2
    titles = [c.args[0]["title"] for c in bridge.broadcast_json.await_args_list]
    assert titles == ["A", "B"]                        # FIFO order preserved
    assert online._load_sync() == []                   # queue cleared


async def test_flush_noop_when_still_offline(monkeypatch, tmp_path):
    _no_tts(monkeypatch)
    qp = tmp_path / "q.json"
    offline = Notifier(bridge=_bridge(connected=False), queue_path=qp)
    await offline.notify("A", "first")

    bridge = _bridge(connected=False)
    n = Notifier(bridge=bridge, queue_path=qp)
    assert await n.flush_pending() == 0
    bridge.broadcast_json.assert_not_awaited()
    assert len(n._load_sync()) == 1                    # still queued


async def test_queue_is_bounded(monkeypatch, tmp_path):
    _no_tts(monkeypatch)
    n = Notifier(bridge=_bridge(connected=False), queue_path=tmp_path / "q.json",
                 max_pending=3)
    for i in range(6):
        await n.notify(f"n{i}", "")
    pending = n._load_sync()
    assert len(pending) == 3
    assert [p["title"] for p in pending] == ["n3", "n4", "n5"]   # oldest dropped


# ===========================================================================
# E13 — ProactiveScheduler escalation nudge
# ===========================================================================

def _proactive(count, notifier):
    from core.proactive_scheduler import ProactiveScheduler
    db = MagicMock()
    db.sagas.count_pending_escalations = AsyncMock(return_value=count)
    db.goals.promote_due_goals = AsyncMock(return_value=[])
    db.misc.reap_expired_leases = AsyncMock(return_value=0)
    return ProactiveScheduler(db, notifier=notifier)


async def test_escalation_nudge_fires_once_when_backlog_present():
    notifier = MagicMock(); notifier.notify = AsyncMock()
    ps = _proactive(count=2, notifier=notifier)

    await ps._tick(0.0)
    await ps._tick(0.0)   # second tick must NOT re-nudge

    notifier.notify.assert_awaited_once()
    title = notifier.notify.await_args.args[0]
    assert "review" in title.lower()


async def test_no_nudge_when_backlog_empty():
    notifier = MagicMock(); notifier.notify = AsyncMock()
    ps = _proactive(count=0, notifier=notifier)
    await ps._tick(0.0)
    notifier.notify.assert_not_awaited()
    assert ps._nudged_escalations is False   # not latched — can still nudge later


# ===========================================================================
# E14 — EmailWatcher skill-offline notice
# ===========================================================================

def _watcher(available: bool, baselined: bool, notifier):
    from core.email_watcher import EmailWatcher
    skills = MagicMock()
    skills.has_skills = MagicMock(return_value=available)
    skills._skills = {"google_pim": object()} if available else {}
    w = EmailWatcher(skills, MagicMock(), notifier=notifier)
    w._baselined = baselined
    w._state_loaded = True   # skip state-file load
    return w


async def test_skill_offline_notifies_once_when_was_working():
    notifier = MagicMock(); notifier.notify = AsyncMock()
    w = _watcher(available=False, baselined=True, notifier=notifier)

    assert await w._tick() == 0
    assert await w._tick() == 0   # second tick must NOT re-notify
    notifier.notify.assert_awaited_once()
    assert "paused" in notifier.notify.await_args.args[0].lower()


async def test_skill_never_setup_stays_quiet():
    notifier = MagicMock(); notifier.notify = AsyncMock()
    w = _watcher(available=False, baselined=False, notifier=notifier)
    await w._tick()
    notifier.notify.assert_not_awaited()   # never set up → not a regression
