"""calibration.acoustic_profiler.py — Per-user voice acoustic profiling for the Desktop Agent.

Addresses the critical gap for disabled users (RA, etc.) whose voice
amplitude, clarity, and pronunciation vary significantly between healthy days
and flare days.  The profiler:

  1. Measures per-utterance RMS amplitude, spectral centroid, and Whisper
     confidence during normal operation and explicit calibration sessions.
  2. Derives per-user VAD silence_threshold and Gate 1 logprob_floor from
     the measured baseline, replacing the static global defaults.
  3. Scales thresholds further on flare days using the user's flare_vad_scale
     factor (captured during onboarding — default 0.5 = voice drops to 50%
     of baseline volume during flares).
  4. Exposes voice_clarity_score() for BehavioralTwinState's PainDayEngine
     as a 5th pain signal.

Usage (wired by main.py):
    profiler = AcousticProfiler(agent_db=agent_db, session_id=session_id)
    await profiler.load()            # loads stored profile from AgentDB
    whisper.set_acoustic_profiler(profiler)

On each transcription WhisperStream calls:
    profiler.record(audio, avg_logprob, actual_text, phrase, is_flare_day)

ThresholdS are recomputed automatically after every RECOMPUTE_EVERY samples.
"""

from __future__ import annotations

import asyncio
import logging
import math
import statistics
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from storage.db import AgentDB

log = logging.getLogger(__name__)

try:
    import numpy as np
    _NUMPY_OK = True
except ImportError:
    _NUMPY_OK = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# How many new samples before we recompute derived thresholds
RECOMPUTE_EVERY = 10

# Fraction of baseline RMS used as silence threshold.
# e.g. baseline=0.08 → threshold=0.08 * 0.35 = 0.028
VAD_FRACTION = 0.35

# Minimum / maximum bounds for the computed VAD threshold
VAD_THRESHOLD_MIN = 0.020
VAD_THRESHOLD_MAX = 0.06

# Gate 1 logprob floor: add this to the median baseline logprob.
# e.g. baseline_logprob=-0.30 → floor=-0.30 + (-0.50) = -0.80
LOGPROB_MARGIN = -0.50
LOGPROB_FLOOR_MIN = -1.5
LOGPROB_FLOOR_MAX = -0.3

# Minimum healthy-day samples before profile is considered calibrated
MIN_CALIBRATED_SAMPLES = 15

# Sprint C — continuous re-calibration
DRIFT_CHECK_EVERY_VOICE = 20    # voice samples between drift checks
DRIFT_RECAL_THRESHOLD   = 0.30  # 30% voice clarity drop triggers re-cal request
SEASONAL_DAYS           = 30    # days before prompting a full re-calibration


@dataclass
class DriftResult:
    needs_recalibration: bool
    degradation_pct: float        # 0–100 (percentage of baseline clarity lost)
    reason: str                   # "voice_clarity" | "seasonal" | "ok"
    voice_samples: int
    total_commands: int


# ---------------------------------------------------------------------------
# Voice phrase bank
# ---------------------------------------------------------------------------

DEFAULT_PHRASES: list[dict] = [
    # Verb coverage
    {"phrase": "click",          "category": "verb",       "phonetic": "click"},
    {"phrase": "scroll down",    "category": "verb",       "phonetic": "scroll down"},
    {"phrase": "scroll up",      "category": "verb",       "phonetic": "scroll up"},
    {"phrase": "screenshot",     "category": "verb",       "phonetic": "screenshot"},
    {"phrase": "close",          "category": "verb",       "phonetic": "close"},
    {"phrase": "open kiro",      "category": "app",        "phonetic": "open kiro"},
    {"phrase": "open chrome",    "category": "app",        "phonetic": "open chrome"},
    {"phrase": "open terminal",  "category": "app",        "phonetic": "open terminal"},
    # Wake phrase
    {"phrase": "hey agent",      "category": "wake",       "phonetic": "hey agent"},
    # Navigation
    {"phrase": "type hello",     "category": "navigation", "phonetic": "type hello"},
    {"phrase": "hotkey control c", "category": "navigation", "phonetic": "hotkey control c"},
]


# ---------------------------------------------------------------------------
# VoiceMetrics
# ---------------------------------------------------------------------------

@dataclass
class VoiceMetrics:
    rms_amplitude: float       # 0.0–1.0 (root mean square of voiced frames)
    freq_centroid: float       # Hz — spectral centre of mass
    avg_logprob: float         # Whisper segment avg log-probability
    duration_s: float          # seconds of voiced audio
    ts: float = field(default_factory=time.time)


def _compute_metrics(audio: "np.ndarray", sr: int, avg_logprob: float) -> VoiceMetrics:
    """Extract acoustic features from a float32 audio array."""
    if not _NUMPY_OK:
        return VoiceMetrics(rms_amplitude=0.0, freq_centroid=0.0,
                            avg_logprob=avg_logprob,
                            duration_s=len(audio) / max(sr, 1))

    # RMS amplitude
    rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))

    # Spectral centroid (magnitude-weighted mean frequency)
    n_fft = min(2048, len(audio))
    if n_fft >= 64:
        spectrum = np.abs(np.fft.rfft(audio[:n_fft]))
        freqs = np.fft.rfftfreq(n_fft, d=1.0 / sr)
        denom = spectrum.sum()
        freq_centroid = float((freqs * spectrum).sum() / denom) if denom > 0 else 0.0
    else:
        freq_centroid = 0.0

    duration_s = len(audio) / max(sr, 1)
    return VoiceMetrics(rms_amplitude=rms, freq_centroid=freq_centroid,
                        avg_logprob=avg_logprob, duration_s=duration_s)


# ---------------------------------------------------------------------------
# AcousticProfiler
# ---------------------------------------------------------------------------

class AcousticProfiler:
    """Maintains a per-user voice profile and derives adaptive thresholds."""

    def __init__(self, agent_db: "AgentDB", session_id: int = -1) -> None:
        self._db = agent_db
        self._session_id = session_id

        # In-memory ring buffers (last 200 samples each)
        self._healthy_samples: list[VoiceMetrics] = []
        self._flare_samples:   list[VoiceMetrics] = []
        self._MAX_RING = 200

        # Derived thresholds — start with safe defaults, overridden on load().
        # Default sits at the documented floor (VAD_THRESHOLD_MIN) so the
        # uncalibrated value always satisfies the MIN ≤ vad ≤ MAX invariant.
        self._vad_threshold:   float = VAD_THRESHOLD_MIN
        self._logprob_floor:   float = -1.0
        self._flare_vad_scale: float = 0.5

        # Voice clarity for PainDayEngine (rolling window)
        self._recent_logprobs: list[float] = []
        self._CLARITY_WINDOW = 20

        # Count new samples since last recompute
        self._since_recompute: int = 0
        self._calibrated: bool = False
        self._sample_count: int = 0

        self._event_loop: Optional[asyncio.AbstractEventLoop] = None
        self._active_condition: str = "good_day"
        self._condition_corrections: dict = {}

        # Sprint C — continuous re-calibration
        self._total_commands: int = 0
        self._last_calibration_ts: float = 0.0   # unix time of last cal
        self._drift_callbacks: list = []          # callable(DriftResult)

        # D6: provisional VAD relaxation on drift (before recalibration completes)
        self._whisper_ref = None          # set via set_whisper_ref()
        self._pre_drift_vad: float = 0.0  # saved threshold to restore after recal

    # ── Lifecycle ──────────────────────────────────────────────────────────

    # ── Drift callback registration ────────────────────────────────────────

    def add_drift_callback(self, cb) -> None:
        """Register a callable(DriftResult) fired when re-calibration is needed.

        Called from the worker thread that runs _transcribe(); callbacks must
        be thread-safe or schedule via run_coroutine_threadsafe themselves.
        """
        self._drift_callbacks.append(cb)

    def set_whisper_ref(self, whisper) -> None:
        """Wire WhisperStream so VAD threshold changes propagate immediately."""
        self._whisper_ref = whisper

    def apply_provisional_vad_relaxation(self, factor: float = 0.7) -> None:
        """Immediately loosen VAD threshold by `factor` when drift is detected.

        The pre-drift value is saved so restore_vad_from_profile() can undo
        the provisional change after the user completes recalibration.
        """
        self._pre_drift_vad = self._vad_threshold
        self._vad_threshold = max(VAD_THRESHOLD_MIN, self._vad_threshold * factor)
        log.info(
            "AcousticProfiler: provisional VAD relaxation %.3f → %.3f (drift detected)",
            self._pre_drift_vad, self._vad_threshold,
        )
        self._push_vad_to_whisper()

    def restore_vad_from_profile(self) -> None:
        """Re-derive thresholds from the updated healthy samples after recalibration.

        Called after a guided voice calibration session completes so the
        corrected profile replaces both the provisional value and the old baseline.
        """
        self._recompute()
        log.info(
            "AcousticProfiler: VAD restored to %.3f after recalibration",
            self._vad_threshold,
        )
        self._push_vad_to_whisper()

    def _push_vad_to_whisper(self) -> None:
        """Push current VAD threshold to the wired WhisperStream immediately."""
        if self._whisper_ref is not None:
            try:
                self._whisper_ref._silence_thresh = self._vad_threshold
                log.debug(
                    "AcousticProfiler: pushed VAD %.3f to WhisperStream",
                    self._vad_threshold,
                )
            except Exception as exc:
                log.debug("AcousticProfiler._push_vad_to_whisper failed: %s", exc)

    def mark_calibrated(self) -> None:
        """Call after a calibration session to reset the drift baseline."""
        self._last_calibration_ts = time.time()
        # Promote recent healthy-day samples as the new baseline
        self._healthy_samples = list(self._healthy_samples[-40:])
        # D6: re-derive thresholds from updated samples and push to WhisperStream
        self.restore_vad_from_profile()
        log.info("AcousticProfiler: marked as freshly calibrated")
        if self._db.available and self._event_loop:
            fut = asyncio.run_coroutine_threadsafe(
                self._db.upsert_voice_profile({
                    "baseline_rms":     statistics.median([m.rms_amplitude for m in self._healthy_samples] or [0]),
                    "baseline_logprob": statistics.median([m.avg_logprob for m in self._healthy_samples] or [-1]),
                    "baseline_freq":    statistics.median([m.freq_centroid for m in self._healthy_samples] or [0]),
                    "flare_rms_scale":  self._flare_vad_scale,
                    "vad_threshold":    self._vad_threshold,
                    "logprob_floor":    self._logprob_floor,
                    "sample_count":     self._sample_count,
                }),
                self._event_loop,
            )
            fut.add_done_callback(
                lambda f: log.error("AcousticProfiler: upsert_voice_profile failed: %s", f.exception())
                if f.exception() else None
            )

    async def load_rom_bounds(self, db) -> None:
        """D4: Apply voice range-of-motion from onboarding as initial VAD bounds.

        Used only when the profiler has fewer than MIN_CALIBRATED_SAMPLES, so
        the user has a reasonable starting threshold even before passive
        calibration accumulates enough data.
        """
        if self._calibrated:
            return  # already have learned data — don't override
        try:
            rom = await db.get_sensor_rom("voice")
            rms_row = rom.get("rms")
            if rms_row:
                comfortable_rms = rms_row.get("comfortable_value")
                if comfortable_rms and comfortable_rms > 0:
                    derived_vad = max(
                        VAD_THRESHOLD_MIN,
                        min(VAD_THRESHOLD_MAX, comfortable_rms * VAD_FRACTION),
                    )
                    self._vad_threshold = derived_vad
                    log.info(
                        "AcousticProfiler: ROM voice VAD set to %.3f from onboarding",
                        derived_vad,
                    )
        except Exception as exc:
            log.debug("AcousticProfiler.load_rom_bounds failed (non-fatal): %s", exc)

    async def load(self, condition: str | None = None) -> None:
        """Load stored profile from AgentDB and apply thresholds.

        If condition is provided (good_day/flare_day/allergy_day),
        loads that condition's calibrated corrections and threshold overrides
        from voice_profiles table in addition to the baseline acoustic profile.
        """
        try:
            self._event_loop = asyncio.get_running_loop()
        except RuntimeError:
            pass

        profile = await self._db.get_voice_profile()
        if profile:
            self._sample_count = profile.get("sample_count", 0)
            self._flare_vad_scale = profile.get("flare_rms_scale", 0.5)
            if profile.get("vad_threshold"):
                self._vad_threshold = profile["vad_threshold"]
            if profile.get("logprob_floor"):
                self._logprob_floor = profile["logprob_floor"]
            self._calibrated = self._sample_count >= MIN_CALIBRATED_SAMPLES
            log.info(
                "AcousticProfiler: loaded profile — vad=%.3f logprob_floor=%.2f "
                "samples=%d calibrated=%s",
                self._vad_threshold, self._logprob_floor,
                self._sample_count, self._calibrated,
            )
        else:
            log.info("AcousticProfiler: no stored profile — using defaults")

        # Also load flare profile for vad_scale
        flare = await self._db.get_flare_profile()
        if flare and flare.get("flare_vad_scale"):
            self._flare_vad_scale = flare["flare_vad_scale"]

        # Load condition-specific calibrated overrides if requested
        if condition:
            self._active_condition = condition
            cond_profile = await self._db.load_voice_profile(condition)
            if cond_profile:
                if cond_profile.get("vad_threshold"):
                    self._vad_threshold = cond_profile["vad_threshold"]
                if cond_profile.get("logprob_floor"):
                    self._logprob_floor = cond_profile["logprob_floor"]
                self._condition_corrections = cond_profile.get("corrections", {})
                log.info(
                    "AcousticProfiler: condition %r applied — vad=%.3f logprob=%.2f "
                    "corrections=%d",
                    condition, self._vad_threshold, self._logprob_floor,
                    len(self._condition_corrections),
                )
            else:
                log.info(
                    "AcousticProfiler: no calibration for %r — using acoustic defaults",
                    condition,
                )

    async def apply_to(self, whisper_stream, coordinator=None) -> None:
        """Push the active profile to WhisperStream and optionally the coordinator."""
        whisper_stream.set_acoustic_profiler(self)
        corrections = getattr(self, "_condition_corrections", {})
        if coordinator and corrections:
            coordinator.add_personal_corrections(corrections)
            log.info(
                "AcousticProfiler: applied %d personal corrections to coordinator",
                len(corrections),
            )

    # ── Recording ──────────────────────────────────────────────────────────

    def record(
        self,
        audio: "np.ndarray",
        avg_logprob: float,
        actual_text: str,
        sr: int = 16_000,
        phrase: str = "",
        is_flare_day: bool = False,
    ) -> VoiceMetrics:
        """Measure and store acoustic metrics from a completed utterance.

        Called from WhisperStream._transcribe() (worker thread).
        DB writes are scheduled thread-safely via the captured event loop.
        """
        metrics = _compute_metrics(audio, sr, avg_logprob)
        # Expose most-recent RMS so FusionEngine telemetry sampler can read it
        self._last_rms: float = metrics.rms_amplitude

        # Update ring buffers
        ring = self._flare_samples if is_flare_day else self._healthy_samples
        ring.append(metrics)
        if len(ring) > self._MAX_RING:
            ring.pop(0)

        # Update rolling clarity window
        self._recent_logprobs.append(avg_logprob)
        if len(self._recent_logprobs) > self._CLARITY_WINDOW:
            self._recent_logprobs.pop(0)

        self._sample_count += 1
        self._since_recompute += 1

        # Recompute thresholds periodically
        if self._since_recompute >= RECOMPUTE_EVERY and not is_flare_day:
            self._recompute()
            self._since_recompute = 0

        # Voice drift check every N voice samples
        if (self._calibrated
                and not is_flare_day
                and self._sample_count % DRIFT_CHECK_EVERY_VOICE == 0):
            drift = self._compute_drift()
            if drift and drift.needs_recalibration:
                self._fire_drift_callbacks(drift)

        # Persist to AgentDB (thread-safe)
        if self._db.available and self._event_loop:
            asyncio.run_coroutine_threadsafe(
                self._db.insert_voice_calibration(
                    session_id=self._session_id,
                    phrase=phrase,
                    actual_text=actual_text,
                    rms_amplitude=metrics.rms_amplitude,
                    freq_centroid=metrics.freq_centroid,
                    avg_logprob=metrics.avg_logprob,
                    duration_s=metrics.duration_s,
                    is_flare_day=is_flare_day,
                ),
                self._event_loop,
            )

        return metrics

    # ── Threshold computation ──────────────────────────────────────────────

    def on_any_command(self) -> None:
        """Call after every executed command (voice or not) for seasonal check."""
        self._total_commands += 1
        if self._total_commands % 50 != 0:
            return
        # Seasonal: if calibrated >SEASONAL_DAYS ago, request a refresh
        if self._last_calibration_ts > 0:
            days = (time.time() - self._last_calibration_ts) / 86400
            if days >= SEASONAL_DAYS:
                drift = DriftResult(
                    needs_recalibration=True,
                    degradation_pct=0.0,
                    reason="seasonal",
                    voice_samples=self._sample_count,
                    total_commands=self._total_commands,
                )
                log.info(
                    "AcousticProfiler: seasonal re-cal prompt (%.0f days since last cal)",
                    days,
                )
                self._fire_drift_callbacks(drift)

    def _compute_drift(self) -> "DriftResult | None":
        """Check voice clarity against baseline; return DriftResult."""
        if not self._recent_logprobs or len(self._healthy_samples) < 5:
            return None
        clarity = self.voice_clarity_score()
        degradation_pct = clarity * 100.0
        needs = clarity >= DRIFT_RECAL_THRESHOLD
        if needs:
            log.info(
                "AcousticProfiler: voice drift detected — %.0f%% degradation "
                "over last %d samples",
                degradation_pct, len(self._recent_logprobs),
            )
        return DriftResult(
            needs_recalibration=needs,
            degradation_pct=round(degradation_pct, 1),
            reason="voice_clarity" if needs else "ok",
            voice_samples=self._sample_count,
            total_commands=self._total_commands,
        )

    def _fire_drift_callbacks(self, drift: DriftResult) -> None:
        for cb in self._drift_callbacks:
            try:
                cb(drift)
            except Exception as exc:
                log.warning("AcousticProfiler drift callback error: %s", exc)

    def _recompute(self) -> None:
        """Derive per-user VAD threshold and logprob floor from healthy samples."""
        if len(self._healthy_samples) < 5:
            return

        rms_values   = [m.rms_amplitude for m in self._healthy_samples if m.rms_amplitude > 0]
        logprob_vals = [m.avg_logprob   for m in self._healthy_samples if m.avg_logprob < 0]

        if not rms_values or not logprob_vals:
            return

        baseline_rms    = statistics.median(rms_values)
        baseline_logprob = statistics.median(logprob_vals)
        baseline_freq   = statistics.median([m.freq_centroid for m in self._healthy_samples])

        # VAD threshold = fraction of baseline RMS (so silence is clearly below voice)
        vad = max(VAD_THRESHOLD_MIN, min(VAD_THRESHOLD_MAX, baseline_rms * VAD_FRACTION))
        # Gate 1 logprob floor = baseline + margin (allows some degradation)
        floor = max(LOGPROB_FLOOR_MIN, min(LOGPROB_FLOOR_MAX, baseline_logprob + LOGPROB_MARGIN))

        self._vad_threshold = vad
        self._logprob_floor = floor
        self._calibrated = self._sample_count >= MIN_CALIBRATED_SAMPLES

        log.info(
            "AcousticProfiler: recomputed — vad=%.3f logprob_floor=%.2f "
            "baseline_rms=%.3f baseline_logprob=%.2f samples=%d",
            vad, floor, baseline_rms, baseline_logprob, self._sample_count,
        )

        # Persist
        if self._db.available and self._event_loop:
            asyncio.run_coroutine_threadsafe(
                self._db.upsert_voice_profile({
                    "baseline_rms":    baseline_rms,
                    "baseline_logprob": baseline_logprob,
                    "baseline_freq":   baseline_freq,
                    "flare_rms_scale": self._flare_vad_scale,
                    "vad_threshold":   vad,
                    "logprob_floor":   floor,
                    "sample_count":    self._sample_count,
                }),
                self._event_loop,
            )

    # ── Threshold accessors ────────────────────────────────────────────────

    def get_vad_threshold(self, pain_day: bool = False) -> float:
        """Return the per-user silence threshold for WhisperStream.

        On pain days, the threshold is scaled down by flare_vad_scale so
        quieter voice still registers as speech (not silence).
        """
        if pain_day:
            return self._vad_threshold * self._flare_vad_scale
        return self._vad_threshold

    def get_logprob_floor(self, pain_day: bool = False) -> float:
        """Return the per-user Gate 1 logprob floor for HybridCoordinator."""
        if pain_day:
            # Relax further on flare days (voice degrades → lower confidence)
            return self._logprob_floor - 0.20
        return self._logprob_floor

    # ── Pain day voice signal ──────────────────────────────────────────────

    def voice_clarity_score(self) -> float:
        """Return [0.0, 1.0] degradation signal for PainDayEngine.

        0.0 = voice quality matches baseline (no pain signal)
        1.0 = voice confidence has collapsed (strong flare signal)

        Computed as: (baseline_logprob - current_median) / |baseline_logprob|
        Clamped to [0.0, 1.0].
        """
        if not self._recent_logprobs or not self._calibrated:
            return 0.0

        # We need healthy baseline from stored samples
        healthy = [m.avg_logprob for m in self._healthy_samples]
        if not healthy:
            return 0.0

        baseline = statistics.median(healthy)
        current  = statistics.median(self._recent_logprobs)

        if baseline >= 0:
            return 0.0

        degradation = (baseline - current) / abs(baseline)
        return max(0.0, min(1.0, degradation))

    # ── Status ─────────────────────────────────────────────────────────────

    def get_status(self) -> dict:
        return {
            "calibrated":       self._calibrated,
            "sample_count":     self._sample_count,
            "vad_threshold":    round(self._vad_threshold, 4),
            "logprob_floor":    round(self._logprob_floor, 3),
            "flare_vad_scale":  self._flare_vad_scale,
            "voice_clarity":    round(self.voice_clarity_score(), 3),
            "healthy_samples":  len(self._healthy_samples),
            "flare_samples":    len(self._flare_samples),
        }
