"""core/proactive_scheduler.py — time-triggered proactive automation (N+2).

A supervised loop (modeled on ResourceGovernor) that promotes due *scheduled*
goals in goal_queue to 'queued' and kicks the existing DevAgent drainer. A
trigger's only job is to make a goal claimable; the drainer then runs it with its
full approval / saga / flare machinery. Recurring goals re-lay their next
occurrence so "every morning" survives without a long-lived row.

Flare-aware: while the AccessibilityScheduler's dev admission is paused (a flare),
promotion is skipped — scheduled goals wait (execute_at is already past) and
promote on the first tick after the flare clears. A flare delays proactive work,
never drops it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from datetime import datetime, timedelta
from typing import Optional

log = logging.getLogger(__name__)


def next_occurrence(recurrence: dict, now: float,
                    prev_execute_at: Optional[float] = None) -> Optional[float]:
    """Next execute_at (epoch) strictly after `now` for a recurrence rule.

    Supported (MVP):
      {"kind": "interval", "every_s": <seconds>}
      {"kind": "daily", "at": "HH:MM"}   (local time)
    Returns None for an unknown / one-shot rule.

    When `prev_execute_at` (the occurrence that just ran) is given, interval
    recurrence advances from that intended time, not from `now`, so the phase
    stays aligned to the original schedule and does not drift forward after a
    delayed promotion or downtime (#3). Missed slots are skipped in one jump
    (no catch-up storm) — the next slot is the first strictly after `now`.
    """
    kind = (recurrence or {}).get("kind")
    if kind == "interval":
        every = float(recurrence.get("every_s", 0) or 0)
        if every <= 0:
            return None
        if prev_execute_at is None:
            return now + every
        nxt = prev_execute_at + every
        if nxt <= now:
            missed = math.floor((now - prev_execute_at) / every)
            nxt = prev_execute_at + (missed + 1) * every
        return nxt
    if kind == "daily":
        at = str(recurrence.get("at", "08:00"))
        try:
            hh, mm = (int(x) for x in at.split(":", 1))
        except (ValueError, TypeError):
            hh, mm = 8, 0
        cand = datetime.fromtimestamp(now).replace(
            hour=hh, minute=mm, second=0, microsecond=0)
        if cand.timestamp() <= now:
            cand = cand + timedelta(days=1)
        return cand.timestamp()
    return None


class ProactiveScheduler:
    POLL_INTERVAL_S = 30.0

    def __init__(self, db, *, dev_agent=None, scheduler=None, notifier=None) -> None:
        self._db = db
        self._dev_agent = dev_agent
        self._scheduler = scheduler    # AccessibilityScheduler — flare gate
        self._notifier = notifier
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._bg: set = set()          # keep fire-and-forget drain tasks alive

    def set_dev_agent(self, dev_agent) -> None:
        self._dev_agent = dev_agent

    def set_scheduler(self, scheduler) -> None:
        self._scheduler = scheduler

    # ── Lifecycle ──────────────────────────────────────────────────────────
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._poll_loop(), name="proactive_scheduler")
        log.info("ProactiveScheduler started (poll=%.0fs)", self.POLL_INTERVAL_S)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("ProactiveScheduler stopped")

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self.POLL_INTERVAL_S)
                await self._tick(time.time())
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.warning("ProactiveScheduler._poll_loop error: %s", exc)

    async def _tick(self, now: float) -> int:
        """Promote due scheduled goals; re-lay recurrences; kick the drainer.
        Returns the number promoted. Public for tests (the loop calls it)."""
        # Flare gate: while dev admission is paused, defer (re-ticks in 30 s).
        if self._scheduler is not None and getattr(self._scheduler, "dev_paused", False):
            return 0
        promoted = await self._db.promote_due_goals(now)
        if not promoted:
            return 0
        for row in promoted:
            rec = row.get("recurrence")
            if not rec:
                continue
            try:
                nxt = next_occurrence(
                    json.loads(rec), now, prev_execute_at=row.get("execute_at"))
                if nxt is not None:
                    trig = row.get("source_trigger") or "schedule"
                    # Deterministic key so a duplicated promotion of the same
                    # occurrence collapses to one 'scheduled' row instead of
                    # accumulating duplicates (#2 — recurrence storm guard).
                    key = f"{trig}:{row['goal']}:{int(nxt)}"
                    await self._db.enqueue_scheduled_goal(
                        row["goal"], execute_at=nxt, recurrence=rec,
                        domain=row.get("domain", "plan"),
                        source_trigger=trig,
                        idempotency_key=key,
                    )
            except Exception as exc:
                log.debug("ProactiveScheduler recurrence re-lay failed: %s", exc)
        if self._dev_agent is not None:
            t = asyncio.create_task(self._dev_agent.drain_goal_queue())
            self._bg.add(t)
            t.add_done_callback(self._bg.discard)
        log.info("ProactiveScheduler: promoted %d due goal(s)", len(promoted))
        return len(promoted)

    # ── Supervision ────────────────────────────────────────────────────────
    def is_healthy(self) -> bool:
        return self._running and self._task is not None and not self._task.done()

    async def restart(self) -> None:
        if not self._running:
            return
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._poll_loop(), name="proactive_scheduler")
        log.warning("ProactiveScheduler: poll loop relaunched by supervisor")
