"""PR-3 — binary WebSocket audio frames.

The iPad may stream microphone audio as raw binary frames (1-byte tag + LE
int16 PCM) instead of base64-in-JSON. These tests pin:

  - the bridge's binary-frame router (`IPadBridge._handle_binary`) — correct
    tag dispatch, graceful handling of empty / unknown-tag / odd-length frames,
    and the WhisperStream-unavailable no-op;
  - parity between the binary ingest path (`on_audio_chunk_pcm`) and the legacy
    base64 path (`on_audio_chunk`);
  - the welcome-frame `binary_audio` capability advertisement, gated by the
    `_BINARY_AUDIO_ENABLED` (DA_BINARY_AUDIO) switch.

Run:
    python -m pytest tests/test_bridge_binary.py -q
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import aiohttp
import numpy as np
import pytest

import core.ipad_bridge as ipad_bridge
from core.ipad_bridge import IPadBridge, _BIN_TAG_AUDIO_PCM16
from sensors.whisper_stream import WhisperStream

BRIDGE_PORT = 8773  # unique to this test module
_TOKEN = "binary-test-token"


class _FakeWhisper:
    """Records on_audio_chunk_pcm calls without running faster-whisper."""

    def __init__(self, available: bool = True):
        self.available = available
        self.calls: list[tuple[bytes, int]] = []

    def on_audio_chunk_pcm(self, raw: bytes, frames: int) -> None:
        self.calls.append((bytes(raw), frames))


def _pcm(*samples: int) -> bytes:
    return np.array(samples, dtype=np.int16).tobytes()


# ---------------------------------------------------------------------------
# _handle_binary — tag routing + robustness
# ---------------------------------------------------------------------------

def test_audio_tag_routes_to_whisper():
    bridge = IPadBridge(port=BRIDGE_PORT, token=_TOKEN)
    fake = _FakeWhisper()
    bridge._whisper = fake
    payload = _pcm(100, -200, 300)
    bridge._handle_binary(bytes([_BIN_TAG_AUDIO_PCM16]) + payload)
    assert fake.calls == [(payload, 3)]   # 3 int16 samples = 3 frames


def test_unknown_tag_dropped():
    bridge = IPadBridge(port=BRIDGE_PORT, token=_TOKEN)
    fake = _FakeWhisper()
    bridge._whisper = fake
    bridge._handle_binary(bytes([0x7F]) + _pcm(1, 2, 3))
    assert fake.calls == []               # unknown tag → no routing, no raise


def test_empty_frame_is_noop():
    bridge = IPadBridge(port=BRIDGE_PORT, token=_TOKEN)
    fake = _FakeWhisper()
    bridge._whisper = fake
    bridge._handle_binary(b"")            # must not raise
    bridge._handle_binary(bytes([_BIN_TAG_AUDIO_PCM16]))  # tag only, no payload
    assert fake.calls == [(b"", 0)]       # zero-length payload → 0 frames


def test_whisper_unavailable_is_noop():
    bridge = IPadBridge(port=BRIDGE_PORT, token=_TOKEN)
    bridge._whisper = _FakeWhisper(available=False)
    bridge._handle_binary(bytes([_BIN_TAG_AUDIO_PCM16]) + _pcm(1, 2))
    assert bridge._whisper.calls == []    # gated by whisper.available

    bridge._whisper = None
    bridge._handle_binary(bytes([_BIN_TAG_AUDIO_PCM16]) + _pcm(1, 2))  # no raise


def test_handle_binary_never_raises(monkeypatch):
    """A whisper that throws must be swallowed (parity with text handlers)."""
    bridge = IPadBridge(port=BRIDGE_PORT, token=_TOKEN)

    class _Boom:
        available = True
        def on_audio_chunk_pcm(self, raw, frames):
            raise ValueError("boom")

    bridge._whisper = _Boom()
    bridge._handle_binary(bytes([_BIN_TAG_AUDIO_PCM16]) + _pcm(1))  # must not raise


# ---------------------------------------------------------------------------
# WhisperStream ingest — binary vs base64 parity + robustness
# ---------------------------------------------------------------------------

def test_pcm_and_base64_paths_are_identical():
    import base64
    ws = WhisperStream()
    ws.available = True
    payload = _pcm(100, -200, 300, -32768, 32767)

    ws.on_audio_chunk_pcm(payload, len(payload) // 2)
    binary_chunk = ws._buffer_chunks[0].copy()

    ws._buffer_chunks = []
    ws.on_audio_chunk(base64.b64encode(payload).decode(), len(payload) // 2)
    base64_chunk = ws._buffer_chunks[0]

    assert np.array_equal(binary_chunk, base64_chunk)


def test_pcm_path_respects_mute_and_suppress():
    ws = WhisperStream()
    ws.available = True
    ws.set_muted(True)
    ws.on_audio_chunk_pcm(_pcm(1, 2, 3), 3)
    assert ws._buffer_chunks == []        # hard mute drops binary audio too


def test_pcm_path_handles_odd_length_without_raising():
    ws = WhisperStream()
    ws.available = True
    ws.on_audio_chunk_pcm(b"\x01\x02\x03", 1)   # not a multiple of int16 size
    assert ws._buffer_chunks == []        # decode error swallowed, nothing buffered


# ---------------------------------------------------------------------------
# Welcome-frame capability advertisement
# ---------------------------------------------------------------------------

@contextlib.asynccontextmanager
async def _running_bridge():
    bridge = IPadBridge(port=BRIDGE_PORT, token=_TOKEN)
    task = asyncio.create_task(bridge.run(no_mdns=True))
    await asyncio.sleep(0.8)  # let TCPSite bind
    try:
        yield bridge
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.1)


async def _first_status(session) -> dict:
    url = f"ws://localhost:{BRIDGE_PORT}/ws?token={_TOKEN}"
    async with session.ws_connect(url) as ws:
        msg = await asyncio.wait_for(ws.receive(), timeout=2.0)
        import json
        return json.loads(msg.data)


async def test_welcome_advertises_binary_audio_when_enabled(monkeypatch):
    monkeypatch.setattr(ipad_bridge, "_BINARY_AUDIO_ENABLED", True)
    async with _running_bridge():
        async with aiohttp.ClientSession() as session:
            welcome = await _first_status(session)
    assert welcome["type"] == "status"
    assert welcome["binary_audio"] is True


async def test_welcome_omits_capability_when_disabled(monkeypatch):
    """DA_BINARY_AUDIO=0 → advertised false → iPad keeps the base64 path."""
    monkeypatch.setattr(ipad_bridge, "_BINARY_AUDIO_ENABLED", False)
    async with _running_bridge():
        async with aiohttp.ClientSession() as session:
            welcome = await _first_status(session)
    assert welcome["binary_audio"] is False
