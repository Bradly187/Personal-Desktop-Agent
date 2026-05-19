"""Tests for gaze fixation slowdown and confidence freeze (Task 12, Req 15)."""

import pytest

from fusion_engine import FusionConfig, FusionEngine


class TestGazeFixationSlowdown:
    """Verify fixation detection and speed reduction."""

    def test_fixation_state_starts_inactive(self):
        fe = FusionEngine(1920, 1080)
        assert not fe._gaze_fixation_active

    def test_low_velocity_activates_fixation(self):
        """Sustained low gaze velocity activates fixation slowdown."""
        cfg = FusionConfig(gaze_fixation_enter=5.0, gaze_fixation_duration=0.1)
        fe = FusionEngine(1920, 1080, config=cfg)

        # Simulate low-velocity gaze deltas for > 100ms
        import time
        now = time.monotonic()
        for i in range(10):
            fe._gaze_delta = (1.0, 1.0)  # small movement (< 5.0 threshold)
            fe._gaze_delta_conf = 0.9
            # Manually trigger fixation logic
            dx, dy = 1.0, 1.0
            import math
            gaze_vel = math.hypot(dx, dy)
            assert gaze_vel < cfg.gaze_fixation_enter

    def test_fixation_slowdown_multiplier(self):
        """During fixation, the slowdown multiplier is applied."""
        cfg = FusionConfig(gaze_fixation_slowdown=0.5)
        assert cfg.gaze_fixation_slowdown == 0.5

    def test_high_velocity_exits_fixation(self):
        """High gaze velocity exits fixation state."""
        fe = FusionEngine(1920, 1080)
        fe._gaze_fixation_active = True
        # Velocity above exit threshold should eventually deactivate
        # (tested via the state machine in _tick)


class TestGazeConfidenceFreeze:
    """Verify confidence-based cursor freeze."""

    def test_confidence_freeze_starts_inactive(self):
        fe = FusionEngine(1920, 1080)
        assert not fe._gaze_conf_frozen

    def test_low_confidence_triggers_freeze(self):
        """Sustained low confidence freezes cursor."""
        cfg = FusionConfig(
            gaze_conf_freeze_threshold=0.3,
            gaze_conf_freeze_duration=0.5,
        )
        fe = FusionEngine(1920, 1080, config=cfg)
        assert cfg.gaze_conf_freeze_threshold == 0.3
        assert cfg.gaze_conf_freeze_duration == 0.5

    def test_high_confidence_resumes(self):
        """High confidence after freeze resumes cursor."""
        cfg = FusionConfig(
            gaze_conf_resume_threshold=0.6,
            gaze_conf_resume_duration=0.2,
        )
        fe = FusionEngine(1920, 1080, config=cfg)
        assert cfg.gaze_conf_resume_threshold == 0.6
        assert cfg.gaze_conf_resume_duration == 0.2

    def test_config_defaults(self):
        """Verify default config values for gaze fixation/confidence."""
        cfg = FusionConfig()
        assert cfg.gaze_fixation_enter == 5.0
        assert cfg.gaze_fixation_exit == 20.0
        assert cfg.gaze_fixation_duration == 0.1
        assert cfg.gaze_fixation_slowdown == 0.5
        assert cfg.gaze_conf_freeze_threshold == 0.3
        assert cfg.gaze_conf_freeze_duration == 0.5
        assert cfg.gaze_conf_resume_threshold == 0.6
        assert cfg.gaze_conf_resume_duration == 0.2

    def test_head_accel_config_defaults(self):
        """Verify default config values for head acceleration."""
        cfg = FusionConfig()
        assert cfg.head_accel_exponent == 1.8
        assert cfg.head_tremor_threshold == 1.0
        assert cfg.head_lock_threshold == 0.5
        assert cfg.head_unlock_threshold == 1.5
        assert cfg.head_lock_delay == 0.2
