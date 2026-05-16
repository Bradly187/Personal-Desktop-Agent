"""Chatterbox TTS — local GPU inference backend.

Wraps ResembleAI/chatterbox (MIT licence) running on the RTX 5090.
Mirrors the PollyStreamClient interface so polly_stream.get_client()
can dispatch here transparently when tts_backend == "chatterbox".

Key differences vs. Polly:
  - No network call — inference runs in-process on CUDA.
  - Batch only (no chunk streaming); speak_stream() is sentence-buffered.
  - exaggeration (0.25–2.0) controls emotional intensity.
  - Paralinguistic tags: [chuckle] [sigh] [gasp] [groan] [cough] etc.
  - Optional zero-shot voice cloning via audio_prompt_path (≥5 s WAV/MP3).
  - Model loads lazily on first call and stays resident in VRAM (~6–8 GB).
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from pathlib import Path
from typing import AsyncIterator, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy model singleton
# ---------------------------------------------------------------------------

_model = None
_model_lock = threading.Lock()
_synthesis_lock = threading.Lock()   # serialise generate() calls
_stop_requested = threading.Event()

_SENTENCE_END = re.compile(r'(?<=[.!?])\s+|(?<=[.!?])$')


def _get_model():
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is not None:
            return _model
        try:
            from chatterbox.tts import ChatterboxTTS  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "chatterbox-tts not installed — run: pip install chatterbox-tts"
            ) from exc
        log.info("ChatterboxTTS: loading onto CUDA (first call, ~10 s)…")
        _model = ChatterboxTTS.from_pretrained(device="cuda")
        log.info("ChatterboxTTS: ready  sr=%d Hz", _model.sr)
    return _model


# ---------------------------------------------------------------------------
# Playback (mirrors polly_stream._play_ogg pattern)
# ---------------------------------------------------------------------------

def _play_wav_tensor(wav, sample_rate: int) -> None:
    """Play a (1, N) float32 torch tensor through sounddevice."""
    try:
        import sounddevice as sd
        import numpy as np

        audio = wav.squeeze(0).cpu().numpy().astype(np.float32)
        sd.play(audio, samplerate=sample_rate)
        while sd.get_stream() and sd.get_stream().active:
            if _stop_requested.is_set():
                sd.stop()
                break
            time.sleep(0.05)
    except Exception as exc:
        log.warning("ChatterboxTTS: playback error — %s", exc)


# ---------------------------------------------------------------------------
# Core speak helpers
# ---------------------------------------------------------------------------

def speak_sync(
    text: str,
    exaggeration: float = 0.5,
    cfg_weight: float = 0.5,
    audio_prompt_path: Optional[str] = None,
) -> bool:
    """Synthesise *text* and play it synchronously.

    Safe to call from asyncio.to_thread (mirrors PollyStreamClient.speak_sync).
    Returns True if audio played, False on any error.
    """
    if not text.strip():
        return False
    _stop_requested.clear()
    try:
        model = _get_model()
        kwargs: dict = dict(
            exaggeration=exaggeration,
            cfg_weight=cfg_weight,
        )
        if audio_prompt_path and Path(audio_prompt_path).exists():
            kwargs["audio_prompt_path"] = audio_prompt_path
        with _synthesis_lock:
            wav = model.generate(text, **kwargs)
        _play_wav_tensor(wav, model.sr)
        return True
    except Exception as exc:
        log.error("ChatterboxTTS: synthesis error — %s", exc)
        return False


def cancel() -> None:
    """Stop current playback (next chunk won't start)."""
    _stop_requested.set()
    try:
        import sounddevice as sd
        sd.stop()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Public client class (drop-in for PollyStreamClient)
# ---------------------------------------------------------------------------

class ChatterboxClient:
    """Mirrors PollyStreamClient for use in polly_stream.get_client() dispatch."""

    def __init__(
        self,
        exaggeration: float = 0.5,
        cfg_weight: float = 0.5,
        audio_prompt_path: Optional[str] = None,
    ) -> None:
        self.exaggeration = exaggeration
        self.cfg_weight = cfg_weight
        self.audio_prompt_path = audio_prompt_path

    # --- Synchronous -------------------------------------------------------

    def speak_sync(self, text: str) -> bool:
        return speak_sync(
            text,
            exaggeration=self.exaggeration,
            cfg_weight=self.cfg_weight,
            audio_prompt_path=self.audio_prompt_path,
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
            ogg = await asyncio.to_thread(self.speak_sync, s)  # noqa: F841

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
        pass  # model stays resident in VRAM between calls

    def get_status(self) -> dict:
        return {
            "backend": "chatterbox",
            "model_loaded": _model is not None,
            "exaggeration": self.exaggeration,
            "cfg_weight": self.cfg_weight,
            "audio_prompt_path": self.audio_prompt_path,
        }
