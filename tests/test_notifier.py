"""N+2 — Notifier: TTS + iPad push, both guarded and non-fatal.

Run:
    python -m pytest tests/test_notifier.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.notifier import Notifier


async def test_notify_pushes_to_bridge(monkeypatch):
    import tts.polly_stream as _ps
    monkeypatch.setattr(_ps, "get_client", lambda *a, **k: MagicMock(speak=AsyncMock()))
    bridge = MagicMock(); bridge.broadcast_json = AsyncMock()
    await Notifier(bridge=bridge).notify("Morning brief", "It is sunny.")
    bridge.broadcast_json.assert_awaited()
    payload = bridge.broadcast_json.await_args.args[0]
    assert payload["type"] == "proactive_notification"
    assert payload["title"] == "Morning brief" and payload["body"] == "It is sunny."


async def test_notify_no_bridge_still_speaks(monkeypatch):
    import tts.polly_stream as _ps
    spoke = AsyncMock()
    monkeypatch.setattr(_ps, "get_client", lambda *a, **k: MagicMock(speak=spoke))
    await Notifier(bridge=None).notify("hi", "there")   # must not raise
    spoke.assert_awaited()


async def test_tts_failure_is_nonfatal(monkeypatch):
    import tts.polly_stream as _ps

    def _boom(*a, **k):
        raise RuntimeError("no tts")

    monkeypatch.setattr(_ps, "get_client", _boom)
    bridge = MagicMock(); bridge.broadcast_json = AsyncMock()
    await Notifier(bridge=bridge).notify("x", "y")   # TTS raises → caught; push still happens
    bridge.broadcast_json.assert_awaited()
