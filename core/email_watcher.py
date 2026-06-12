"""core/email_watcher.py — publish email.arrived events from the Gmail skill.

The Google PIM MCP server runs in a subprocess with no access to the agent's
EventBus, so the watcher lives in the main process: it polls the skill's
`unread_messages` tool, dedups by message id, and publishes one `email.arrived`
event per NEW message onto the bus — which the EventRuleEngine turns into
notifications ("when an email from X arrives, tell me").

The first poll only establishes a baseline (it does NOT fire for the inbox's
existing unread mail). Active only when the `google_pim` skill is registered
(the manifest ships disabled until OAuth is set up), so it is otherwise a no-op.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from core.events import TOPIC_EMAIL_ARRIVED

log = logging.getLogger(__name__)


class EmailWatcher:
    POLL_INTERVAL_S = 120.0
    _SKILL_ID = "google_pim"
    _TOOL = "unread_messages"
    _MAX_SEEN = 1000

    def __init__(self, skill_registry, event_bus, notifier=None) -> None:
        self._skills = skill_registry
        self._bus = event_bus
        self._notifier = notifier
        self._seen: set = set()
        self._baselined = False
        self._task: Optional[asyncio.Task] = None
        self._running = False
        # One-time expiry alert: set after notifying, cleared on the next
        # successful poll so a future expiry alerts again.
        self._auth_alerted = False

    def _available(self) -> bool:
        if self._skills is None or self._bus is None:
            return False
        try:
            return (self._skills.has_skills()
                    and self._SKILL_ID in getattr(self._skills, "_skills", {}))
        except Exception:
            return False

    # ── Lifecycle ──────────────────────────────────────────────────────────
    async def start(self) -> None:
        """Always start the loop (skill presence is re-checked per tick) so a
        google_pim hot-started later — the voice "connect Google" flow — is
        picked up within one poll interval, no agent restart needed."""
        if self._running:
            return
        if self._skills is None or self._bus is None:
            log.info("EmailWatcher: registry/bus not wired — watcher idle")
            return
        self._running = True
        self._task = asyncio.create_task(self._poll_loop(), name="email_watcher")
        log.info("EmailWatcher started (poll=%.0fs%s)", self.POLL_INTERVAL_S,
                 "" if self._available() else "; waiting for google_pim")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self.POLL_INTERVAL_S)
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.warning("EmailWatcher._poll_loop error: %s", exc)

    async def _tick(self) -> int:
        """Poll unread mail; publish email.arrived for new messages. Returns the
        number published (0 on the baseline poll). Public for tests."""
        if not self._available():
            return 0   # skill not (yet) running — re-checked next tick
        res = await self._skills.call(self._SKILL_ID, self._TOOL, {})
        if not isinstance(res, dict) or res.get("status") != "ok":
            return 0
        text = res.get("text") or ""
        # Auth expiry: the server returns its RECONNECT message as a normal
        # string. Tell the user ONCE (spoken + iPad banner) instead of failing
        # silently every 2 minutes forever; re-arm after the next success.
        if "reconnect google" in text.lower():
            if not self._auth_alerted and self._notifier is not None:
                self._auth_alerted = True
                try:
                    await self._notifier.notify(
                        "Google access expired",
                        "Email alerts are paused. Say 'reconnect Google' to sign "
                        "in again.")
                except Exception as exc:
                    log.debug("EmailWatcher expiry notify failed: %s", exc)
            return 0
        try:
            messages = json.loads(text or "[]")
        except (ValueError, TypeError):
            return 0
        if not isinstance(messages, list):
            return 0
        self._auth_alerted = False   # healthy again — re-arm the alert

        published = 0
        for m in messages:
            mid = str(m.get("id", ""))
            if not mid or mid in self._seen:
                continue
            self._seen.add(mid)
            if self._baselined:
                await self._bus.publish(
                    TOPIC_EMAIL_ARRIVED,
                    {"from": m.get("from", ""), "subject": m.get("subject", ""),
                     "snippet": m.get("snippet", ""), "thread_id": m.get("thread_id", ""),
                     "id": mid},
                    source="gmail_skill",
                )
                published += 1
        self._baselined = True
        if len(self._seen) > self._MAX_SEEN:
            self._seen = set(list(self._seen)[-self._MAX_SEEN:])
        return published

    # ── Supervision ────────────────────────────────────────────────────────
    def is_healthy(self) -> bool:
        return self._running and self._task is not None and not self._task.done()

    async def restart(self) -> None:
        if not self._running:
            return
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._poll_loop(), name="email_watcher")
        log.warning("EmailWatcher: poll loop relaunched by supervisor")
