"""Tests for OneEuroFilter — adaptive low-pass filter."""

import math
import pytest

from sensors.one_euro_filter import OneEuroFilter


class TestOneEuroFilterBasics:
    """Basic functionality tests."""

    def test_first_sample_passes_through(self):
        """First sample should be returned unchanged (no jump)."""
        f = OneEuroFilter()
        assert f(5.0, timestamp=0.0) == 5.0

    def test_constant_input_converges(self):
        """Constant input should converge to that value."""
        f = OneEuroFilter(min_cutoff=1.0, beta=0.007)
        dt = 1.0 / 60.0
        for i in range(200):
            result = f(3.0, timestamp=i * dt)
        assert abs(result - 3.0) < 0.001

    def test_step_response_monotonic(self):
        """Step from 0 to 1 should produce monotonically increasing output."""
        f = OneEuroFilter(min_cutoff=1.0, beta=0.007)
        dt = 1.0 / 60.0
        # Initialize at 0
        f(0.0, timestamp=0.0)
        for i in range(1, 10):
            f(0.0, timestamp=i * dt)

        # Step to 1.0
        prev = 0.0
        for i in range(10, 100):
            result = f(1.0, timestamp=i * dt)
            assert result >= prev - 1e-10, f"Non-monotonic at frame {i}: {result} < {prev}"
            prev = result

    def test_output_bounded_by_input_range(self):
        """Output should never exceed the range of inputs seen."""
        f = OneEuroFilter(min_cutoff=1.0, beta=0.5)
        dt = 1.0 / 60.0
        outputs = []
        for i in range(100):
            x = math.sin(i * 0.1) * 5.0  # range [-5, 5]
            outputs.append(f(x, timestamp=i * dt))

        assert all(-5.1 <= o <= 5.1 for o in outputs), "Output exceeded input range"

    def test_reset_clears_state(self):
        """After reset, next sample should pass through unchanged."""
        f = OneEuroFilter()
        dt = 1.0 / 60.0
        f(10.0, timestamp=0.0)
        f(10.0, timestamp=dt)
        f.reset()
        assert f(5.0, timestamp=2 * dt) == 5.0

    def test_reset_with_initial_value(self):
        """Reset with initial value seeds the filter."""
        f = OneEuroFilter()
        f(10.0, timestamp=0.0)
        f.reset(initial_value=3.0)
        # Next call should start from 3.0
        result = f(3.5, timestamp=1.0)
        assert 3.0 <= result <= 3.5

    def test_dt_zero_returns_previous(self):
        """Same timestamp should return previous output."""
        f = OneEuroFilter()
        f(1.0, timestamp=0.0)
        result = f(5.0, timestamp=0.0)
        assert result == 1.0

    def test_negative_dt_returns_previous(self):
        """Earlier timestamp should return previous output."""
        f = OneEuroFilter()
        f(1.0, timestamp=1.0)
        result = f(5.0, timestamp=0.5)
        assert result == 1.0


class TestOneEuroFilterParameterClamping:
    """Parameter validation and clamping."""

    def test_min_cutoff_clamped_low(self):
        f = OneEuroFilter(min_cutoff=0.001)
        assert f.min_cutoff == 0.1

    def test_min_cutoff_clamped_high(self):
        f = OneEuroFilter(min_cutoff=100.0)
        assert f.min_cutoff == 10.0

    def test_beta_clamped_low(self):
        f = OneEuroFilter(beta=-0.5)
        assert f.beta == 0.0

    def test_beta_clamped_high(self):
        f = OneEuroFilter(beta=5.0)
        assert f.beta == 1.0

    def test_d_cutoff_clamped_low(self):
        f = OneEuroFilter(d_cutoff=0.01)
        assert f.d_cutoff == 0.1

    def test_d_cutoff_clamped_high(self):
        f = OneEuroFilter(d_cutoff=50.0)
        assert f.d_cutoff == 10.0


class TestOneEuroFilterAdaptiveBehavior:
    """Verify adaptive cutoff behavior."""

    def test_low_speed_strong_smoothing(self):
        """At low speed, output should lag significantly behind input."""
        f = OneEuroFilter(min_cutoff=1.0, beta=0.007)
        dt = 1.0 / 60.0
        f(0.0, timestamp=0.0)

        # Very slow ramp: 0.001 per frame
        for i in range(1, 60):
            x = i * 0.001
            result = f(x, timestamp=i * dt)

        # After 1 second of slow ramp (input at 0.059), output should lag
        assert result < 0.055, f"Expected strong smoothing (lag behind input), got {result}"

    def test_high_speed_minimal_lag(self):
        """At high speed, output should track input closely."""
        f = OneEuroFilter(min_cutoff=1.0, beta=0.5)
        dt = 1.0 / 60.0
        f(0.0, timestamp=0.0)

        # Fast step: jump to 100
        results = []
        for i in range(1, 20):
            results.append(f(100.0, timestamp=i * dt))

        # After a few frames, should be close to 100
        assert results[-1] > 90.0, f"Expected fast tracking, got {results[-1]}"

    def test_initialized_property(self):
        """initialized should be False before first sample, True after."""
        f = OneEuroFilter()
        assert not f.initialized
        f(1.0, timestamp=0.0)
        assert f.initialized
        f.reset()
        assert not f.initialized
