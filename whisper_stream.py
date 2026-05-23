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

    # Hallucination filters — faster-whisper returns these per segment.
    # no_speech_prob: probability the segment is silence/noise (0–1).
    #   > 0.5 means Whisper itself thinks there was no speech → discard.
    # avg_logprob_floor: very low log-probability transcriptions are usually
    #   hallucinated words from background noise → discard.
    NO_SPEECH_PROB_MAX: float = 0.5
    AVG_LOGPROB_FLOOR: float = -0.8

    # Wake phrase — transcripts must start with one of these (case-insensitive)
    # to be forwarded to FusionEngine.  Bypassed when awaiting a clarification
    # response so the user can say "up" without needing "hey agent up".
    WAKE_PHRASES: tuple[str, ...] = ("hey agent", "agent")

    def __init__(
        self,
        model_size: str = "large-v3",
        device: str = "cuda",
        compute_type: str = "float16",
        # VAD parameters — raised silence_threshold from 0.008 to 0.015 to
        # reduce how much background noise enters the transcription pipeline.
        silence_threshold: float = 0.015,  # RMS below this = silence
        silence_duration_s: float = 0.6,   # trailing silence to end a segment
        min_speech_s: float = 0.5,         # minimum duration worth transcribing
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
        # Static hotwords always included in the initial_prompt to bias Whisper
        # toward app names and domain vocab that it would otherwise misspell.
        # Word-list initial_prompt biases Whisper toward brand names without
        # triggering prompt-bleeding (Whisper echoing the prompt into output).
        # IMPORTANT: never include "hey agent" here — it would cause the wake-
        # phrase filter to pass lecture / ambient audio as commands.
        self._static_hotwords: list[str] = [
            "Kiro IDE", "Kiro", "Slack", "Discord", "Claude",
        ]
        self._hotwords: list[str] = []

        # Audio buffer — float32 samples accumulated from audio_stream chunks.
        # Written by on_audio_chunk() from the event loop; read by the background
        # task. Both run in the same asyncio thread so no locking is needed.
        #
        # Uses a list accumulator (_buffer_chunks) for O(1) appends per chunk.
        # Concatenated into a single ndarray only at transcription time.
        self._buffer_chunks: list["np.ndarray"] = []
        self._buffer_start_ts: float | None = None  # monotonic time of first sample

        self._running = False
        self._task: Optional[asyncio.Task] = None
        self.available = False
        self._suppress_until: float = 0.0
        self._awaiting_clarification: bool = False
        self._clarification_deadline: float = 0.0
        self._agent_db = None
        self._session_id: int = -1
        self._lecture_mode: bool = False
        self._event_loop = None
        self._calibration_capture = None
        self._profiler = None
        self._logprob_floor_override: float | None = None
        self._gaze_cal_trigger = None  # callable: () → None, set by main.py

    # ---------------------------------------------------------------------- #
    # Wiring
    # ---------------------------------------------------------------------- #

    def set_fusion_engine(self, fusion: "FusionEngine") -> None:
        self._fusion = fusion

    def set_acoustic_profiler(self, profiler) -> None:
        """Wire AcousticProfiler for per-user adaptive thresholds."""
        self._profiler = profiler
        # Apply stored thresholds immediately (profile already loaded)
        self._silence_thresh = profiler.get_vad_threshold()
        self._logprob_floor_override = profiler.get_logprob_floor()
        log.info(
            "WhisperStream: acoustic profile applied — vad=%.3f logprob_floor=%.2f",
            self._silence_thresh, self._logprob_floor_override,
        )

    def set_agent_db(self, agent_db, session_id: int = -1) -> None:
        """Wire AgentDB so lecture-mode transcriptions can be stored.

        Must be called from the running event loop so we can capture it for
        thread-safe coroutine scheduling from _transcribe (a worker thread).
        """
        self._agent_db = agent_db
        self._session_id = session_id
        import asyncio as _asyncio
        try:
            self._event_loop = _asyncio.get_running_loop()
        except RuntimeError:
            self._event_loop = None

    def set_gaze_calibration_trigger(self, callback) -> None:
        """Set callable to invoke when 'calibrate monitor' is spoken after wake phrase."""
        self._gaze_cal_trigger = callback

    def set_lecture_mode(self, enabled: bool) -> None:
        """Enable/disable lecture mode — stores non-command audio to AgentDB."""
        self._lecture_mode = enabled

    def set_calibration_capture(self, callback) -> None:
        """One-shot capture: next transcript goes to callback(text, logprob, duration_s).
        Set to None to cancel. Used by VoiceCalibrator during guided sessions.
        """
        self._calibration_capture = callback
        log.info("WhisperStream: calibration capture %s", "SET" if callback else "CLEARED")

    def set_awaiting_clarification(self, active: bool) -> None:
        """Called by HybridCoordinator to bypass wake-phrase check during Q&A."""
        self._awaiting_clarification = active
        self._clarification_deadline = (
            time.monotonic() + 15.0 if active else 0.0
        )

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
        self._buffer_chunks = []
        self._buffer_start_ts = None
        self.available = False
        log.info("WhisperStream: stopped")

    # ---------------------------------------------------------------------- #
    # Audio ingestion — called from IPadBridge (event loop)
    # ---------------------------------------------------------------------- #

    def suppress(self, seconds: float) -> None:
        """Discard incoming audio for `seconds` after TTS playback ends.

        Called by HybridCoordinator immediately after speak_sync() returns so
        that Danielle's voice echoing through the room doesn't re-enter the
        pipeline as a new command.  Also flushes any audio already buffered
        during TTS playback.
        """
        self._suppress_until = time.monotonic() + seconds
        self._buffer_chunks = []
        self._buffer_start_ts = None
        log.debug("WhisperStream: suppressing mic for %.1fs (post-TTS echo guard)", seconds)

    def on_audio_chunk(self, samples_b64: str, frames: int) -> None:
        """Decode a base64 PCM chunk and append it to the buffer.

        Called synchronously from the asyncio event loop; must be fast.
        Auto-detects float32 vs int16 from bytes-per-frame ratio.

        Uses a list accumulator instead of np.concatenate per call to avoid
        O(n) reallocation on every chunk (100ms intervals, up to 30s = 480k samples).
        The list is concatenated once at transcription time.
        """
        if not self.available or not _NUMPY_AVAILABLE:
            return
        if time.monotonic() < self._suppress_until:
            return  # post-TTS echo guard — discard mic echo
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

            self._buffer_chunks.append(chunk)
            if self._buffer_start_ts is None:
                self._buffer_start_ts = time.monotonic()
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
        if not self._buffer_chunks:
            return

        # Concatenate once (O(n) total, not O(n) per chunk)
        buf = np.concatenate(self._buffer_chunks)
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
        self._buffer_chunks = []
        self._buffer_start_ts = None

        log.debug("WhisperStream: transcribing %.1f s of audio (force=%s)", duration, force)
        await asyncio.to_thread(self._transcribe, audio)

    def _transcribe(self, audio: "np.ndarray") -> None:
        """Run faster-whisper and emit Command(s) to FusionEngine. Blocking."""
        if self._model is None or self._fusion is None:
            return

        # Build initial_prompt from static + dynamic hotwords to bias the model.
        # Static hotwords cover app names Whisper consistently misspells.
        all_hotwords = self._static_hotwords + self._hotwords
        initial_prompt = ", ".join(all_hotwords) if all_hotwords else None

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

        # Hallucination filter: drop segments where Whisper itself signals
        # low confidence or likely silence.  Single-word outputs like "direction",
        # "you", "the" from background noise are almost always caught here.
        valid = []
        for s in segments:
            if s.no_speech_prob > self.NO_SPEECH_PROB_MAX:
                log.debug(
                    "WhisperStream: dropping hallucination (no_speech_prob=%.2f): %r",
                    s.no_speech_prob, s.text.strip(),
                )
                continue
            if s.avg_logprob < self.AVG_LOGPROB_FLOOR:
                log.debug(
                    "WhisperStream: dropping low-confidence segment (logprob=%.2f): %r",
                    s.avg_logprob, s.text.strip(),
                )
                continue
            valid.append(s)

        if not valid:
            log.debug("WhisperStream: all segments filtered as hallucinations")
            return

        # Combine all segments into one Command; average their logprobs
        text = " ".join(s.text.strip() for s in valid if s.text.strip())
        if not text:
            return

        avg_logprob = sum(s.avg_logprob for s in valid) / len(valid)

        log.info("WhisperStream: %r (logprob=%.2f)", text, avg_logprob)

        # Feed acoustic profiler — builds per-user voice model over time
        if self._profiler is not None:
            self._profiler.record(
                audio=audio,
                avg_logprob=avg_logprob,
                actual_text=text,
                sr=self.SAMPLE_RATE,
            )

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

        # Calibration capture: one-shot intercept for VoiceCalibrator sessions.
        # Fires before wake-phrase gate so the user doesn't need "hey agent".
        if self._calibration_capture is not None:
            cb = self._calibration_capture
            self._calibration_capture = None  # consume — one shot only
            duration_s = len(audio) / self.SAMPLE_RATE
            log.info("WhisperStream: calibration capture: %r (logprob=%.2f)", text, avg_logprob)
            cb(text, avg_logprob, duration_s)
            return  # don't route to FusionEngine

        # Post-TTS echo guard: discard transcription results that completed
        # while the mic was suppressed (e.g. Danielle's voice buffered during
        # TTS playback and transcribed just as suppress() was called).
        if time.monotonic() < self._suppress_until:
            log.debug("WhisperStream: discarding transcription during suppress window: %r", text)
            return

        # Wake-phrase gate: discard anything that doesn't start with a wake
        # phrase, UNLESS we're waiting for a clarification answer.
        now_mono = time.monotonic()
        awaiting = self._awaiting_clarification and now_mono < self._clarification_deadline

        if awaiting:
            # Auto-expire if 15s elapsed with no valid answer
            if now_mono >= self._clarification_deadline:
                log.info("WhisperStream: clarification timed out — re-enabling wake phrase")
                self._awaiting_clarification = False
                awaiting = False
            else:
                # Clarification answers should be short (1–6 words).
                # Reject long sentences — they are almost certainly lecture audio
                # leaking through the open gate, not the user's answer.
                word_count = len(text.split())
                if word_count > 6:
                    log.debug(
                        "WhisperStream: discarding long clarification response "
                        "(%d words) — likely ambient audio: %r", word_count, text
                    )
                    return
                # Stricter logprob during clarification
                if avg_logprob < -0.5:
                    log.debug(
                        "WhisperStream: discarding low-confidence clarification "
                        "response (logprob=%.2f): %r", avg_logprob, text
                    )
                    return

        if not awaiting:
            import re as _re
            # Normalise: lowercase + collapse all punctuation/whitespace to
            # single spaces so "Hey, Agent, open kiro." matches "hey agent".
            normalised = _re.sub(r'[^\w\s]', ' ', text.lower())
            normalised = _re.sub(r'\s+', ' ', normalised).strip()

            matched = next(
                (p for p in sorted(self.WAKE_PHRASES, key=len, reverse=True)
                 if normalised.startswith(p)),
                None,
            )
            if matched is None:
                if self._lecture_mode and self._agent_db and self._agent_db.available \
                        and self._event_loop is not None:
                    log.debug("WhisperStream: lecture mode — storing: %r", text)
                    import asyncio as _asyncio
                    _asyncio.run_coroutine_threadsafe(
                        self._agent_db.insert_ambient_transcript(
                            session_id=self._session_id,
                            text=text,
                            logprob=avg_logprob,
                            duration_s=len(audio) / self.SAMPLE_RATE,
                        ),
                        self._event_loop,
                    )
                else:
                    log.debug("WhisperStream: no wake phrase — discarding: %r", text)
                return

            # Strip wake phrase words from the original text by word count
            phrase_word_count = len(matched.split())
            words = _re.split(r'[\s,\.]+', text.strip())
            command_words = [w for w in words[phrase_word_count:] if w]
            text = ' '.join(command_words)

            # Discard if nothing meaningful remains after stripping wake phrase
            if not text or not _re.search(r'[a-zA-Z]', text):
                log.debug("WhisperStream: wake phrase with no command — discarding")
                return

            log.info("WhisperStream: wake phrase detected, command: %r", text)

            # Intercept "calibrate monitor" before it reaches the LLM pipeline.
            if text.lower().startswith("calibrate monitor") and self._gaze_cal_trigger:
                log.info("WhisperStream: 'calibrate monitor' → launching gaze calibration")
                if self._event_loop is not None:
                    import asyncio as _asyncio
                    _asyncio.run_coroutine_threadsafe(
                        self._gaze_cal_trigger(),
                        self._event_loop,
                    )
                return

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
        buf_samples = sum(len(c) for c in self._buffer_chunks)
        buf_s = buf_samples / self.SAMPLE_RATE if buf_samples > 0 else 0.0
        return {
            "available": self.available,
            "model": self._model_size,
            "device": self._device,
            "buffer_s": round(buf_s, 2),
            "hotwords": len(self._hotwords),
        }
