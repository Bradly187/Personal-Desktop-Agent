"""Integration tests for AcousticProfiler — per-user voice calibration (Sprint A)."""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db():
    """Return a minimal mock AgentDB that satisfies the profiler."""
    db = MagicMock()
    db.available = True
    db.get_voice_profile = AsyncMock(return_value=None)
    db.get_flare_profile = AsyncMock(return_value=None)
    db.upsert_voice_profile = AsyncMock()
    db.insert_voice_calibration = AsyncMock()
    return db


def _sine_audio(freq_hz: float = 440.0, duration_s: float = 0.5, sr: int = 16_000):
    """Generate a float32 numpy sine wave."""
    try:
        import numpy as np
        t = np.linspace(0, duration_s, int(sr * duration_s), endpoint=False)
        return (np.sin(2 * np.pi * freq_hz * t) * 0.3).astype(np.float32)
    except ImportError:
        return None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestAcousticProfilerLoad:
    def test_load_with_no_stored_profile_uses_defaults(self):
        from calibration.acoustic_profiler import AcousticProfiler, VAD_THRESHOLD_MIN, VAD_THRESHOLD_MAX
        db = _make_db()
        profiler = AcousticProfiler(agent_db=db)
        asyncio.run(profiler.load())

        assert profiler._calibrated is False
        assert VAD_THRESHOLD_MIN <= profiler._vad_threshold <= VAD_THRESHOLD_MAX

    def test_load_with_stored_profile_applies_thresholds(self):
        from calibration.acoustic_profiler import AcousticProfiler
        db = _make_db()
        db.get_voice_profile = AsyncMock(return_value={
            "baseline_rms": 0.08,
            "baseline_logprob": -0.30,
            "baseline_freq": 300.0,
            "flare_rms_scale": 0.5,
            "vad_threshold": 0.025,
            "logprob_floor": -0.80,
            "sample_count": 20,
        })
        profiler = AcousticProfiler(agent_db=db)
        asyncio.run(profiler.load())

        assert profiler._vad_threshold == pytest.approx(0.025)
        assert profiler._logprob_floor == pytest.approx(-0.80)
        assert profiler._calibrated is True   # sample_count >= 15


class TestAcousticProfilerRecord:
    def test_record_returns_voice_metrics(self):
        from calibration.acoustic_profiler import AcousticProfiler, VoiceMetrics
        audio = _sine_audio()
        if audio is None:
            pytest.skip("numpy not available")
        db = _make_db()
        profiler = AcousticProfiler(agent_db=db)
        profiler._event_loop = asyncio.new_event_loop()  # avoid run_coroutine_threadsafe error

        metrics = profiler.record(audio, avg_logprob=-0.3, actual_text="scroll down")

        assert isinstance(metrics, VoiceMetrics)
        assert metrics.rms_amplitude > 0
        assert metrics.freq_centroid > 0
        assert metrics.avg_logprob == pytest.approx(-0.3)

    def test_record_increments_sample_count(self):
        from calibration.acoustic_profiler import AcousticProfiler
        audio = _sine_audio()
        if audio is None:
            pytest.skip("numpy not available")
        db = _make_db()
        profiler = AcousticProfiler(agent_db=db)
        profiler._event_loop = asyncio.new_event_loop()

        before = profiler._sample_count
        profiler.record(audio, avg_logprob=-0.3, actual_text="click")
        assert profiler._sample_count == before + 1

    def test_record_updates_healthy_samples(self):
        from calibration.acoustic_profiler import AcousticProfiler
        audio = _sine_audio()
        if audio is None:
            pytest.skip("numpy not available")
        db = _make_db()
        profiler = AcousticProfiler(agent_db=db)
        profiler._event_loop = asyncio.new_event_loop()

        profiler.record(audio, avg_logprob=-0.3, actual_text="click", is_flare_day=False)
        assert len(profiler._healthy_samples) == 1

    def test_record_flare_day_goes_to_flare_samples(self):
        from calibration.acoustic_profiler import AcousticProfiler
        audio = _sine_audio()
        if audio is None:
            pytest.skip("numpy not available")
        db = _make_db()
        profiler = AcousticProfiler(agent_db=db)
        profiler._event_loop = asyncio.new_event_loop()

        profiler.record(audio, avg_logprob=-0.6, actual_text="click", is_flare_day=True)
        assert len(profiler._flare_samples) == 1
        assert len(profiler._healthy_samples) == 0


class TestAcousticProfilerThresholds:
    def _seeded_profiler(self, n_samples: int = 20, rms: float = 0.08, logprob: float = -0.30):
        from calibration.acoustic_profiler import AcousticProfiler, VoiceMetrics
        db = _make_db()
        profiler = AcousticProfiler(agent_db=db)
        profiler._event_loop = asyncio.new_event_loop()
        for _ in range(n_samples):
            profiler._healthy_samples.append(
                VoiceMetrics(rms_amplitude=rms, freq_centroid=300.0,
                             avg_logprob=logprob, duration_s=0.5)
            )
            profiler._sample_count += 1
        profiler._recompute()
        return profiler

    def test_vad_threshold_is_fraction_of_baseline_rms(self):
        from calibration.acoustic_profiler import AcousticProfiler, VAD_FRACTION
        profiler = self._seeded_profiler(rms=0.08)
        expected = 0.08 * VAD_FRACTION
        assert profiler._vad_threshold == pytest.approx(expected, abs=0.005)

    def test_pain_day_vad_scales_by_flare_factor(self):
        profiler = self._seeded_profiler()
        normal_vad = profiler.get_vad_threshold(pain_day=False)
        flare_vad  = profiler.get_vad_threshold(pain_day=True)
        assert flare_vad < normal_vad
        assert flare_vad == pytest.approx(normal_vad * profiler._flare_vad_scale)

    def test_logprob_floor_below_baseline(self):
        from calibration.acoustic_profiler import LOGPROB_MARGIN
        profiler = self._seeded_profiler(logprob=-0.30)
        floor = profiler.get_logprob_floor(pain_day=False)
        assert floor == pytest.approx(-0.30 + LOGPROB_MARGIN, abs=0.01)

    def test_pain_day_logprob_floor_more_relaxed(self):
        profiler = self._seeded_profiler()
        normal_floor = profiler.get_logprob_floor(pain_day=False)
        flare_floor  = profiler.get_logprob_floor(pain_day=True)
        assert flare_floor < normal_floor   # more negative = more lenient


class TestAcousticProfilerDrift:
    def test_voice_clarity_zero_when_uncalibrated(self):
        from calibration.acoustic_profiler import AcousticProfiler
        db = _make_db()
        profiler = AcousticProfiler(agent_db=db)
        assert profiler.voice_clarity_score() == 0.0

    def test_voice_clarity_nonzero_on_degradation(self):
        from calibration.acoustic_profiler import AcousticProfiler, VoiceMetrics
        db = _make_db()
        profiler = AcousticProfiler(agent_db=db)
        profiler._event_loop = asyncio.new_event_loop()
        profiler._calibrated = True

        # Healthy baseline: logprob = -0.30
        for _ in range(15):
            profiler._healthy_samples.append(
                VoiceMetrics(rms_amplitude=0.08, freq_centroid=300.0,
                             avg_logprob=-0.30, duration_s=0.5)
            )

        # Current session: degraded logprob = -0.80
        profiler._recent_logprobs = [-0.80] * 10

        clarity = profiler.voice_clarity_score()
        assert clarity > 0.0, "Degraded voice should produce nonzero clarity score"

    def test_drift_callback_fired_on_threshold(self):
        from calibration.acoustic_profiler import AcousticProfiler, VoiceMetrics, DRIFT_RECAL_THRESHOLD
        db = _make_db()
        profiler = AcousticProfiler(agent_db=db)
        profiler._event_loop = asyncio.new_event_loop()
        profiler._calibrated = True

        fired = []
        profiler.add_drift_callback(fired.append)

        # Seed healthy baseline
        for _ in range(15):
            profiler._healthy_samples.append(
                VoiceMetrics(rms_amplitude=0.08, freq_centroid=300.0,
                             avg_logprob=-0.20, duration_s=0.5)
            )

        # Force massive degradation so clarity exceeds threshold
        profiler._recent_logprobs = [-1.5] * 20

        drift = profiler._compute_drift()
        if drift and drift.needs_recalibration:
            profiler._fire_drift_callbacks(drift)

        assert len(fired) == 1
        assert fired[0].needs_recalibration is True

    def test_mark_calibrated_resets_last_calibration_ts(self):
        from calibration.acoustic_profiler import AcousticProfiler, VoiceMetrics
        db = _make_db()
        profiler = AcousticProfiler(agent_db=db)
        profiler._event_loop = asyncio.new_event_loop()
        profiler._healthy_samples = [
            VoiceMetrics(0.08, 300.0, -0.3, 0.5) for _ in range(5)
        ]

        before = profiler._last_calibration_ts
        profiler.mark_calibrated()
        assert profiler._last_calibration_ts > before
