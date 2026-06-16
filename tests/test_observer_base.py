"""PR 1 (R-1) — ObserverAgent substrate: subscribe loop, exception survival,
liveness + restart, structured stop."""
import asyncio
import os
import tempfile

from storage.db import AgentDB
from core.events import EventBus
from agents.observer_base import ObserverAgent


class _RecordingObserver(ObserverAgent):
    def __init__(self, bus):
        super().__init__(bus, "rec")
        self.events = []
        self.raise_next = False

    def topics(self):
        return ["test.%"]

    async def on_event(self, evt):
        if self.raise_next:
            self.raise_next = False
            raise RuntimeError("boom")
        self.events.append(evt)


async def _bus():
    d = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    db = AgentDB()
    await db.open(os.path.join(d.name, "agent.db"))
    return EventBus(db), db, d


async def test_subscribe_and_receive():
    bus, db, d = await _bus()
    obs = _RecordingObserver(bus)
    try:
        await obs.start()
        await asyncio.sleep(0.03)
        assert obs.is_healthy()
        await bus.publish("test.ping", {"n": 1}, source="t")
        await asyncio.sleep(0.05)
        assert any(e["topic"] == "test.ping" for e in obs.events)
    finally:
        await obs.stop()
        await db.close()
        d.cleanup()


async def test_handler_exception_does_not_kill_loop():
    bus, db, d = await _bus()
    obs = _RecordingObserver(bus)
    try:
        await obs.start()
        await asyncio.sleep(0.03)
        obs.raise_next = True
        await bus.publish("test.boom", {}, source="t")   # handler raises, swallowed
        await asyncio.sleep(0.05)
        await bus.publish("test.ok", {"n": 2}, source="t")
        await asyncio.sleep(0.05)
        assert obs.is_healthy()
        assert any(e["topic"] == "test.ok" for e in obs.events)
    finally:
        await obs.stop()
        await db.close()
        d.cleanup()


async def test_dead_task_then_restart():
    bus, db, d = await _bus()
    obs = _RecordingObserver(bus)
    try:
        await obs.start()
        await asyncio.sleep(0.03)
        assert obs.is_healthy()
        # Simulate a dead subscription task.
        obs._tasks[0].cancel()
        await asyncio.sleep(0.03)
        assert not obs.is_healthy()
        await obs.restart()
        await asyncio.sleep(0.03)
        assert obs.is_healthy()
        await bus.publish("test.again", {"n": 3}, source="t")
        await asyncio.sleep(0.05)
        assert any(e["topic"] == "test.again" for e in obs.events)
    finally:
        await obs.stop()
        await db.close()
        d.cleanup()


async def test_stop_is_clean():
    bus, db, d = await _bus()
    obs = _RecordingObserver(bus)
    await obs.start()
    await asyncio.sleep(0.03)
    await obs.stop()
    assert not obs.is_healthy()
    assert obs._tasks == []
    await db.close()
    d.cleanup()


async def test_no_bus_is_inert():
    obs = _RecordingObserver(None)
    await obs.start()           # must not raise
    assert not obs.is_healthy()
    await obs.stop()
