"""core/event_rule_engine.py — event-triggered proactive automation (N+2).

A consumer of the EventBus: when an event's topic matches an enabled rule and its
predicate holds (and cooldown allows), the engine fires — either NOTIFY the user
(cheap, safe) or ENQUEUE a goal the DevAgent drainer runs. External events (e.g.
the Gmail skill's `email.arrived`) are published onto the same bus, so a rule can
fire on them; the engine is agnostic about the publisher and is fully testable
with synthetic events.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

from core.events import _topic_matches

log = logging.getLogger(__name__)


# Distinct from None so a field that is absent is not confused with a field
# explicitly set to JSON null (#10): `exists` must fail on absent but pass on
# present-null, and `eq null` must only match a field that is actually present.
_MISSING = object()


def _get_path(payload: dict, path: str):
    cur = payload
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return _MISSING
    return cur


def _eval_predicate(predicate, payload: dict) -> bool:
    """AND-ed list of {path, op, value}. ops: eq | contains | exists.
    None / empty predicate matches any event."""
    if not predicate:
        return True
    try:
        clauses = json.loads(predicate) if isinstance(predicate, str) else predicate
    except (ValueError, TypeError):
        return False
    if not isinstance(clauses, list):
        return False
    for c in clauses:
        val = _get_path(payload, str(c.get("path", "")))
        op = c.get("op", "eq")
        want = c.get("value")
        if op == "exists":
            if val is _MISSING:
                return False
        elif op == "eq":
            if val is _MISSING or val != want:
                return False
        elif op == "contains":
            if val is _MISSING or val is None or str(want).lower() not in str(val).lower():
                return False
        else:
            return False
    return True


def _render(template: str, payload: dict) -> str:
    out = template or ""
    for k, v in payload.items():
        out = out.replace("{" + str(k) + "}", str(v))
    return out


class EventRuleEngine:
    def __init__(self, db, event_bus, *, notifier=None, dev_agent=None) -> None:
        self._db = db
        self._bus = event_bus
        self._notifier = notifier
        self._dev_agent = dev_agent
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._bg: set = set()
        # In-memory last-fired time per rule, updated BEFORE awaiting _fire so a
        # same-tick burst of matching events is suppressed by cooldown even
        # though last_fired_at hasn't been persisted yet (#16).
        self._mem_fired: dict = {}

    def set_dev_agent(self, dev_agent) -> None:
        self._dev_agent = dev_agent

    # ── Lifecycle ──────────────────────────────────────────────────────────
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._consume_loop(), name="event_rule_engine")
        log.info("EventRuleEngine started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("EventRuleEngine stopped")

    async def _consume_loop(self) -> None:
        # One broad subscription; rules are re-read per event so changes take
        # effect without a restart. The durable event_log backs replay if needed.
        try:
            async for event in self._bus.subscribe("event_rule_engine", "%"):
                try:
                    await self.on_event(event)
                except Exception as exc:
                    log.warning("EventRuleEngine.on_event error: %s", exc)
        except asyncio.CancelledError:
            return

    async def on_event(self, event: dict) -> int:
        """Evaluate one event against the rule set; fire matches. Returns the
        number fired. Public for tests (synthetic events)."""
        topic = event.get("topic", "")
        payload = event.get("payload", {}) or {}
        now = time.time()
        fired = 0
        for rule in await self._db.list_event_rules(enabled_only=True):
            if not _topic_matches(topic, rule["topic_pattern"]):
                continue
            if not _eval_predicate(rule.get("predicate"), payload):
                continue
            cooldown = rule.get("cooldown_s", 0) or 0
            if cooldown:
                # Guard against both a persisted recent fire and a same-tick burst
                # whose last_fired_at hasn't landed yet.
                last = max(rule.get("last_fired_at") or 0,
                           self._mem_fired.get(rule["id"], 0))
                if (now - last) < cooldown:
                    continue
                self._mem_fired[rule["id"]] = now   # claim the slot before awaiting
            await self._fire(rule, payload, now)
            fired += 1
        return fired

    async def _fire(self, rule: dict, payload: dict, now: float) -> None:
        rendered = _render(rule.get("goal_template", ""), payload)
        if rule.get("action_kind") == "enqueue_goal" and self._dev_agent is not None:
            key = f"event_rule:{rule['id']}:{payload.get('id', now)}"
            await self._db.enqueue_goal(rendered, domain="plan", idempotency_key=key)
            t = asyncio.create_task(self._dev_agent.drain_goal_queue())
            self._bg.add(t)
            t.add_done_callback(self._bg.discard)
        elif self._notifier is not None:
            await self._notifier.notify(rule.get("name") or "Notification", rendered)
        await self._db.touch_rule_fired(rule["id"], now)

    # ── Supervision ────────────────────────────────────────────────────────
    def is_healthy(self) -> bool:
        return self._running and self._task is not None and not self._task.done()

    async def restart(self) -> None:
        if not self._running:
            return
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._consume_loop(), name="event_rule_engine")
        log.warning("EventRuleEngine: consume loop relaunched by supervisor")
