"""core/notifier.py — Notifier: the proactive notification sink (N+2).

Unifies the two delivery channels — Danielle TTS + iPad push — behind one call so
the ProactiveScheduler, EventRuleEngine, and DevAgent plan steps don't each
re-implement them. Both channels are guarded and non-fatal: a notification is
best-effort (TTS down, or no iPad connected → no-op, not an error).
"""

from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, bridge=None) -> None:
        self._bridge = bridge  # IPadBridge | None

    def set_bridge(self, bridge) -> None:
        self._bridge = bridge

    async def notify(self, title: str, body: str = "", *,
                     speak: bool = True, push: bool = True) -> None:
        """Deliver a proactive notification on both channels (each independent)."""
        text = f"{title}. {body}" if (title and body) else (title or body)
        if speak and text:
            try:
                from tts.polly_stream import get_client as _get_tts
                await _get_tts().speak(text)
            except Exception as exc:
                log.debug("Notifier TTS failed: %s", exc)
        if push and self._bridge is not None:
            try:
                await self._bridge.broadcast_json({
                    "type": "proactive_notification",
                    "title": title,
                    "body": body,
                    "ts": time.time(),
                })
            except Exception as exc:
                log.debug("Notifier iPad push failed: %s", exc)
