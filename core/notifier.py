"""core/notifier.py — Notifier: the proactive notification sink (N+2).

Unifies the two delivery channels — Danielle TTS + iPad push — behind one call so
the ProactiveScheduler, EventRuleEngine, and DevAgent plan steps don't each
re-implement them. TTS is best-effort (it plays on the PC, independent of the
iPad). The iPad push is store-and-forward (EH-3 / E12): when no iPad is connected
the notification is persisted to a durable sidecar and flushed when one
(re)connects, so a proactive alert raised while the tablet is asleep/offline is
not silently lost.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_DEFAULT_QUEUE = Path.home() / ".claude" / "pending_notifications.json"


class Notifier:
    def __init__(self, bridge=None, *, queue_path=None, max_pending: int = 50) -> None:
        self._bridge = bridge  # IPadBridge | None
        self._queue_path: Optional[Path] = (
            Path(queue_path) if queue_path is not None else _DEFAULT_QUEUE
        )
        self._max_pending = max(1, max_pending)
        # Serialize sidecar mutations — notify() and flush_pending() both touch it
        # and can interleave on the event loop.
        self._lock = asyncio.Lock()

    def set_bridge(self, bridge) -> None:
        self._bridge = bridge

    # ── Delivery ────────────────────────────────────────────────────────────
    async def notify(self, title: str, body: str = "", *,
                     speak: bool = True, push: bool = True) -> None:
        """Deliver a proactive notification. TTS is fire-and-forget; the iPad push
        is queued for later delivery when no client is currently connected."""
        text = f"{title}. {body}" if (title and body) else (title or body)
        if speak and text:
            try:
                from tts.polly_stream import get_client as _get_tts
                await _get_tts().speak(text)
            except Exception as exc:
                log.debug("Notifier TTS failed: %s", exc)

        if not push:
            return

        if await self._push_now(title, body):
            return
        # No connected iPad (or the push failed) — persist for later (E12).
        await self._enqueue(title, body)

    async def _push_now(self, title: str, body: str) -> bool:
        """Try to push to a connected iPad. Returns True only if delivered."""
        bridge = self._bridge
        if bridge is None:
            return False
        try:
            if not bridge.has_clients():
                return False
        except Exception:
            return False
        try:
            await bridge.broadcast_json(self._envelope(title, body))
            return True
        except Exception as exc:
            log.debug("Notifier iPad push failed: %s", exc)
            return False

    @staticmethod
    def _envelope(title: str, body: str, ts: Optional[float] = None) -> dict:
        return {
            "type": "proactive_notification",
            "title": title,
            "body": body,
            "ts": ts if ts is not None else time.time(),
        }

    # ── Store-and-forward (E12) ───────────────────────────────────────────────
    async def _enqueue(self, title: str, body: str) -> None:
        if self._queue_path is None:
            return
        async with self._lock:
            pending = await asyncio.to_thread(self._load_sync)
            pending.append(self._envelope(title, body))
            # Bound the backlog — drop the OLDEST so a long offline stretch can't
            # grow the file unboundedly.
            if len(pending) > self._max_pending:
                pending = pending[-self._max_pending:]
            await asyncio.to_thread(self._save_sync, pending)
        log.info("Notifier: iPad offline — queued notification %r (%d pending)",
                 title, len(pending))

    async def flush_pending(self) -> int:
        """Deliver any queued notifications to a now-connected iPad. Registered as
        the bridge's connect handler. Returns the number delivered."""
        if self._queue_path is None or self._bridge is None:
            return 0
        async with self._lock:
            pending = await asyncio.to_thread(self._load_sync)
            if not pending:
                return 0
            try:
                if not self._bridge.has_clients():
                    return 0
            except Exception:
                return 0
            delivered = 0
            for env in pending:
                try:
                    await self._bridge.broadcast_json(env)
                    delivered += 1
                except Exception as exc:
                    # A client dropped mid-flush — keep the rest for the next connect.
                    log.debug("Notifier flush interrupted after %d: %s", delivered, exc)
                    break
            remainder = pending[delivered:]
            await asyncio.to_thread(self._save_sync, remainder)
        if delivered:
            log.info("Notifier: flushed %d queued notification(s) to iPad", delivered)
        return delivered

    # ── Sidecar I/O (blocking — call via to_thread) ───────────────────────────
    def _load_sync(self) -> list:
        if not self._queue_path or not self._queue_path.exists():
            return []
        try:
            data = json.loads(self._queue_path.read_text("utf-8"))
            return data if isinstance(data, list) else []
        except Exception as exc:
            log.debug("Notifier queue load failed: %s", exc)
            return []

    def _save_sync(self, pending: list) -> None:
        if not self._queue_path:
            return
        try:
            self._queue_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._queue_path.with_suffix(self._queue_path.suffix + ".tmp")
            tmp.write_text(json.dumps(pending), "utf-8")
            os.replace(tmp, self._queue_path)   # atomic on same volume
        except Exception as exc:
            log.debug("Notifier queue save failed: %s", exc)
