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
  email.arrived      — external event published by the Gmail skill; payload:
                       {from, subject, snippet, thread_id} — drives event rules (N+2)
  plan.generated     — DevAgent approved a plan; payload: {goal, steps:[{n,action,args,deps}]}
  dag.step_started   — a plan step began executing; payload: {n, action}
  dag.step_completed — a plan step finished; payload: {n, action, success, latency_ms, result_snippet}
  chat.token         — a streamed LLM text chunk for the chat UI; payload: {text}

  The plan.generated / dag.* / chat.token topics carry a trace_id and drive the
  live DAG + token stream in the PC desktop chat UI (core/chat_server.py).

  Background-work topics (2026-06-19 observability batch) — surface autonomous
  actions that used to happen with no live signal (counter increments at most):
  goal.dequeued      — a queued goal was claimed for execution; payload: {goal_id, goal, source_trigger}
  goal.completed     — a queued goal finished; payload: {goal_id, status, success}
  vram.evicted       — ResourceGovernor evicted heavy models on a flare; payload: {free_gb, reason}
  vram.restored      — heavy models restored after a flare cleared; payload: {free_gb}
  breaker.opened     — an inference backend circuit-breaker opened; payload: {name, state, reason}
  inference.stalled  — an Ollama call hit DA_OLLAMA_TIMEOUT_S (hang guard); payload: {timeout_s, backend}

  Alerting topics — drive the dashboard Activity feed + Alerts panel:
  metric.threshold_crossed — MetricWatcher KPI breach/recovery; payload: {metric, value, message, severity}
  slo.breached       — ContinuousTrainer per-domain SLO breach; payload: {domain, metric, value, budget, verdict}
"""
from __future__ import annotations

import asyncio
import functools
import json
import logging
import re
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
TOPIC_EMAIL_ARRIVED     = "email.arrived"   # external (Gmail skill) → event rules (N+2)
# DAG/token topics for the PC desktop chat UI (core/chat_server.py). All carry trace_id.
TOPIC_PLAN_GENERATED    = "plan.generated"
TOPIC_DAG_STEP_STARTED  = "dag.step_started"
TOPIC_DAG_STEP_DONE     = "dag.step_completed"
TOPIC_CHAT_TOKEN        = "chat.token"
TOPIC_DAG_APPROVAL      = "dag.approval_requested"  # {message, destructive} — chat approval card
# Background-work observability topics (2026-06-19) — make autonomous actions visible.
TOPIC_GOAL_DEQUEUED     = "goal.dequeued"
TOPIC_GOAL_COMPLETED    = "goal.completed"
TOPIC_VRAM_EVICTED      = "vram.evicted"
TOPIC_VRAM_RESTORED     = "vram.restored"
TOPIC_BREAKER_OPENED    = "breaker.opened"
TOPIC_INFERENCE_STALLED = "inference.stalled"
# Alerting topics — surfaced on the dashboard Activity feed / Alerts panel.
TOPIC_METRIC_THRESHOLD  = "metric.threshold_crossed"  # MetricWatcher KPI alert
TOPIC_SLO_BREACHED      = "slo.breached"              # ContinuousTrainer SLO breach


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
        # Events dropped because a subscriber's queue was full (observability).
        self._dropped_events: int = 0

    @property
    def dropped_events(self) -> int:
        return self._dropped_events

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
                for q in queues:
                    try:
                        q.put_nowait(envelope)
                    except asyncio.QueueFull:
                        # Drop THIS EVENT for the slow consumer — never the
                        # subscription. The old code removed the queue, which
                        # turned a transient burst into a permanently hung
                        # consumer (it drained its buffer, then blocked on
                        # q.get() forever with no signal). The durable log
                        # still has the event; a consumer that must not miss
                        # any can replay via AgentDB.poll_events + its cursor.
                        self._dropped_events += 1
                        log.warning(
                            "EventBus: dropped event %s for slow consumer "
                            "(pattern=%s, total dropped=%d)",
                            topic, pattern, self._dropped_events,
                        )
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
        finally:
            # NOTE: CancelledError is deliberately NOT swallowed here —
            # catching it converted task.cancel() into a normal `async for`
            # exit, letting code after the loop run in a cancelled task and
            # breaking structured shutdown of subscribers.
            try:
                self._queues[topic_pattern].remove(q)
            except (KeyError, ValueError):
                pass


@functools.lru_cache(maxsize=128)
def _pattern_regex(pattern: str) -> "re.Pattern[str]":
    """Compile a SQL-LIKE pattern ('%' = zero or more chars) to a regex."""
    return re.compile(
        "^" + ".*".join(re.escape(part) for part in pattern.split("%")) + "$"
    )


def _topic_matches(topic: str, pattern: str) -> bool:
    """Match a topic against a SQL-LIKE pattern where % matches zero or more chars.

    Semantics are identical to the SQL LIKE used by AgentDB.poll_events, so
    real-time subscribers and replay consumers of the same pattern see the
    same event set ('%' anywhere in the pattern works, not just at the tail;
    SQL's '_' single-char wildcard is NOT supported — treated literally).
    Exact patterns (no %) must equal the topic.
    """
    if "%" not in pattern:
        return topic == pattern
    return _pattern_regex(pattern).match(topic) is not None
