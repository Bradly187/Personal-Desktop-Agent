"""Polly Bidirectional Streaming TTS — Python client.

Wraps the Node.js TTS sidecar (tts_service/server.js) which calls
StartSpeechSynthesisStream via the AWS SDK for JavaScript v3.

Public API
----------
    client = PollyStreamClient()

    # Speak a complete string (CLARIFY messages, short confirmations)
    client.speak_sync("Did you mean close the window?")

    # Stream LLM tokens sentence-by-sentence (EXPLAIN responses)
    await client.speak_stream(token_async_generator)

    # Cancel current playback
    client.cancel()

How it works
------------
    1. Python sends POST /speak { text } to localhost:8766 (Node.js sidecar).
    2. Node.js opens StartSpeechSynthesisStream (Generative engine, OGG Vorbis).
    3. Node.js returns the complete OGG audio bytes as the HTTP response body.
    4. Python decodes the OGG with soundfile and plays via sounddevice.
    5. For streaming EXPLAIN text, sentences are batched and POSTed one at a time
       so playback starts as soon as the first sentence is synthesised.

Auto-start
----------
    The sidecar is launched automatically on first use if port 8766 is not
    responding. npm install is run if node_modules is missing.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import socket
import subprocess
import time
import threading
import urllib.request
from pathlib import Path
from typing import AsyncIterator, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_TTS_PORT = int(os.environ.get("TTS_PORT", "8766"))
_TTS_HOST = "127.0.0.1"
_TTS_BASE = f"http://{_TTS_HOST}:{_TTS_PORT}"
_SIDECAR_DIR = Path(__file__).parent / "tts_service"

# Regex that matches the end of a speakable sentence (., !, ?, — with trailing space or end)
_SENTENCE_END = re.compile(r'(?<=[.!?])\s+|(?<=[.!?])$')

# ---------------------------------------------------------------------------
# Sidecar lifecycle
# ---------------------------------------------------------------------------

_sidecar_proc: Optional[subprocess.Popen] = None
_sidecar_lock = threading.Lock()


def _is_running() -> bool:
    """Return True if the TTS sidecar is accepting connections on its port."""
    try:
        with socket.create_connection((_TTS_HOST, _TTS_PORT), timeout=0.5):
            return True
    except OSError:
        return False


def _ensure_started() -> bool:
    """Start the Node.js TTS sidecar if it isn't already running.

    Returns True if the sidecar is up, False if startup failed.
    """
    global _sidecar_proc

    if _is_running():
        return True

    with _sidecar_lock:
        if _is_running():
            return True  # another thread started it

        # Install node_modules if this is the first run
        node_modules = _SIDECAR_DIR / "node_modules"
        if not node_modules.exists():
            log.info("TTS: running npm install in %s ...", _SIDECAR_DIR)
            try:
                subprocess.run(
                    ["npm", "install"],
                    cwd=_SIDECAR_DIR,
                    check=True,
                    capture_output=True,
                    timeout=120,
                )
            except Exception as exc:
                log.error("TTS: npm install failed — %s", exc)
                return False

        log.info("TTS: starting Node.js sidecar on port %d ...", _TTS_PORT)
        try:
            _sidecar_proc = subprocess.Popen(
                ["node", "server.js"],
                cwd=_SIDECAR_DIR,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                env={**os.environ, "TTS_PORT": str(_TTS_PORT)},
            )
        except FileNotFoundError:
            log.error("TTS: 'node' not found — install Node.js v18+")
            return False

        # Wait up to 6 seconds for the sidecar to bind
        for _ in range(30):
            if _is_running():
                log.info("TTS: sidecar ready")
                return True
            time.sleep(0.2)

        log.error("TTS: sidecar failed to start within 6s")
        _sidecar_proc.kill()
        _sidecar_proc = None
        return False


def shutdown() -> None:
    """Terminate the TTS sidecar (called at agent shutdown)."""
    global _sidecar_proc
    if _sidecar_proc and _sidecar_proc.poll() is None:
        _sidecar_proc.terminate()
        _sidecar_proc = None
        log.info("TTS: sidecar terminated")


# ---------------------------------------------------------------------------
# Synchronous HTTP call (safe to call from asyncio.to_thread / _dispatch)
# ---------------------------------------------------------------------------

def _http_post_speak(text: str, voice: str = "Ruth") -> bytes | None:
    """POST to the sidecar and return OGG Vorbis bytes, or None on failure."""
    if not _ensure_started():
        return None
    try:
        payload = json.dumps({"text": text, "voice": voice}).encode()
        req = urllib.request.Request(
            f"{_TTS_BASE}/speak",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except Exception as exc:
        log.warning("TTS: POST /speak failed — %s", exc)
        return None


# ---------------------------------------------------------------------------
# Playback
# ---------------------------------------------------------------------------

_playback_lock = threading.Lock()   # only one playback at a time
_stop_requested = threading.Event()


def _play_ogg(ogg_bytes: bytes) -> None:
    """Decode OGG Vorbis bytes and play through sounddevice (blocking)."""
    try:
        import soundfile as sf
        import sounddevice as sd
        import numpy as np

        audio, sample_rate = sf.read(io.BytesIO(ogg_bytes))
        if audio.ndim > 1:
            audio = audio.mean(axis=1)  # downmix to mono

        sd.play(audio.astype(np.float32), samplerate=sample_rate)
        # Poll for stop request instead of blocking indefinitely
        while sd.get_stream() and sd.get_stream().active:
            if _stop_requested.is_set():
                sd.stop()
                break
            time.sleep(0.05)

    except Exception as exc:
        log.warning("TTS: playback failed — %s", exc)


# ---------------------------------------------------------------------------
# Sentence splitter (for streaming LLM output)
# ---------------------------------------------------------------------------

def _split_sentences(text: str) -> list[str]:
    """Split text into speakable sentence chunks."""
    sentences = [s.strip() for s in _SENTENCE_END.split(text) if s.strip()]
    return sentences if sentences else [text.strip()] if text.strip() else []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class PollyStreamClient:
    """High-level TTS client — wraps the Node.js sidecar.

    Can be used as a module-level singleton (see `_default_client` below)
    or instantiated per-component.
    """

    def __init__(self, voice: str = "Ruth") -> None:
        self.voice = voice

    # --- Synchronous (for use from threads / _dispatch) --------------------

    def speak_sync(self, text: str) -> bool:
        """Speak `text` synchronously. Safe to call from asyncio.to_thread.

        Returns True if audio played, False on any error.
        """
        if not text.strip():
            return False
        _stop_requested.clear()
        ogg = _http_post_speak(text, voice=self.voice)
        if ogg is None:
            return False
        with _playback_lock:
            _play_ogg(ogg)
        return True

    # --- Async (for use from async contexts / DevAgent) --------------------

    async def speak(self, text: str) -> bool:
        """Async wrapper around speak_sync — runs in thread pool."""
        return await asyncio.to_thread(self.speak_sync, text)

    async def speak_stream(self, tokens: AsyncIterator[str]) -> str:
        """Consume an async token stream, speaking sentence-by-sentence.

        As the LLM generates tokens, they are buffered until a sentence
        boundary (.!?) is detected. Each complete sentence is sent to the
        sidecar for synthesis and played immediately, overlapping with the
        LLM generating the next sentence.

        Returns the full accumulated text.
        """
        _stop_requested.clear()
        buffer: list[str] = []
        full_text: list[str] = []
        playback_tasks: list[asyncio.Task] = []

        async def _play_sentence(sentence: str) -> None:
            ogg = await asyncio.to_thread(_http_post_speak, sentence, self.voice)
            if ogg:
                await asyncio.to_thread(_play_ogg, ogg)

        async for token in tokens:
            if _stop_requested.is_set():
                break

            full_text.append(token)
            buffer.append(token)
            combined = "".join(buffer)

            # Check for a sentence-ending punctuation followed by space or EOS
            sentences = _split_sentences(combined)
            if len(sentences) > 1:
                # First n-1 sentences are complete; keep the last as the next buffer
                for sentence in sentences[:-1]:
                    if sentence:
                        task = asyncio.create_task(_play_sentence(sentence))
                        playback_tasks.append(task)
                buffer = [sentences[-1]] if sentences[-1] else []
            elif combined.rstrip().endswith((".", "!", "?")):
                # Single complete sentence
                if combined.strip():
                    task = asyncio.create_task(_play_sentence(combined.strip()))
                    playback_tasks.append(task)
                buffer = []

        # Speak any remaining text after the token stream ends
        remainder = "".join(buffer).strip()
        if remainder and not _stop_requested.is_set():
            task = asyncio.create_task(_play_sentence(remainder))
            playback_tasks.append(task)

        # Wait for all in-flight playback tasks
        if playback_tasks:
            await asyncio.gather(*playback_tasks, return_exceptions=True)

        return "".join(full_text)

    def cancel(self) -> None:
        """Request that ongoing playback stops after the current audio chunk."""
        _stop_requested.set()
        try:
            import sounddevice as sd
            sd.stop()
        except Exception:
            pass

    def get_status(self) -> dict:
        return {
            "sidecar_running": _is_running(),
            "port": _TTS_PORT,
            "voice": self.voice,
        }


# ---------------------------------------------------------------------------
# Module-level default client (importable by command_executor + dev_agent)
# ---------------------------------------------------------------------------

_default_client: Optional[PollyStreamClient] = None


def get_client(voice: str = "Ruth") -> PollyStreamClient:
    """Return the module-level singleton, creating it on first call."""
    global _default_client
    if _default_client is None:
        _default_client = PollyStreamClient(voice=voice)
    return _default_client
