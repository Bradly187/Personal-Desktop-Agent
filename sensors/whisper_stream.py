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
import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

# Shared approval gate directory — approval_hook.py writes "pending",
# WhisperStream responds with the canonical verdict in "response".
_APPROVAL_DIR = Path.home() / ".claude" / "approval"

# Seconds to flush+suppress the mic when the approval gate first opens, so the
# TTS echo of Danielle's spoken "Approve …?" question (which contains the word
# "approve") isn't transcribed and mistaken for the user's answer.
_APPROVAL_ECHO_GUARD_S: float = 1.0

# Shared confirmation vocabulary — kept in core/ so approval_hook.py and this
# module parse identical yes/no language and can never drift out of sync.
from core.approval_keywords import classify_confirmation

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CUDA DLL search path — must be registered before ctranslate2 / faster-whisper
# are imported so ctranslate2's dynamic LoadLibrary calls find cublas64_12.dll.
#
# Python 3.8+ restricts DLL search for extension modules to explicitly
# registered directories (os.add_dll_directory), not the process PATH.
# ctranslate2 also loads CUDA libs at runtime, which goes through LoadLibraryW
# and therefore also benefits from add_dll_directory on Windows.
#
# The DLL directory is the torch\lib folder from a CUDA-enabled torch install.
# If torch gets reinstalled with CUDA the new path will be torch\lib (no tilde);
# we probe both so the code stays correct after a reinstall.
# ---------------------------------------------------------------------------
if sys.platform == "win32":
    _SITE_PACKAGES = Path(os.path.expandvars(
        r"%APPDATA%\Python\Python314\site-packages"
    ))
    _CUDA_DLL_CANDIDATES = [
        _SITE_PACKAGES / "torch" / "lib",       # proper CUDA torch install
        _SITE_PACKAGES / "~orch" / "lib",        # DLLs left by previous CUDA torch
        Path(r"C:\Users\bradt\AppData\Local\Programs\Ollama\lib\ollama\cuda_v12"),
    ]
    for _candidate in _CUDA_DLL_CANDIDATES:
        if (_candidate / "cublas64_12.dll").exists():
            try:
                os.add_dll_directory(str(_candidate))
                log.info("Registered CUDA DLL directory: %s", _candidate)
            except OSError as _e:
                log.warning("Could not register CUDA DLL dir %s: %s", _candidate, _e)
            break
    else:
        log.warning(
            "cublas64_12.dll not found in any candidate directory — "
            "faster-whisper GPU inference will fail. Install CUDA toolkit 12.x "
            "or reinstall torch with CUDA: pip install torch --index-url "
            "https://download.pytorch.org/whl/cu124"
        )

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

try:
    import webrtcvad
    _WEBRTCVAD_AVAILABLE = True
except ImportError:
    _WEBRTCVAD_AVAILABLE = False
    log.warning("webrtcvad not installed — streaming VAD falls back to the RMS "
                "energy gate. Install with: pip install webrtcvad-wheels")

if TYPE_CHECKING:
    from core.fusion_engine import FusionEngine


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


class _StreamingVAD:
    """Neural trailing-silence detector for utterance segmentation.

    Wraps webrtcvad's GMM classifier. webrtcvad labels each fixed-size frame
    (10/20/30 ms at 8/16/32/48 kHz) as speech/non-speech independently — cheap
    enough (~tens of µs/frame, C, no allocation, no GPU) to run on the asyncio
    event loop every poll tick. We use 30 ms frames at 16 kHz.

    Per-frame labels flicker, so segmentation reads the *fraction* of voiced
    frames over the trailing-silence window: the window is "silent" when the
    voiced fraction is at or below ``end_ratio``. This is the neural analog of
    the RMS ``_has_trailing_silence`` test and drives exactly the same
    end-of-utterance decision — only the per-frame speech test changes.
    """

    FRAME_MS: int = 30

    def __init__(self, aggressiveness: int, sample_rate: int) -> None:
        # webrtcvad aggressiveness 0 (least) .. 3 (most aggressive at filtering
        # out non-speech). Raises if out of range or sample_rate unsupported.
        self._vad = webrtcvad.Vad(aggressiveness)
        self._sr = sample_rate
        self._frame_samples = int(sample_rate * self.FRAME_MS / 1000)

    def trailing_silence(
        self,
        tail_int16: "np.ndarray",
        silence_s: float,
        end_ratio: float,
    ) -> bool:
        """True if the last ``silence_s`` of ``tail_int16`` is non-speech.

        ``tail_int16`` is mono 16-bit PCM at ``self._sr``. Only the final
        ``silence_s`` worth of whole 30 ms frames is examined. Returns False
        when there isn't a full window of frames yet (utterance still open).
        """
        n_window = int(self._sr * silence_s)
        if n_window <= 0 or len(tail_int16) < n_window:
            return False
        window = tail_int16[-n_window:]
        fs = self._frame_samples
        n_frames = len(window) // fs
        if n_frames == 0:
            return False
        voiced = 0
        for i in range(n_frames):
            frame = window[i * fs:(i + 1) * fs]
            # webrtcvad wants raw little-endian int16 bytes.
            if self._vad.is_speech(frame.tobytes(), self._sr):
                voiced += 1
        return (voiced / n_frames) <= end_ratio


def _log_future_exc(fut) -> None:
    """done-callback for run_coroutine_threadsafe fire-and-forget coroutines.

    Without this, an exception inside a scheduled coroutine (composer dictation,
    ambient-transcript DB write) is stored on the future and only surfaces as
    Python's contextless "exception was never retrieved" warning. Logging it here
    keeps a background failure from disappearing silently — and a failed non-
    critical write can never affect the command path it was forked off of.
    """
    try:
        exc = fut.exception()
    except Exception:
        return  # cancelled or not-yet-done — nothing to report
    if exc is not None:
        log.warning("WhisperStream: background task failed (non-fatal): %s", exc)


# ---------------------------------------------------------------------------
# Bypass-gate sanity filter
# ---------------------------------------------------------------------------
# The wake-armed and awaiting-clarification paths accept an utterance WITHOUT a
# wake phrase, so a Whisper misrecognition can pose as a command there
# ("agent compose note" misheard as "are you taking us now?"). These reject text
# that is clearly not a command/answer: an interrogative, or text sharing no token
# with the command surface. Erring toward rejection is safe in a bypass gate — a
# dropped utterance just means the user repeats it; a false-accept fires an action.

_QUESTION_OPENERS: frozenset = frozenset({
    "are", "is", "was", "were", "am", "do", "does", "did", "can", "could",
    "would", "should", "will", "shall", "may", "might", "have", "has", "had",
    "what", "whats", "why", "how", "when", "where", "who", "whom", "whose", "which",
})

_COMMAND_VOCAB: frozenset = frozenset({
    # desktop / accessibility actions
    "click", "scroll", "open", "close", "type", "press", "copy", "paste", "save",
    "undo", "redo", "select", "focus", "minimize", "maximize", "screenshot",
    "dictate", "hotkey", "drag", "move", "resize", "switch", "tab", "window", "up",
    "down", "left", "right", "enter", "escape", "delete", "back", "forward", "next",
    "previous", "play", "pause", "stop", "mute", "unmute", "volume", "page",
    # confirmation / short clarification answers
    "yes", "no", "yeah", "nope", "yep", "cancel", "approve", "confirm", "ok",
    "okay", "sure", "the", "that", "this", "it", "one", "first", "second", "third",
    "top", "bottom", "here",
    # dev / general verbs
    "fix", "write", "explain", "find", "show", "search", "read", "run", "commit",
    "test", "build", "make", "create", "add", "remove", "update", "refactor",
    "review", "check", "summarize", "summarise", "plan", "implement", "debug",
    "compose", "note", "notes",
    # skill nouns / triggers
    "journal", "weather", "forecast", "calendar", "meeting", "event", "email",
    "inbox", "reply", "paper", "papers", "arxiv", "diagram", "file", "files",
    "reminder", "remind", "pain", "log", "medication", "dose", "brief", "schedule",
})


def bypass_reject_reason(text: str, extra_vocab: frozenset | set = frozenset()) -> Optional[str]:
    """Why a bypass-gate utterance is NOT a plausible command/answer, else None.

    Rejects: empty text, interrogatives (trailing '?' or a question-word opener),
    and text sharing no token with the command vocabulary (+ live hotwords passed
    as extra_vocab). Pure/stdlib so it is unit-tested without a model.
    """
    import re as _re
    t = (text or "").strip()
    if not t:
        return "empty"
    if t.endswith("?"):
        return "interrogative (ends with '?')"
    words = _re.findall(r"[a-z0-9']+", t.lower())
    if words and words[0] in _QUESTION_OPENERS:
        return f"interrogative (opens with {words[0]!r})"
    if not (set(words) & (_COMMAND_VOCAB | set(extra_vocab))):
        return "no overlap with command vocabulary"
    return None


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
    # Includes common Whisper mishearings of "agent" — observed live: "agent"
    # → "aiden"; also "agents"/"a gent". Erring toward firing is the right
    # trade-off for a single-user accessibility tool (the alternative is a
    # silently-dropped command). Longest match wins (sorted in the matcher).
    WAKE_PHRASES: tuple[str, ...] = (
        "hey agent", "hey aiden", "hey agents", "hey a gent",
        "agent", "aiden", "agents",
    )
    # Seconds a bare wake phrase stays "armed" — the next utterance within this
    # window is accepted as the command without repeating the wake. Tolerates
    # the VAD splitting "hey agent <pause> pain day on" into two segments.
    WAKE_ARM_WINDOW: float = 6.0

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
        # Streaming neural VAD — drives the end-of-utterance segmentation gate
        # ahead of the RMS check (RMS stays as the fallback). See _StreamingVAD.
        use_neural_vad: bool = True,
        vad_aggressiveness: int = 2,       # webrtcvad mode 0 (lax) .. 3 (strict)
        vad_speech_end_ratio: float = 0.10,  # voiced-frame fraction ≤ this = silent
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

        # Streaming neural VAD — opt-in (default on), disabled if webrtcvad is
        # missing or DA_NEURAL_VAD=0. Construction failure (bad aggressiveness /
        # unsupported sample rate) degrades to the RMS gate rather than crashing.
        self._vad_end_ratio = vad_speech_end_ratio
        self._use_neural_vad = (
            use_neural_vad and _WEBRTCVAD_AVAILABLE
            and os.environ.get("DA_NEURAL_VAD", "1") != "0"
        )
        self._neural_vad: Optional["_StreamingVAD"] = None
        if self._use_neural_vad:
            try:
                self._neural_vad = _StreamingVAD(vad_aggressiveness, self.SAMPLE_RATE)
            except Exception as exc:
                log.warning("WhisperStream: neural VAD init failed (%s) — using RMS gate", exc)
                self._use_neural_vad = False

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
            "VS Code", "VSCode", "Slack", "Discord", "Claude",
            # Bias toward the real note phrasing so it isn't misheard as ambient
            # speech (e.g. "compose a note" -> "are you taking us now").
            "compose a note", "take a note", "add a note",
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
        # Tracks whether the approval gate is currently open so the TTS echo of
        # the spoken question is flushed exactly once when the gate first opens.
        self._approval_pending_active: bool = False
        # Wake-arming: a bare "hey agent" (VAD split it off from the command)
        # arms this window; the next segment is taken as the command.
        self._wake_armed_until: float = 0.0
        self._agent_db = None
        self._session_id: int = -1
        self._lecture_mode: bool = False
        self._event_loop = None
        self._calibration_capture = None
        self._profiler = None
        self._metrics = None   # set via set_metrics()
        self._logprob_floor_override: float | None = None
        self._pain_day_active: bool = False  # tracked for apply_pain_day() idempotence
        self._muted: bool = False           # hard mic mute; unmute via iPad button
        # Fired whenever the mute state changes (voice- or iPad-initiated) so the
        # bridge can push a `mic_state` message and keep the iPad indicator in sync.
        self.on_mute_change: Optional[Callable[[bool], None]] = None
        # Fired once when the approval gate opens (not-open → open), carrying the
        # spoken action description. main.py wires this to push an A2UI Approve/
        # Deny surface to the iPad — a parallel input to the voice gate.
        self.on_approval_gate_open: Optional[Callable[[str], None]] = None
        # D8: correction detection state — tracks last command outcome
        self._last_command_status: str = ""   # "ok" | "CLARIFY" | "failed"
        self._last_command_text: str = ""     # text of previous command
        self._composer = None                 # VoicePromptComposer | None

    # ---------------------------------------------------------------------- #
    # Wiring
    # ---------------------------------------------------------------------- #

    def set_fusion_engine(self, fusion: "FusionEngine") -> None:
        self._fusion = fusion

    def set_composer(self, composer) -> None:
        """Wire VoicePromptComposer for voice-to-Claude-Code prompt dictation."""
        self._composer = composer

    def set_metrics(self, metrics) -> None:
        """Wire the global Metrics singleton for whisper latency + hallucination tracking."""
        self._metrics = metrics

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

    def apply_pain_day(self, active: bool) -> None:
        """Relax (or restore) the voice recognizer for a pain/flare day.

        Single source of truth for pain-day voice adaptation — pulls BOTH the
        VAD silence threshold and the Gate 1 logprob floor from the profiler so
        a softer, lower-confidence voice still registers. Idempotent, so it is
        safe to call from HybridCoordinator.route() on every command as well as
        from the immediate manual-override sites (voice keyword / iPad toggle).

        No-op when no AcousticProfiler is wired (the recognizer keeps its
        constructor defaults).
        """
        if active == self._pain_day_active:
            return  # idempotent — no work on the common no-change path
        self._pain_day_active = active
        if self._profiler is None:
            return
        self._silence_thresh = self._profiler.get_vad_threshold(pain_day=active)
        self._logprob_floor_override = self._profiler.get_logprob_floor(pain_day=active)
        log.info(
            "WhisperStream: pain-day %s — vad=%.3f logprob_floor=%.2f",
            "ON" if active else "OFF",
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

    def set_last_command_status(self, status: str, text: str) -> None:
        """D8: Called by HybridCoordinator after each route() so the next
        utterance can be detected as a correction if the previous command failed."""
        self._last_command_status = status
        self._last_command_text = text

    def set_muted(self, muted: bool) -> None:
        """Hard mute/unmute the microphone input.

        When muted, on_audio_chunk() silently drops all audio — identical to
        having no mic connected. The buffer is cleared on mute so no queued
        audio fires after unmute. Unmute is iPad-driven (mic_mute message with
        muted=false); voice cannot unmute because the mic is deaf when muted.
        """
        self._muted = muted
        if muted:
            self._buffer_chunks = []
            self._buffer_start_ts = None
        log.info("WhisperStream: mic %s", "MUTED" if muted else "UNMUTED")
        if self.on_mute_change is not None:
            try:
                self.on_mute_change(muted)
            except Exception as exc:  # never let a UI sync failure break muting
                log.debug("on_mute_change callback failed: %s", exc)

    def set_lecture_mode(self, enabled: bool) -> None:
        """Enable/disable lecture mode — stores non-command audio to AgentDB."""
        self._lecture_mode = enabled

    def set_calibration_capture(self, callback) -> None:
        """One-shot capture: next transcript goes to callback(text, logprob, duration_s).
        Set to None to cancel. Used by VoiceCalibrator during guided sessions.
        """
        self._calibration_capture = callback
        log.info("WhisperStream: calibration capture %s", "SET" if callback else "CLEARED")

    def _bypass_reject_reason(self, text: str) -> Optional[str]:
        """Bypass-gate filter with live hotwords folded into the command vocab.
        Returns a reason to reject the utterance (it can't pose as a command in a
        wake-armed / awaiting-clarification gate), or None if it looks plausible."""
        import re as _re
        hotword_tokens = {
            w for hw in (self._static_hotwords + self._hotwords)
            for w in _re.findall(r"[a-z0-9']+", hw.lower())
        }
        return bypass_reject_reason(text, hotword_tokens)

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
        if not _NUMPY_AVAILABLE:
            log.warning("WhisperStream: numpy missing — not starting")
            return

        if not _WHISPER_AVAILABLE:
            log.warning("WhisperStream: faster-whisper missing — not starting")
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
            log.warning("WhisperStream: %s model load failed (%s) — retrying on CPU/int8",
                        self._device, exc)
            try:
                self._model = await asyncio.to_thread(
                    WhisperModel, self._model_size, device="cpu", compute_type="int8"
                )
                self._device = "cpu"
                self._compute_type = "int8"
                log.info("WhisperStream: running on CPU (int8) as fallback")
            except Exception as cpu_exc:
                log.error("WhisperStream: CPU fallback also failed — voice disabled: %s", cpu_exc)
                return
        self.available = True
        self._running = True
        self._task = asyncio.create_task(self._loop())
        log.info("WhisperStream: ready (gate=%s, VAD threshold=%.3f silence=%.1fs)",
                 self._vad_gate_desc(), self._silence_thresh, self._silence_s)

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

    def _accept_audio(self) -> bool:
        """Shared admission guard for both the base64 and binary ingest paths.

        Returns False (drop the chunk) when the stream isn't ready, the mic is
        hard-muted, or we're inside the post-TTS echo-guard window.
        """
        if not self.available or not _NUMPY_AVAILABLE:
            return False
        if self._muted:
            return False  # hard mute — iPad button unmutes
        if time.monotonic() < self._suppress_until:
            return False  # post-TTS echo guard — discard mic echo
        return True

    def _append_pcm_chunk(self, chunk: "np.ndarray") -> None:
        """Append a decoded float32 chunk to the buffer (O(1); concat deferred).

        Uses a list accumulator instead of np.concatenate per call to avoid
        O(n) reallocation on every chunk (100ms intervals, up to 30s = 480k
        samples). The list is concatenated once at transcription time.
        """
        self._buffer_chunks.append(chunk)
        if self._buffer_start_ts is None:
            self._buffer_start_ts = time.monotonic()

    def on_audio_chunk(self, samples_b64: str, frames: int) -> None:
        """Decode a base64 PCM chunk (legacy `audio_stream` text frame) and buffer it.

        Called synchronously from the asyncio event loop; must be fast.
        Auto-detects float32 vs int16 from bytes-per-frame ratio. Retained for
        backward compatibility with iPad builds that don't negotiate the binary
        audio path (see on_audio_chunk_pcm).
        """
        if not self._accept_audio():
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

            self._append_pcm_chunk(chunk)
        except Exception as exc:
            log.warning("WhisperStream.on_audio_chunk decode error: %s", exc)

    def on_audio_chunk_pcm(self, raw: bytes, frames: int) -> None:
        """Buffer a raw binary PCM chunk (new binary WebSocket frame path).

        The binary transport carries little-endian int16 PCM by contract — no
        base64, no JSON envelope — so there's no per-frame format detection. Same
        admission guards and buffer as the legacy base64 path; produces an
        identical float32 chunk so downstream segmentation is unchanged.
        """
        if not self._accept_audio():
            return
        try:
            chunk = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            self._append_pcm_chunk(chunk)
        except Exception as exc:
            log.warning("WhisperStream.on_audio_chunk_pcm decode error: %s", exc)

    # ---------------------------------------------------------------------- #
    # Background transcription loop
    # ---------------------------------------------------------------------- #

    async def _loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._poll_s)
            # Auto-expire clarification gate so silence never locks voice input forever.
            if self._awaiting_clarification and time.monotonic() >= self._clarification_deadline:
                log.info("WhisperStream: clarification timed out — re-enabling wake gate")
                self._awaiting_clarification = False
            try:
                await self._maybe_transcribe()
            except Exception as exc:
                log.error("WhisperStream loop error: %s", exc)

    def _check_approval_echo_guard(self) -> None:
        """Drop the TTS echo of the approval question when the gate first opens.

        approval_hook.py speaks "Approve …?" through the PC speakers, then writes
        the ``pending`` signal file. The iPad mic captures that spoken question;
        if transcribed it would be mistaken for the user's answer — and the
        question text itself contains the word "approve". When the gate first
        opens we flush whatever the mic captured during playback and briefly
        suppress incoming audio so only the user's actual reply is transcribed.
        Idempotent per gate: fires once on the not-open → open transition.
        """
        try:
            pending = (_APPROVAL_DIR / "pending").exists()
        except OSError:
            pending = False
        if pending and not self._approval_pending_active:
            self._approval_pending_active = True
            self.suppress(_APPROVAL_ECHO_GUARD_S)   # flush buffer + drop echo tail
            log.debug("WhisperStream: approval gate opened — TTS echo guard %.1fs",
                      _APPROVAL_ECHO_GUARD_S)
            # Push an A2UI Approve/Deny surface to the iPad in parallel with the
            # spoken question. Best-effort: a failure here never blocks the voice
            # gate, which remains the authoritative path.
            if self.on_approval_gate_open is not None:
                try:
                    prompt = (_APPROVAL_DIR / "prompt").read_text(encoding="utf-8").strip()
                except Exception:
                    prompt = ""
                try:
                    self.on_approval_gate_open(prompt or "Approve this action?")
                except Exception as exc:
                    log.debug("on_approval_gate_open callback failed: %s", exc)
        elif not pending and self._approval_pending_active:
            self._approval_pending_active = False

    def _handle_approval_gate(self, text: str) -> bool:
        """Treat ``text`` as a candidate yes/no answer when the gate is open.

        Returns True if the transcript was consumed by the gate (caller must NOT
        forward it to FusionEngine). Only a *deliberate* confirmation word is
        written to the response file — ambient speech, the TTS echo, or garbage
        is discarded and the gate keeps waiting (approval_hook.py then times out,
        failing safe to DENY). This is the core fix: random speech can no longer
        silently approve or deny a destructive tool call.
        """
        if not (_APPROVAL_DIR / "pending").exists():
            return False
        verdict = classify_confirmation(text)
        if verdict is None:
            log.info("WhisperStream: ignoring non-confirmation during approval "
                     "gate (still waiting): %r", text)
            return True  # consume — never forward ambient audio downstream
        try:
            # Write the canonical token ("approve"/"deny") so approval_hook.py
            # parses the verdict unambiguously regardless of the exact phrasing.
            (_APPROVAL_DIR / "response").write_text(verdict, encoding="utf-8")
            log.info("WhisperStream: approval response %s ← %r", verdict, text)
        except Exception as exc:
            log.warning("WhisperStream: could not write approval response: %s", exc)
        return True

    def _trailing_silence_from_chunks(self) -> bool:
        """Trailing-silence check that concatenates only the tail chunks needed.

        Walks the buffered chunks from the end until it has the last
        ``silence_s`` worth of samples (≈ a handful of ~100 ms chunks) and tests
        that window's RMS — instead of concatenating the entire (growing) buffer
        on every 150 ms poll tick. Same result as
        ``_has_trailing_silence(full_buffer, …)`` but O(silence window), not O(n).
        """
        n = int(self.SAMPLE_RATE * self._silence_s)
        if n <= 0:
            return False
        tail: list = []
        count = 0
        for c in reversed(self._buffer_chunks):
            tail.append(c)
            count += len(c)
            if count >= n:
                break
        if count < n:
            return False  # not enough audio buffered yet
        tail_audio = np.concatenate(list(reversed(tail)))
        return _rms(tail_audio[-n:]) < self._silence_thresh

    def _trailing_silence_neural(self) -> Optional[bool]:
        """Neural end-of-utterance gate over the trailing-silence window.

        Mirrors ``_trailing_silence_from_chunks``'s O(window) tail-gather, then
        hands the int16-converted tail to webrtcvad. Returns:
          - True/False  — the neural silence verdict,
          - None        — not enough audio buffered yet, or any failure,
        so the caller falls back to the RMS gate on None. Never raises into the
        background loop.
        """
        if self._neural_vad is None:
            return None
        n = int(self.SAMPLE_RATE * self._silence_s)
        if n <= 0:
            return None
        tail: list = []
        count = 0
        for c in reversed(self._buffer_chunks):
            tail.append(c)
            count += len(c)
            if count >= n:
                break
        if count < n:
            return None  # window not full yet — let RMS path return "not silent"
        try:
            tail_audio = np.concatenate(list(reversed(tail)))[-n:]
            # Float32 [-1,1] → int16 LE, matching webrtcvad's expected encoding.
            tail_int16 = (tail_audio * 32768.0).clip(-32768, 32767).astype(np.int16)
            return self._neural_vad.trailing_silence(
                tail_int16, self._silence_s, self._vad_end_ratio
            )
        except Exception as exc:
            log.debug("WhisperStream: neural VAD probe failed (%s) — RMS fallback", exc)
            return None

    async def _maybe_transcribe(self) -> None:
        # Flush the TTS echo of the approval prompt before it can be transcribed.
        self._check_approval_echo_guard()
        if not self._buffer_chunks:
            return

        # Measure duration without concatenating: summing chunk lengths is
        # O(#chunks) (~hundreds), not O(#samples) (up to 480k). Concatenation of
        # the full buffer is deferred to the moment we actually transcribe — the
        # whole point of the on_audio_chunk list accumulator. Re-concatenating
        # the growing buffer on every poll tick would be O(n²) on the event loop.
        total_samples = sum(len(c) for c in self._buffer_chunks)
        duration = total_samples / self.SAMPLE_RATE

        # Too short to be speech
        if duration < self._min_speech_s:
            return

        force = duration >= self._max_buffer_s

        if not force:
            # Streaming neural VAD drives the end-of-utterance decision; the RMS
            # gate is the fallback when the VAD is disabled/missing, the window
            # isn't full yet, or the probe errors (neural returns None then).
            silent: Optional[bool] = None
            if self._use_neural_vad and self._neural_vad is not None:
                silent = self._trailing_silence_neural()
            if silent is None:
                silent = self._trailing_silence_from_chunks()
            if not silent:
                return

        # Claim the buffer — clear before awaiting so new chunks go into a
        # fresh buffer while transcription runs on the old one. The single full
        # concatenation happens here, exactly once per utterance.
        audio = np.concatenate(self._buffer_chunks)
        self._buffer_chunks = []
        self._buffer_start_ts = None

        log.debug("WhisperStream: transcribing %.1f s of audio (force=%s)", duration, force)
        await asyncio.to_thread(self._transcribe, audio)

    def _run_whisper(self, audio: "np.ndarray", initial_prompt):
        """Run the local forward pass.

        Returns (segments_list, info) from faster-whisper so the caller's
        hallucination filter is unchanged. Raises only when no local model can
        produce a result.
        """
        seg_iter, info = self._model.transcribe(
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
        return list(seg_iter), info

    def _transcribe(self, audio: "np.ndarray") -> None:
        """Run faster-whisper and emit Command(s) to FusionEngine. Blocking."""
        if self._fusion is None:
            return
        if self._model is None:
            return

        # Build initial_prompt from static + dynamic hotwords to bias the model.
        # Static hotwords cover app names Whisper consistently misspells.
        all_hotwords = self._static_hotwords + self._hotwords
        initial_prompt = ", ".join(all_hotwords) if all_hotwords else None

        try:
            _t_transcribe = time.monotonic()
            segments, info = self._run_whisper(audio, initial_prompt)
            _whisper_latency_ms = (time.monotonic() - _t_transcribe) * 1000
            if self._metrics is not None:
                try:
                    self._metrics.record_whisper_latency(_whisper_latency_ms)
                except Exception:
                    pass
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
                if self._metrics is not None:
                    try:
                        self._metrics.inc("whisper_hallucinations")
                    except Exception:
                        pass
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

        # Feed acoustic profiler — builds per-user voice model over time.
        # Guarded: this is a non-critical adaptive write. A profiler failure must
        # never abort _transcribe before the command reaches FusionEngine, or a
        # learning bug would silently drop the user's accessibility command.
        if self._profiler is not None:
            try:
                self._profiler.record(
                    audio=audio,
                    avg_logprob=avg_logprob,
                    actual_text=text,
                    sr=self.SAMPLE_RATE,
                )
            except Exception as exc:
                log.warning("WhisperStream: acoustic profiler.record failed "
                            "(non-fatal): %s", exc)

        from core.command_executor import Command
        params: dict = {}
        if self._preserve_audio:
            # Gate 1 re-transcription: HybridCoordinator._retranscribe() uses
            # these bytes when amazon-transcribe is installed and logprob is low.
            params["audio_bytes"] = audio.astype("int16").tobytes()
            params["sample_rate"] = self.SAMPLE_RATE

        # ---------------------------------------------------------------------------
        # Approval gate intercept — check before forwarding to FusionEngine.
        # approval_hook.py (Claude Code PreToolUse) writes a "pending" signal file
        # when waiting for a yes/no from the user. Only a deliberate confirmation
        # word (yes/approve vs no/cancel) is written as the response — ambient
        # audio, the TTS echo of the question, and stray words are discarded so
        # they can never silently approve or deny a destructive tool call.
        # ---------------------------------------------------------------------------
        if self._handle_approval_gate(text):
            return  # consumed by approval gate — do NOT forward to FusionEngine

        # Calibration capture: one-shot intercept for VoiceCalibrator sessions.
        # Fires before wake-phrase gate so the user doesn't need "hey agent".
        if self._calibration_capture is not None:
            cb = self._calibration_capture
            self._calibration_capture = None  # consume — one shot only
            duration_s = len(audio) / self.SAMPLE_RATE
            log.info("WhisperStream: calibration capture: %r (logprob=%.2f)", text, avg_logprob)
            try:
                cb(text, avg_logprob, duration_s)
            except Exception as exc:
                log.warning("WhisperStream: calibration callback failed: %s", exc)
            return  # don't route to FusionEngine

        # Compose mode: while the user is building a multi-sentence prompt,
        # every utterance goes to the composer instead of FusionEngine — no
        # wake phrase required between sentences.
        # Still respect the suppress window so Danielle's own TTS echo doesn't
        # add itself to the compose buffer.
        if self._composer is not None and self._composer.is_composing():
            if time.monotonic() >= self._suppress_until:
                if self._event_loop is not None:
                    import asyncio as _asyncio
                    _fut = _asyncio.run_coroutine_threadsafe(
                        self._composer.handle_utterance(text),
                        self._event_loop,
                    )
                    _fut.add_done_callback(_log_future_exc)
            return  # always consume — never fall through to normal pipeline

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
                # Reject interrogatives / text with no command-vocab overlap: a
                # misrecognition (e.g. "are you taking us now?") must not pose as
                # the user's clarification answer.
                reason = self._bypass_reject_reason(text)
                if reason:
                    log.debug("WhisperStream: discarding clarification response "
                              "(%s): %r", reason, text)
                    return

        if not awaiting:
            import re as _re
            # Normalise: lowercase + collapse all punctuation/whitespace to
            # single spaces so "Hey, Agent, open vscode." matches "hey agent".
            normalised = _re.sub(r'[^\w\s]', ' ', text.lower())
            normalised = _re.sub(r'\s+', ' ', normalised).strip()

            matched = next(
                (p for p in sorted(self.WAKE_PHRASES, key=len, reverse=True)
                 if normalised.startswith(p)),
                None,
            )
            wake_armed = now_mono < self._wake_armed_until

            if matched is None:
                if wake_armed:
                    # Command segment arriving just after a bare "hey agent"
                    # (the VAD split the utterance). Take the whole text as the
                    # command — but reject long sentences (ambient audio leaking
                    # through the open window), mirroring the clarification gate.
                    self._wake_armed_until = 0.0
                    if len(text.split()) > 8:
                        log.debug("WhisperStream: wake-armed window — ignoring long "
                                  "utterance (%d words): %r", len(text.split()), text)
                        return
                    # Reject interrogatives / no-vocab-overlap text so a
                    # misrecognition can't ride the armed window in as a command.
                    reason = self._bypass_reject_reason(text)
                    if reason:
                        log.debug("WhisperStream: wake-armed window — ignoring "
                                  "non-command (%s): %r", reason, text)
                        return
                    log.info("WhisperStream: wake-armed command: %r", text)
                elif self._lecture_mode and self._agent_db and self._agent_db.available \
                        and self._event_loop is not None:
                    log.debug("WhisperStream: lecture mode — storing: %r", text)
                    import asyncio as _asyncio
                    _fut = _asyncio.run_coroutine_threadsafe(
                        self._agent_db.insert_ambient_transcript(
                            session_id=self._session_id,
                            text=text,
                            logprob=avg_logprob,
                            duration_s=len(audio) / self.SAMPLE_RATE,
                        ),
                        self._event_loop,
                    )
                    _fut.add_done_callback(_log_future_exc)
                    return
                else:
                    log.debug("WhisperStream: no wake phrase — discarding: %r", text)
                    return
            else:
                # Strip wake phrase words from the original text by word count
                phrase_word_count = len(matched.split())
                words = _re.split(r'[\s,\.]+', text.strip())
                command_words = [w for w in words[phrase_word_count:] if w]
                stripped = ' '.join(command_words)

                # Bare wake ("hey agent") with no command in this segment — the
                # VAD likely split the utterance. Arm a window so the NEXT
                # segment is accepted as the command without repeating the wake.
                if not stripped or not _re.search(r'[a-zA-Z]', stripped):
                    self._wake_armed_until = now_mono + self.WAKE_ARM_WINDOW
                    log.info("WhisperStream: wake armed — say the command (%.0fs window)",
                             self.WAKE_ARM_WINDOW)
                    return

                text = stripped
                self._wake_armed_until = 0.0
                log.info("WhisperStream: wake phrase detected, command: %r", text)

            # Compose-mode trigger: "claude compose" / "claude [text]" / "hey claude"
            # enters VoicePromptComposer instead of routing through HybridCoordinator.
            if self._composer is not None and self._composer.is_trigger(text):
                if self._event_loop is not None:
                    import asyncio as _asyncio
                    _fut = _asyncio.run_coroutine_threadsafe(
                        self._composer.handle_utterance(text),
                        self._event_loop,
                    )
                    _fut.add_done_callback(_log_future_exc)
                return

            # D8: correction detection — "no/wait/actually <new command>" after
            # a failed or CLARIFY outcome re-routes as a voice_correction.
            _CORRECTION_OPENERS = ("no ", "no,", "wait ", "wait,",
                                   "actually ", "actually,")
            _text_lower = text.lower()
            if (self._last_command_status in ("CLARIFY", "failed", "error")
                    and any(_text_lower.startswith(op) for op in _CORRECTION_OPENERS)):
                # Strip the correction opener word(s)
                corrected = _re.sub(r'^(no|wait|actually)[,\s]+', '', text,
                                    flags=_re.IGNORECASE).strip()
                if corrected:
                    log.info(
                        "WhisperStream: correction detected — %r → %r",
                        self._last_command_text, corrected,
                    )
                    from core.command_executor import Command as _Cmd
                    correction_cmd = _Cmd(
                        text=corrected,
                        action="DICTATE",
                        source="voice_correction",
                        whisper_logprob=avg_logprob,
                        params={
                            **params,
                            "original_text": self._last_command_text,
                            "original_status": self._last_command_status,
                        },
                    )
                    self._fusion.on_voice(correction_cmd)
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

    def _vad_gate_desc(self) -> str:
        """Human-readable label for the active end-of-utterance gate."""
        if self._use_neural_vad and self._neural_vad is not None:
            return f"neural(end_ratio={self._vad_end_ratio:.2f})"
        return "rms"

    def get_status(self) -> dict:
        buf_samples = sum(len(c) for c in self._buffer_chunks)
        buf_s = buf_samples / self.SAMPLE_RATE if buf_samples > 0 else 0.0
        return {
            "available": self.available,
            "model": self._model_size,
            "device": self._device,
            "buffer_s": round(buf_s, 2),
            "hotwords": len(self._hotwords),
            "muted": self._muted,
            "vad_gate": self._vad_gate_desc(),
        }
