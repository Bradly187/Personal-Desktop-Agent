"""Tests for GyroBiasCalibrator — continuous gyro drift correction."""

import pytest

from calibration.gyro_bias_calibrator import BiasState, GyroBiasCalibrator


class TestStateMachineTransitions:
    """Verify state machine transitions."""

    def test_starts_uncalibrated(self):
        cal = GyroBiasCalibrator()
        assert cal.state == BiasState.UNCALIBRATED

    def test_stationary_triggers_collecting(self):
        """Stationary for duration → COLLECTING."""
        cal = GyroBiasCalibrator(stationary_duration=1.0)
        dt = 1.0 / 60.0
        # Feed stationary samples for 1.1 seconds
        for i in range(70):
            cal.update(0.001, 0.001, now=i * dt)
        assert cal.state == BiasState.COLLECTING

    def test_motion_during_uncalibrated_resets_timer(self):
        """Motion resets the stationary timer."""
        cal = GyroBiasCalibrator(stationary_duration=1.0)
        dt = 1.0 / 60.0
        # 0.5s stationary
        for i in range(30):
            cal.update(0.001, 0.001, now=i * dt)
        # Motion interrupts
        cal.update(0.5, 0.5, now=30 * dt)
        # Another 0.5s stationary — not enough total
        for i in range(31, 61):
            cal.update(0.001, 0.001, now=i * dt)
        assert cal.state == BiasState.UNCALIBRATED

    def test_collecting_to_calibrated(self):
        """Collecting enough samples → CALIBRATED."""
        cal = GyroBiasCalibrator(
            stationary_duration=0.1, min_samples=10, max_samples=20
        )
        dt = 1.0 / 60.0
        # Feed enough frames to: detect stationary (0.1s = 6 frames) + collect 20 samples
        for i in range(40):
            cal.update(0.005, -0.003, now=i * dt)
        assert cal.state == BiasState.CALIBRATED

    def test_calibrated_to_frozen_on_motion(self):
        """Motion after calibration → FROZEN."""
        cal = GyroBiasCalibrator(
            stationary_duration=0.1, min_samples=10, max_samples=20
        )
        dt = 1.0 / 60.0
        # Get to calibrated (need ~26 frames: 6 for detection + 20 for collection)
        for i in range(40):
            cal.update(0.005, -0.003, now=i * dt)
        assert cal.state == BiasState.CALIBRATED
        # Motion
        cal.update(1.0, 1.0, now=40 * dt)
        assert cal.state == BiasState.FROZEN

    def test_frozen_to_collecting_on_new_stationary(self):
        """New stationary period after frozen → COLLECTING."""
        cal = GyroBiasCalibrator(
            stationary_duration=0.1, min_samples=10, max_samples=20
        )
        dt = 1.0 / 60.0
        # Get to calibrated then frozen
        for i in range(40):
            cal.update(0.005, -0.003, now=i * dt)
        assert cal.state == BiasState.CALIBRATED
        cal.update(1.0, 1.0, now=40 * dt)
        assert cal.state == BiasState.FROZEN
        # New stationary period (need 0.1s = 6+ frames)
        for i in range(41, 55):
            cal.update(0.002, 0.001, now=i * dt)
        assert cal.state == BiasState.COLLECTING


class TestBiasEstimation:
    """Verify bias computation accuracy."""

    def test_bias_equals_mean_of_samples(self):
        """Bias should be the mean of collected stationary samples."""
        cal = GyroBiasCalibrator(
            stationary_duration=0.1, min_samples=10, max_samples=50
        )
        dt = 1.0 / 60.0
        bias_x, bias_y = 0.008, -0.005
        # Feed constant bias values
        for i in range(60):
            cal.update(bias_x, bias_y, now=i * dt)

        # Should be calibrated with bias close to input
        assert cal.state == BiasState.CALIBRATED
        bx, by = cal.get_current_bias(now=100.0)  # well past lerp
        assert abs(bx - bias_x) < 0.001
        assert abs(by - bias_y) < 0.001

    def test_motion_interruption_with_enough_samples(self):
        """If motion interrupts but we have enough samples, finalize."""
        cal = GyroBiasCalibrator(
            stationary_duration=0.1, min_samples=10, max_samples=200
        )
        dt = 1.0 / 60.0
        # Get to collecting
        for i in range(10):
            cal.update(0.005, 0.005, now=i * dt)
        # Collect 15 samples (> min_samples=10)
        for i in range(10, 25):
            cal.update(0.005, 0.005, now=i * dt)
        # Motion interrupts
        cal.update(1.0, 1.0, now=25 * dt)
        # Should have finalized with the samples we had
        assert cal.state in (BiasState.CALIBRATED, BiasState.FROZEN)


class TestLerpTransition:
    """Verify smooth bias transitions."""

    def test_lerp_interpolates_over_duration(self):
        """Bias should interpolate linearly over lerp_duration."""
        cal = GyroBiasCalibrator(
            stationary_duration=0.1,
            min_samples=10,
            max_samples=20,
            lerp_duration=0.5,
        )
        dt = 1.0 / 60.0
        # First calibration with bias (0.01, -0.01) — need ~26 frames
        for i in range(40):
            cal.update(0.01, -0.01, now=i * dt)
        assert cal.state == BiasState.CALIBRATED

        # At lerp start (t=0), bias should be near 0 (starting from uncalibrated)
        lerp_start = 35 * dt
        bx_start, by_start = cal.get_current_bias(now=lerp_start)

        # At lerp midpoint
        bx_mid, by_mid = cal.get_current_bias(now=lerp_start + 0.25)

        # At lerp end
        bx_end, by_end = cal.get_current_bias(now=lerp_start + 0.6)

        # Midpoint should be between start and end
        assert bx_start <= bx_mid <= bx_end or bx_start >= bx_mid >= bx_end
        # End should be close to target
        assert abs(bx_end - 0.01) < 0.001
        assert abs(by_end - (-0.01)) < 0.001

    def test_small_bias_change_applied_immediately(self):
        """Bias change < 0.005 should apply without lerp."""
        cal = GyroBiasCalibrator(
            stationary_duration=0.1,
            min_samples=10,
            max_samples=20,
            lerp_duration=0.5,
        )
        dt = 1.0 / 60.0
        # First calibration with very small bias
        for i in range(35):
            cal.update(0.001, 0.001, now=i * dt)
        # Should be applied immediately (no lerp needed from 0 → 0.001 < 0.005)
        bx, by = cal.get_current_bias(now=35 * dt)
        assert abs(bx - 0.001) < 0.0001


class TestUncalibratedSuppression:
    """Verify suppression behavior when uncalibrated."""

    def test_suppresses_small_velocities_when_uncalibrated(self):
        """Small velocities should be suppressed before first calibration."""
        cal = GyroBiasCalibrator(uncalibrated_suppress=0.05)
        assert cal.should_suppress(0.03, 0.02) is True

    def test_does_not_suppress_large_velocities_when_uncalibrated(self):
        """Large velocities should pass through even when uncalibrated."""
        cal = GyroBiasCalibrator(uncalibrated_suppress=0.05)
        assert cal.should_suppress(0.1, 0.02) is False

    def test_does_not_suppress_when_calibrated(self):
        """After calibration, should_suppress always returns False."""
        cal = GyroBiasCalibrator(
            stationary_duration=0.1, min_samples=10, max_samples=20
        )
        dt = 1.0 / 60.0
        for i in range(40):
            cal.update(0.005, 0.005, now=i * dt)
        assert cal.state == BiasState.CALIBRATED
        assert cal.should_suppress(0.03, 0.02) is False


class TestReset:
    """Verify reset behavior."""

    def test_reset_returns_to_uncalibrated(self):
        cal = GyroBiasCalibrator(
            stationary_duration=0.1, min_samples=10, max_samples=20
        )
        dt = 1.0 / 60.0
        for i in range(40):
            cal.update(0.005, 0.005, now=i * dt)
        assert cal.state == BiasState.CALIBRATED
        cal.reset()
        assert cal.state == BiasState.UNCALIBRATED
        assert cal.bias_x == 0.0
        assert cal.bias_y == 0.0
