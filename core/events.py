"""core/events.py — EventBus: SQLite-backed append-only event log + asyncio.Queue fan-out.

Architecture:
- `EventBus.publish()` writes to AgentDB.event_log (durable, replayable) AND
  notifies all in-process subscribers via asyncio.Queue (zero-latency real-time delivery).
- Consumers that need replay call `AgentDB.poll_events()` directly with their cursor.
- Consumers that need real-time delivery call `EventBus.subscribe()` to get an async iterator.

Topic namespace (dotted paths):
  command.executed   — every command outcome; payload: {action, route, gate, latency_ms, success}
  gate.decided       — gate label + latency per command; payload: {gate, latency_ms, domain}
  voice.drift        — acoustic drift detected; payload: {drift_pct, sessions_since_cal}
  step.failed        — DevAgent step failure before replan; payload: {run_id, step_num, action, error}
  replan.exhausted   — MAX_REPLANS hit; payload: {run_id, goal, replans}
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING, AsyncIterator, Optional

if TYPE_CHECKING:
    from storage.db import AgentDB

log = logging.getLogger(__name__)

# Topic constants — use these instead of bare strings to catch typos at import time.
TOPIC_COMMAND_EXECUTED  = "command.executed"
TOPIC_GATE_DECIDED      = "gate.decided"
TOPIC_VOICE_DRIFT       = "voice.drift"
TOPIC_STEP_FAILED       = "step.failed"
TOPIC_REPLAN_EXHAUSTED  = "replan.exhausted"


class EventBus:
    """Dual-delivery event bus: durable SQLite log + in-process asyncio.Queue fan-out.

    Usage::

        bus = EventBus(agent_db)
        await bus.publish(TOPIC_COMMAND_EXECUTED, {"action": "CLICK", "success": True},
                          source="coordinator", command_id=42)

        async for event in bus.subscribe("gate_watcher", "gate.%"):
            print(event)          # dict with id, topic, payload (parsed), etc.
    """

    def __init__(self, db: "AgentDB") -> None:
        self._db = db
        # topic_pattern → list of Queue instances for that subscription
        self._queues: dict[str, list[asyncio.Queue]] = {}

    # ---------------------------------------------------------------------- #
    # Publish
    # ---------------------------------------------------------------------- #

    async def publish(
        self,
        topic: str,
        payload: dict,
        source: str,
        *,
        session_id: Optional[int] = None,
        command_id: Optional[int] = None,
        trace_id: Optional[str] = None,
    ) -> Optional[int]:
        """Write one event to the durable log and fan-out to in-process subscribers.

        Returns the new event_log row id, or None if the DB write failed (in-process
        fan-out still happens regardless).
        """
        payload_str = json.dumps(payload, separators=(",", ":"))
        # Durable write first — if it fails, log it but still deliver in-process.
        row_id: Optional[int] = None
        try:
            row_id = await self._db.insert_event(
                topic, payload_str, source,
                session_id=session_id,
                command_id=command_id,
                trace_id=trace_id,
            )
        except Exception as exc:
            log.warning("EventBus: DB write failed (in-process fan-out continues): %s", exc)
        # In-process fan-out to all subscribers whose pattern matches this topic.
        envelope = {
            "id": row_id,
            "ts": time.time(),
            "topic": topic,
            "source": source,
            "session_id": session_id,
            "command_id": command_id,
            "trace_id": trace_id,
            "payload": payload,
        }
        for pattern, queues in list(self._queues.items()):
            if _topic_matches(topic, pattern):
                dead: list[asyncio.Queue] = []
                for q in queues:
                    try:
                        q.put_nowait(envelope)
                    except asyncio.QueueFull:
                        dead.append(q)
                        log.debug("EventBus: dropped event for slow consumer (pattern=%s)", pattern)
                for q in dead:
                    queues.remove(q)
        return row_id

    # ---------------------------------------------------------------------- #
    # Subscribe (async iterator)
    # ---------------------------------------------------------------------- #

    async def subscribe(
        self,
        consumer_name: str,
        topic_pattern: str,
        maxsize: int = 256,
    ) -> AsyncIterator[dict]:
        """Async generator yielding events matching topic_pattern in real-time.

        Registers consumer_name in AgentDB for cursor tracking. The generator
        runs until the caller breaks or cancels.
        """
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        # Register the queue BEFORE the first await so publish() can fan-out
        # even if the DB call suspends and publish() runs concurrently.
        self._queues.setdefault(topic_pattern, []).append(q)
        try:
            await self._db.upsert_event_consumer(consumer_name, topic_pattern)
            while True:
                event = await q.get()
                yield event
                q.task_done()
        except asyncio.CancelledError:
            pass
        finally:
            try:
                self._queues[topic_pattern].remove(q)
            except (KeyError, ValueError):
                pass


def _topic_matches(topic: str, pattern: str) -> bool:
    """Match a topic against a LIKE-style pattern where % is a wildcard segment.

    'command.%' matches 'command.executed' and 'command.foo.bar' but NOT 'command.'.
    '%' alone matches anything. Exact patterns (no %) must equal the topic.
    """
    if "%" not in pattern:
        return topic == pattern
    prefix = pattern.rstrip("%")  # e.g. "command." or "" for bare "%"
    return topic.startswith(prefix)
