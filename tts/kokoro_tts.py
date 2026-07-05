"""Kokoro TTS — local inference backend using ONNX.

Wraps the highly natural Kokoro TTS model (kokoro-onnx).
Mirrors the PollyStreamClient interface so polly_stream.get_client()
can dispatch here transparently when tts_backend == "kokoro".
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import time
from typing import AsyncIterator

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy model singleton
# ---------------------------------------------------------------------------

_kokoro = None
_kokoro_lock = threading.Lock()
_synthesis_lock = threading.Lock()   # serialise generate() calls
_playback_lock = threading.Lock()    # serialise sd.play() — concurrent calls clobber each other
_stop_requested = threading.Event()

_SENTENCE_END = re.compile(r'(?<=[.!?])\s+|(?<=[.!?])$')


def _select_onnx_provider() -> str:
    """Pick the ONNX execution provider for Kokoro and return its name.

    kokoro-onnx reads the ONNX_PROVIDER env var and uses that single provider
    (its own onnxruntime-gpu auto-detect via find_spec("onnxruntime-gpu") is a
    no-op — a hyphenated name is never importable — so the env var is the only
    reliable hook). We prefer CUDA on the RTX 5090 when onnxruntime-gpu is
    installed AND the CUDA provider actually loaded; otherwise we stay on CPU.
    Degrades gracefully: an absent/unusable GPU stack leaves CPU untouched.

    An explicitly-set ONNX_PROVIDER always wins (setdefault, never override).
    """
    try:
        import onnxruntime as ort
        available = ort.get_available_providers()
    except Exception:
        available = []
    provider = "CUDAExecutionProvider" if "CUDAExecutionProvider" in available else "CPUExecutionProvider"
    os.environ.setdefault("ONNX_PROVIDER", provider)
    return os.environ["ONNX_PROVIDER"]


def _get_kokoro():
    global _kokoro
    if _kokoro is not None:
        return _kokoro
    with _kokoro_lock:
        if _kokoro is not None:
            return _kokoro
        try:
            from kokoro_onnx import Kokoro
        except ImportError as exc:
            raise RuntimeError(
                "kokoro-onnx not installed — run: pip install kokoro-onnx sounddevice"
            ) from exc

        provider = _select_onnx_provider()
        log.info("Kokoro TTS: loading ONNX model… (provider=%s)", provider)
        base_dir = os.path.dirname(os.path.abspath(__file__))
        model_path = os.path.join(base_dir, "kokoro-v1.0.onnx")
        voices_path = os.path.join(base_dir, "voices.bin")
        if not os.path.exists(model_path) or not os.path.exists(voices_path):
             raise FileNotFoundError(f"Missing {model_path} or {voices_path}. Run download_kokoro.py")
             
        _kokoro = Kokoro(model_path, voices_path)
        log.info("Kokoro TTS: ready")
    return _kokoro


# ---------------------------------------------------------------------------
# Playback
# ---------------------------------------------------------------------------

def _play_audio(audio, sample_rate: int) -> None:
    """Play a numpy array through sounddevice."""
    try:
        import sounddevice as sd
        import numpy as np

        if audio.ndim > 1:
            audio = audio.mean(axis=1) # downmix to mono

        sd.play(audio.astype(np.float32), samplerate=sample_rate)
        while sd.get_stream() and sd.get_stream().active:
            if _stop_requested.is_set():
                sd.stop()
                break
            time.sleep(0.05)
    except Exception as exc:
        log.warning("Kokoro TTS: playback error — %s", exc)


# ---------------------------------------------------------------------------
# Core speak helpers
# ---------------------------------------------------------------------------

def speak_sync(
    text: str,
    voice: str = "af_bella",
    speed: float = 1.0,
) -> bool:
    """Synthesise *text* and play it synchronously.

    Safe to call from asyncio.to_thread (mirrors PollyStreamClient.speak_sync).
    Returns True if audio played, False on any error.
    """
    if not text.strip():
        return False
    _stop_requested.clear()
    try:
        kokoro = _get_kokoro()
        with _synthesis_lock:
            # Generate audio using ONNX (serialised — one inference at a time)
            samples, sample_rate = kokoro.create(text, voice=voice, speed=speed, lang="en-us")
        # Play outside the synthesis lock so the next sentence can generate while
        # this one plays, but under _playback_lock so concurrent speak_stream
        # tasks can't clobber each other on the single sounddevice output.
        if not _stop_requested.is_set() and samples is not None and len(samples) > 0:
            with _playback_lock:
                if not _stop_requested.is_set():
                    _play_audio(samples, sample_rate)
        return True
    except Exception as exc:
        log.error("Kokoro TTS: synthesis error — %s", exc)
        return False


def cancel() -> None:
    """Stop current playback."""
    _stop_requested.set()
    try:
        import sounddevice as sd
        sd.stop()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Public client class (drop-in for PollyStreamClient)
# ---------------------------------------------------------------------------

class KokoroClient:
    """Mirrors PollyStreamClient for use in polly_stream.get_client() dispatch."""

    def __init__(
        self,
        voice: str = "af_bella",
        speed: float = 1.0,
    ) -> None:
        self.voice = voice
        self.speed = speed

    # --- Synchronous -------------------------------------------------------

    def speak_sync(self, text: str) -> bool:
        return speak_sync(
            text,
            voice=self.voice,
            speed=self.speed,
        )

    # --- Async wrappers (sentence-buffered, not chunk streaming) -----------

    async def speak(self, text: str) -> bool:
        return await asyncio.to_thread(self.speak_sync, text)

    async def speak_stream(self, tokens: AsyncIterator[str]) -> str:
        """Buffer LLM tokens and speak sentence-by-sentence.

        Plays each complete sentence as soon as it's synthesised, overlapping
        with the LLM generating the next sentence.  Returns accumulated text.
        """
        _stop_requested.clear()
        buffer: list[str] = []
        full_text: list[str] = []
        playback_tasks: list[asyncio.Task] = []

        async def _play_sentence(s: str) -> None:
            await asyncio.to_thread(self.speak_sync, s)

        async for token in tokens:
            if _stop_requested.is_set():
                break
            full_text.append(token)
            buffer.append(token)
            combined = "".join(buffer)
            sentences = [s.strip() for s in _SENTENCE_END.split(combined) if s.strip()]
            if len(sentences) > 1:
                for sentence in sentences[:-1]:
                    playback_tasks.append(asyncio.create_task(_play_sentence(sentence)))
                buffer = [sentences[-1]] if sentences[-1] else []
            elif combined.rstrip().endswith((".", "!", "?")):
                if combined.strip():
                    playback_tasks.append(asyncio.create_task(_play_sentence(combined.strip())))
                buffer = []

        remainder = "".join(buffer).strip()
        if remainder and not _stop_requested.is_set():
            playback_tasks.append(asyncio.create_task(_play_sentence(remainder)))

        if playback_tasks:
            await asyncio.gather(*playback_tasks, return_exceptions=True)

        return "".join(full_text)

    def cancel(self) -> None:
        cancel()

    def shutdown(self) -> None:
        pass  # model stays resident in memory between calls

    def get_status(self) -> dict:
        return {
            "backend": "kokoro-onnx",
            "model_loaded": _kokoro is not None,
            "voice": self.voice,
            "speed": self.speed,
        }
