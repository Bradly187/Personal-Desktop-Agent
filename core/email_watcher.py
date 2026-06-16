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
import os
import time
from pathlib import Path
from typing import Optional

from core.events import TOPIC_EMAIL_ARRIVED

log = logging.getLogger(__name__)


class EmailWatcher:
    POLL_INTERVAL_S = 120.0
    _SKILL_ID = "google_pim"
    _TOOL = "unread_messages"
    _MAX_SEEN = 1000

    def __init__(self, skill_registry, event_bus, notifier=None, *,
                 state_path=None) -> None:
        self._skills = skill_registry
        self._bus = event_bus
        self._notifier = notifier
        self._seen: set = set()
        self._baselined = False
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._last_beat: float = 0.0   # monotonic ts of last poll iteration (#2)
        # One-time expiry alert: set after notifying, cleared on the next
        # successful poll so a future expiry alerts again.
        self._auth_alerted = False
        # One-time "skill went away" alert (E14): a watcher that WAS working
        # (baselined) and then loses the google_pim skill goes blind silently.
        # Notify once; re-armed when the skill returns.
        self._skill_alerted = False
        # Restart-durable dedup (#7): when a state_path is given, the seen-id set
        # and baseline flag survive a restart, so mail that arrived while the
        # agent was down still fires on the next poll instead of being absorbed
        # into a fresh baseline. None (the default, and all unit tests) keeps the
        # old in-memory-only behavior.
        self._state_path = Path(state_path) if state_path else None
        self._state_loaded = False

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
        self._last_beat = time.monotonic()   # seed heartbeat baseline (#2)
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
                self._last_beat = time.monotonic()   # heartbeat (#2)
                await asyncio.sleep(self.POLL_INTERVAL_S)
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.warning("EmailWatcher._poll_loop error: %s", exc)

    # ── Durable dedup state (#7) ─────────────────────────────────────────────
    def _load_state_sync(self) -> Optional[dict]:
        if not self._state_path or not self._state_path.exists():
            return None
        try:
            return json.loads(self._state_path.read_text("utf-8"))
        except Exception as exc:
            log.debug("EmailWatcher state load failed: %s", exc)
            return None

    def _save_state_sync(self) -> None:
        if not self._state_path:
            return
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
            tmp.write_text(
                json.dumps({"seen": list(self._seen), "baselined": self._baselined}),
                "utf-8")
            os.replace(tmp, self._state_path)   # atomic on same volume
        except Exception as exc:
            log.debug("EmailWatcher state save failed: %s", exc)

    async def _tick(self) -> int:
        """Poll unread mail; publish email.arrived for new messages. Returns the
        number published (0 on the baseline poll). Public for tests."""
        if not self._state_loaded:
            self._state_loaded = True
            data = await asyncio.to_thread(self._load_state_sync)
            if data:
                self._seen = set(data.get("seen", []))
                self._baselined = bool(data.get("baselined", False))
        if not self._available():
            # E14: if the watcher had been working and the skill has since gone
            # away (disabled/crashed), the user is now blind to new mail. Say so
            # once. Stay quiet when the skill was simply never set up (not
            # baselined) — that's an expected idle state, not a regression.
            if self._baselined and not self._skill_alerted and self._notifier is not None:
                self._skill_alerted = True
                try:
                    await self._notifier.notify(
                        "Email alerts paused",
                        "The Gmail connection went offline. Say 'reconnect Google' "
                        "to restore email alerts.")
                except Exception as exc:
                    log.debug("EmailWatcher skill-offline notify failed: %s", exc)
            return 0   # skill not (yet) running — re-checked next tick
        self._skill_alerted = False   # skill present again — re-arm the alert
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
        changed = False
        for m in messages:
            mid = str(m.get("id", ""))
            if not mid or mid in self._seen:
                continue
            self._seen.add(mid)
            changed = True
            if self._baselined:
                await self._bus.publish(
                    TOPIC_EMAIL_ARRIVED,
                    {"from": m.get("from", ""), "subject": m.get("subject", ""),
                     "snippet": m.get("snippet", ""), "thread_id": m.get("thread_id", ""),
                     "id": mid},
                    source="gmail_skill",
                )
                published += 1
        if not self._baselined:
            self._baselined = True
            changed = True       # persist the initial baseline so a restart resumes
        if len(self._seen) > self._MAX_SEEN:
            self._seen = set(list(self._seen)[-self._MAX_SEEN:])
            changed = True
        if changed:
            await asyncio.to_thread(self._save_state_sync)
        return published

    # ── Supervision ────────────────────────────────────────────────────────
    def is_healthy(self) -> bool:
        return self._running and self._task is not None and not self._task.done()

    def last_heartbeat(self) -> float:
        """Monotonic ts of the last poll iteration (#2) — Supervisor staleness check.
        A wedged skill stdio call (no internal timeout) is the likely stall here."""
        return self._last_beat

    async def restart(self) -> None:
        """Relaunch a dead OR wedged poll loop (Supervisor-driven). A stale loop is
        still alive, so cancel it before relaunching to avoid a duplicate."""
        if not self._running:
            return
        from core.async_utils import cancel_if_alive
        await cancel_if_alive(self._task, "EmailWatcher poll loop", log)
        self._last_beat = time.monotonic()
        self._task = asyncio.create_task(self._poll_loop(), name="email_watcher")
        log.warning("EmailWatcher: poll loop relaunched by supervisor")
