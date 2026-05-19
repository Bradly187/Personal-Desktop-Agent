"""Tests for head acceleration curve and stationary lock (Task 11, Req 16)."""

import pytest

from fusion_engine import head_acceleration_curve, HeadStationaryLock


class TestHeadAccelerationCurve:
    """Verify 3-zone head acceleration model."""

    def test_tremor_suppression_zone(self):
        """Velocity below tremor threshold produces zero output."""
        assert head_acceleration_curve(0.0) == 0.0
        assert head_acceleration_curve(0.5) == 0.0
        assert head_acceleration_curve(0.99) == 0.0
        assert head_acceleration_curve(-0.5) == 0.0

    def test_at_threshold_produces_zero(self):
        """Exactly at tremor threshold, effective velocity is 0 → output 0."""
        # effective = |1.0| - 1.0 = 0.0, 0^1.8 = 0
        result = head_acceleration_curve(1.0)
        assert result == 0.0

    def test_just_above_threshold_produces_small_output(self):
        """Just above tremor threshold produces very small output."""
        result = head_acceleration_curve(1.1)
        assert 0.0 < result < 0.01  # very small

    def test_fine_control_zone(self):
        """3°/s should produce small cursor movement (< 50 px/s equivalent)."""
        # At 3°/s: effective = 2.0, output = 2.0^1.8 / 60 ≈ 0.058 px/tick
        # × 60 ticks = ~3.5 px/s (before sensitivity multiplier)
        result = head_acceleration_curve(3.0, exponent=1.8, tremor_threshold=1.0, tick_hz=60.0)
        px_per_sec = abs(result) * 60.0
        assert px_per_sec < 5.0  # small before sensitivity multiplier

    def test_fast_traversal_zone(self):
        """15°/s should produce large cursor movement."""
        # At 15°/s: effective = 14.0, output = 14.0^1.8 / 60
        result = head_acceleration_curve(15.0, exponent=1.8, tremor_threshold=1.0, tick_hz=60.0)
        assert result > 1.0  # significant per-tick displacement

    def test_sign_preservation(self):
        """Positive velocity → positive output, negative → negative."""
        pos = head_acceleration_curve(5.0)
        neg = head_acceleration_curve(-5.0)
        assert pos > 0
        assert neg < 0
        assert abs(pos - abs(neg)) < 1e-10  # symmetric

    def test_monotonically_increasing(self):
        """Larger velocity always produces larger output."""
        prev = 0.0
        for v in [1.5, 2.0, 3.0, 5.0, 10.0, 15.0, 20.0, 30.0]:
            result = head_acceleration_curve(v)
            assert result > prev, f"Non-monotonic at {v}: {result} <= {prev}"
            prev = result

    def test_continuity_at_threshold(self):
        """No discontinuity at tremor threshold boundary."""
        below = head_acceleration_curve(0.99)
        at = head_acceleration_curve(1.0)
        just_above = head_acceleration_curve(1.001)
        assert below == 0.0
        assert at == 0.0
        assert just_above > 0.0
        assert just_above < 0.001  # very small, continuous


class TestHeadStationaryLock:
    """Verify hysteresis-based cursor lock."""

    def test_starts_unlocked(self):
        lock = HeadStationaryLock()
        assert not lock.locked

    def test_locks_after_delay(self):
        """Cursor locks after being still for lock_delay seconds."""
        lock = HeadStationaryLock(lock_threshold=0.5, lock_delay=0.2)
        # Still for 0.3s (> 0.2s delay)
        assert not lock.update(0.3, now=0.0)
        assert not lock.update(0.3, now=0.1)
        assert lock.update(0.3, now=0.25)  # locked after 0.25s > 0.2s

    def test_does_not_lock_before_delay(self):
        """Cursor does not lock before delay elapses."""
        lock = HeadStationaryLock(lock_threshold=0.5, lock_delay=0.2)
        assert not lock.update(0.3, now=0.0)
        assert not lock.update(0.3, now=0.15)  # only 0.15s, not enough

    def test_unlocks_above_threshold(self):
        """Cursor unlocks when velocity exceeds unlock_threshold."""
        lock = HeadStationaryLock(lock_threshold=0.5, unlock_threshold=1.5, lock_delay=0.2)
        # Lock it
        lock.update(0.3, now=0.0)
        lock.update(0.3, now=0.3)
        assert lock.locked
        # Unlock with fast movement
        result = lock.update(2.0, now=0.4)
        assert not result
        assert not lock.locked

    def test_hysteresis_prevents_oscillation(self):
        """Velocity between lock and unlock thresholds doesn't toggle."""
        lock = HeadStationaryLock(lock_threshold=0.5, unlock_threshold=1.5, lock_delay=0.2)
        # Lock it
        lock.update(0.3, now=0.0)
        lock.update(0.3, now=0.3)
        assert lock.locked
        # Velocity at 1.0 (between 0.5 and 1.5) — should stay locked
        assert lock.update(1.0, now=0.4)
        assert lock.update(1.0, now=0.5)
        assert lock.locked

    def test_motion_resets_timer(self):
        """Motion above lock_threshold resets the stillness timer."""
        lock = HeadStationaryLock(lock_threshold=0.5, lock_delay=0.2)
        lock.update(0.3, now=0.0)  # start timer
        lock.update(0.3, now=0.1)  # 0.1s still
        lock.update(1.0, now=0.15)  # motion — resets timer
        lock.update(0.3, now=0.2)  # restart timer
        assert not lock.update(0.3, now=0.35)  # only 0.15s since restart
        assert lock.update(0.3, now=0.45)  # 0.25s since restart — locks

    def test_reset(self):
        """Reset clears lock state."""
        lock = HeadStationaryLock(lock_threshold=0.5, lock_delay=0.2)
        lock.update(0.3, now=0.0)
        lock.update(0.3, now=0.3)
        assert lock.locked
        lock.reset()
        assert not lock.locked
