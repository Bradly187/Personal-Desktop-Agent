"""PR 0 (R-1 foundation) — the previously-silent events are now emitted.

- EventBus delivers voice.drift + step.failed to a subscriber (mechanics).
- DevAgent._persist_step publishes step.failed for a failed step (the single
  chokepoint for both sequential and DAG execution paths), independent of DB
  availability.
"""
import asyncio
import os
import tempfile

from storage.db import AgentDB
from core.events import EventBus, TOPIC_VOICE_DRIFT, TOPIC_STEP_FAILED
from inference.dev_agent import DevAgent, AgentStep


class _RecorderBus:
    """Minimal EventBus stand-in that records publish calls."""
    def __init__(self):
        self.published = []

    async def publish(self, topic, payload, source, **kw):
        self.published.append((topic, payload, source))
        return len(self.published)


async def test_eventbus_delivers_drift_and_step_failed():
    d = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    db = AgentDB()
    await db.open(os.path.join(d.name, "agent.db"))
    try:
        bus = EventBus(db)
        received = []

        async def consume():
            async for evt in bus.subscribe("test_consumer", "%"):
                received.append(evt)
                if len(received) >= 2:
                    break

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.02)  # let the subscription register
        await bus.publish(TOPIC_VOICE_DRIFT, {"drift_pct": 42.0, "reason": "voice_clarity"},
                          source="acoustic_profiler")
        await bus.publish(TOPIC_STEP_FAILED, {"run_id": 1, "step_num": 2, "action": "RUN_TERMINAL",
                                              "error": "boom"}, source="dev_agent")
        await asyncio.wait_for(task, timeout=2.0)

        topics = {e["topic"] for e in received}
        assert TOPIC_VOICE_DRIFT in topics
        assert TOPIC_STEP_FAILED in topics
        # durable log has them too
        async with db._conn.execute("SELECT topic FROM event_log") as cur:
            logged = {r[0] for r in await cur.fetchall()}
        assert {TOPIC_VOICE_DRIFT, TOPIC_STEP_FAILED} <= logged
    finally:
        await db.close()
        d.cleanup()


async def test_persist_step_publishes_step_failed_on_failure():
    agent = DevAgent(router=None)          # router unused by _persist_step
    bus = _RecorderBus()
    agent.set_event_bus(bus)

    failed = AgentStep(action="RUN_TERMINAL", args="pytest")
    failed.success = False
    failed.result = "ERROR: tests failed"
    await agent._persist_step(run_id=7, step_num=3, step=failed)

    assert len(bus.published) == 1
    topic, payload, source = bus.published[0]
    assert topic == TOPIC_STEP_FAILED
    assert payload["run_id"] == 7 and payload["step_num"] == 3
    assert payload["action"] == "RUN_TERMINAL"
    assert "tests failed" in payload["error"]
    assert source == "dev_agent"


async def test_persist_step_silent_on_success():
    agent = DevAgent(router=None)
    bus = _RecorderBus()
    agent.set_event_bus(bus)

    ok = AgentStep(action="READ_SCREEN")
    ok.success = True
    await agent._persist_step(run_id=7, step_num=1, step=ok)

    assert bus.published == []
