"""WhisperStream — GPU-accelerated speech transcription for the desktop agent.

Receives raw 16 kHz PCM audio chunks from the iPad (via `audio_stream`
WebSocket messages), buffers them, detects speech boundaries with a simple
energy VAD, and transcribes complete utterances with faster-whisper on the
RTX 5090 GPU.

On a completed utterance it emits:
    Command(source="voice", text=<transcript>, whisper_logprob=<avg_logprob>)
to FusionEngine.on_voice(), which routes it as priority-10 through
HybridCoordinator's full 4-gate pipeline.

Audio format expected from the iPad:
  - Sample rate : 16 000 Hz  (AVAudioEngine default, matches Whisper)
  - Encoding    : float32 LE or int16 LE detected automatically from bytes/frame
  - Chunk size  : ~100 ms  (1 600 float32 samples ≅ 6 400 bytes)

Graceful degradation: if faster-whisper or CUDA is unavailable the class
logs a warning and silently discards all audio — consistent with every other
optional hardware module in this project.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Optional

# Shared approval gate directory — approval_hook.py writes "pending",
# WhisperStream responds with the transcript in "response".
_APPROVAL_DIR = Path.home() / ".claude" / "approval"

log = logging.getLogger(__name__)

try:
    import numpy as np
    _NUMPY_AVAILABLE = True
except ImportError:
    _NUMPY_AVAILABLE = False

try:
    from faster_whisper import WhisperModel
    _WHISPER_AVAILABLE = True
except ImportError:
    _WHISPER_AVAILABLE = False
    log.warning("faster-whisper not installed — WhisperStream disabled. "
                "Install with: pip install faster-whisper")

if TYPE_CHECKING:
    from command_executor import Command
    from fusion_engine import FusionEngine


# ---------------------------------------------------------------------------
# Energy VAD helper
# ---------------------------------------------------------------------------

def _rms(chunk: "np.ndarray") -> float:
    """Root-mean-square amplitude of a float32 audio chunk."""
    return float(np.sqrt(np.mean(chunk.astype(np.float64) ** 2)))


def _has_trailing_silence(
    audio: "np.ndarray",
    sr: int,
    silence_s: float,
    threshold: float,
) -> bool:
    """True if the last `silence_s` seconds of `audio` are below `threshold` RMS."""
    n = int(sr * silence_s)
    if len(audio) < n:
        return False
    return _rms(audio[-n:]) < threshold


# ---------------------------------------------------------------------------
# WhisperStream
# ---------------------------------------------------------------------------

class WhisperStream:
    """Buffer iPad audio chunks and transcribe speech segments on the GPU.

    Wire-up (main.py):
        ws = WhisperStream()
        await ws.start()
        ws.set_fusion_engine(fusion)
        bridge.set_whisper_stream(ws)   # routes audio_stream messages here

        # After ContinuousTrainer.get_hotwords():
        ws.update_hotwords(["scroll", "click", ...])

        # On shutdown:
        await ws.stop()
    """

    SAMPLE_RATE = 16_000

    def __init__(
        self,
        model_size: str = "large-v3",
        device: str = "cuda",
        compute_type: str = "float16",
        # VAD parameters
        silence_threshold: float = 0.008,  # RMS below this = silence
        silence_duration_s: float = 0.6,   # trailing silence to end a segment
        min_speech_s: float = 0.3,         # minimum duration worth transcribing
        max_buffer_s: float = 30.0,        # force-transcribe after this long
        poll_interval_s: float = 0.15,     # background loop cadence
        # Phase 6: preserve audio bytes so Gate 1 can re-transcribe via
        # Amazon Transcribe when whisper_logprob is below the Gate 1 threshold.
        preserve_audio: bool = True,
    ) -> None:
        self._model_size = model_size
        self._device = device
        self._compute_type = compute_type
        self._silence_thresh = silence_threshold
        self._silence_s = silence_duration_s
        self._min_speech_s = min_speech_s
        self._max_buffer_s = max_buffer_s
        self._poll_s = poll_interval_s

        self._preserve_audio = preserve_audio
        self._model: Optional[WhisperModel] = None
        self._fusion: Optional["FusionEngine"] = None
        self._hotwords: list[str] = []

        # Audio buffer — float32 samples accumulated from audio_stream chunks.
        # Written by on_audio_chunk() from the event loop; read by the background
        # task. Both run in the same asyncio thread so no locking is needed.
        self._buffer: Optional["np.ndarray"] = None
        self._buffer_start_ts: float = 0.0   # monotonic time of first sample

        self._running = False
        self._task: Optional[asyncio.Task] = None
        self.available = False

    # ---------------------------------------------------------------------- #
    # Wiring
    # ---------------------------------------------------------------------- #

    def set_fusion_engine(self, fusion: "FusionEngine") -> None:
        self._fusion = fusion

    def update_hotwords(self, hotwords: list[str]) -> None:
        self._hotwords = list(hotwords)
        log.debug("WhisperStream: %d hotwords loaded", len(hotwords))

    # ---------------------------------------------------------------------- #
    # Lifecycle
    # ---------------------------------------------------------------------- #

    async def start(self) -> None:
        if not _WHISPER_AVAILABLE or not _NUMPY_AVAILABLE:
            log.warning("WhisperStream: dependencies missing — not starting")
            return
        log.info("WhisperStream: loading %s on %s (%s) ...",
                 self._model_size, self._device, self._compute_type)
        try:
            self._model = await asyncio.to_thread(
                WhisperModel,
                self._model_size,
                device=self._device,
                compute_type=self._compute_type,
            )
        except Exception as exc:
            log.error("WhisperStream: model load failed — %s", exc)
            return
        self.available = True
        self._running = True
        self._task = asyncio.create_task(self._loop())
        log.info("WhisperStream: ready (VAD threshold=%.3f silence=%.1fs)",
                 self._silence_thresh, self._silence_s)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._buffer = None
        self.available = False
        log.info("WhisperStream: stopped")

    # ---------------------------------------------------------------------- #
    # Audio ingestion — called from IPadBridge (event loop)
    # ---------------------------------------------------------------------- #

    def on_audio_chunk(self, samples_b64: str, frames: int) -> None:
        """Decode a base64 PCM chunk and append it to the buffer.

        Called synchronously from the asyncio event loop; must be fast.
        Auto-detects float32 vs int16 from bytes-per-frame ratio.
        """
        if not self.available or not _NUMPY_AVAILABLE:
            return
        try:
            raw = base64.b64decode(samples_b64)
            bytes_per_frame = len(raw) // max(frames, 1)

            if bytes_per_frame == 4:
                chunk = np.frombuffer(raw, dtype=np.float32)
            elif bytes_per_frame == 2:
                chunk = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            else:
                # Unknown format — try float32 anyway
                chunk = np.frombuffer(raw, dtype=np.float32)

            if self._buffer is None:
                self._buffer = chunk
                self._buffer_start_ts = time.monotonic()
            else:
                self._buffer = np.concatenate([self._buffer, chunk])
        except Exception as exc:
            log.debug("WhisperStream.on_audio_chunk decode error: %s", exc)

    # ---------------------------------------------------------------------- #
    # Background transcription loop
    # ---------------------------------------------------------------------- #

    async def _loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._poll_s)
            try:
                await self._maybe_transcribe()
            except Exception as exc:
                log.error("WhisperStream loop error: %s", exc)

    async def _maybe_transcribe(self) -> None:
        buf = self._buffer
        if buf is None or len(buf) == 0:
            return

        duration = len(buf) / self.SAMPLE_RATE

        # Too short to be speech
        if duration < self._min_speech_s:
            return

        force = duration >= self._max_buffer_s
        silence_at_end = _has_trailing_silence(
            buf, self.SAMPLE_RATE, self._silence_s, self._silence_thresh
        )

        if not force and not silence_at_end:
            return

        # Claim the buffer — clear before awaiting so new chunks go into a
        # fresh buffer while transcription runs on the old one
        audio = buf
        self._buffer = None
        self._buffer_start_ts = 0.0

        log.debug("WhisperStream: transcribing %.1f s of audio (force=%s)", duration, force)
        await asyncio.to_thread(self._transcribe, audio)

    def _transcribe(self, audio: "np.ndarray") -> None:
        """Run faster-whisper and emit Command(s) to FusionEngine. Blocking."""
        if self._model is None or self._fusion is None:
            return

        # Build initial_prompt from hotwords to bias the model
        initial_prompt = ", ".join(self._hotwords) if self._hotwords else None

        try:
            segments_iter, info = self._model.transcribe(
                audio,
                language="en",
                beam_size=5,
                vad_filter=True,           # silero-vad built into faster-whisper
                vad_parameters={
                    "min_silence_duration_ms": int(self._silence_s * 1000),
                    "speech_pad_ms": 100,
                },
                initial_prompt=initial_prompt,
            )
            segments = list(segments_iter)
        except Exception as exc:
            log.error("WhisperStream transcription error: %s", exc)
            return

        if not segments:
            log.debug("WhisperStream: VAD found no speech")
            return

        # Combine all segments into one Command; average their logprobs
        text = " ".join(s.text.strip() for s in segments if s.text.strip())
        if not text:
            return

        avg_logprob = sum(s.avg_logprob for s in segments) / len(segments)

        log.info("WhisperStream: %r (logprob=%.2f)", text, avg_logprob)

        from command_executor import Command
        params: dict = {}
        if self._preserve_audio:
            # Gate 1 re-transcription: HybridCoordinator._retranscribe() uses
            # these bytes when amazon-transcribe is installed and logprob is low.
            params["audio_bytes"] = audio.astype("int16").tobytes()
            params["sample_rate"] = self.SAMPLE_RATE

        # ---------------------------------------------------------------------------
        # Approval gate intercept — check before forwarding to FusionEngine.
        # approval_hook.py (Claude Code PreToolUse) writes a "pending" signal file
        # when waiting for a yes/no from the user. If we see it, write the
        # transcript as the approval response and suppress it from the pipeline so
        # "yes" / "no" never reaches the desktop action pipeline.
        # ---------------------------------------------------------------------------
        _approval_pending = _APPROVAL_DIR / "pending"
        _approval_response = _APPROVAL_DIR / "response"
        if _approval_pending.exists():
            try:
                _approval_response.write_text(text, encoding="utf-8")
                log.info("WhisperStream: approval response → %r", text)
            except Exception as exc:
                log.warning("WhisperStream: could not write approval response: %s", exc)
            return  # consumed by approval gate — do NOT forward to FusionEngine

        cmd = Command(
            text=text,
            action="DICTATE",   # placeholder — HybridCoordinator will reclassify
            source="voice",
            whisper_logprob=avg_logprob,
            params=params,
        )
        # on_voice() is thread-safe (just sets self._voice = cmd)
        self._fusion.on_voice(cmd)

    # ---------------------------------------------------------------------- #
    # Status
    # ---------------------------------------------------------------------- #

    def get_status(self) -> dict:
        buf_s = (len(self._buffer) / self.SAMPLE_RATE) if self._buffer is not None else 0.0
        return {
            "available": self.available,
            "model": self._model_size,
            "device": self._device,
            "buffer_s": round(buf_s, 2),
            "hotwords": len(self._hotwords),
        }
