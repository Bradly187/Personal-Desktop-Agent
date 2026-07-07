"""Tests for dead_zone_ramp and power_curve signal processing functions."""


from core.fusion_engine import dead_zone_ramp, power_curve


class TestDeadZoneRamp:
    """Verify smoothstep dead zone ramp behavior."""

    def test_zero_below_inner(self):
        """Output is 0 when magnitude is below inner threshold."""
        assert dead_zone_ramp(0.0, 0.05, 0.125) == 0.0
        assert dead_zone_ramp(0.03, 0.05, 0.125) == 0.0
        assert dead_zone_ramp(0.05, 0.05, 0.125) == 0.0

    def test_full_above_outer(self):
        """Output equals magnitude when above outer threshold."""
        assert dead_zone_ramp(0.125, 0.05, 0.125) == 0.125
        assert dead_zone_ramp(0.5, 0.05, 0.125) == 0.5
        assert dead_zone_ramp(2.0, 0.05, 0.125) == 2.0

    def test_monotonically_increasing(self):
        """Output increases monotonically with input."""
        inner, outer = 0.05, 0.125
        prev = 0.0
        for i in range(200):
            mag = i * 0.001
            result = dead_zone_ramp(mag, inner, outer)
            assert result >= prev - 1e-10, f"Non-monotonic at {mag}: {result} < {prev}"
            prev = result

    def test_continuity_at_inner_boundary(self):
        """No discontinuity at inner threshold."""
        inner, outer = 0.05, 0.125
        # Just below and at inner
        below = dead_zone_ramp(inner - 0.0001, inner, outer)
        at = dead_zone_ramp(inner, inner, outer)
        above = dead_zone_ramp(inner + 0.0001, inner, outer)
        assert below == 0.0
        assert at == 0.0
        assert above < 0.001  # very small, near zero

    def test_continuity_at_outer_boundary(self):
        """No discontinuity at outer threshold."""
        inner, outer = 0.05, 0.125
        just_below = dead_zone_ramp(outer - 0.0001, inner, outer)
        at_outer = dead_zone_ramp(outer, inner, outer)
        # Should be very close to each other
        assert abs(at_outer - just_below) < 0.001

    def test_midpoint_value(self):
        """At midpoint of ramp, output should be ~50% of magnitude (smoothstep)."""
        inner, outer = 0.05, 0.125
        mid = (inner + outer) / 2.0  # 0.0875
        result = dead_zone_ramp(mid, inner, outer)
        # Smoothstep at t=0.5: s = 0.5² * (3 - 2*0.5) = 0.25 * 2 = 0.5
        expected = 0.5 * mid
        assert abs(result - expected) < 0.001


class TestPowerCurve:
    """Verify power curve transfer function."""

    def test_zero_input_zero_output(self):
        """Zero input always produces zero output."""
        assert power_curve(0.0, 2.0) == 0.0
        assert power_curve(0.0, 1.5, sensitivity=100.0) == 0.0

    def test_sign_preservation_positive(self):
        """Positive input produces positive output."""
        assert power_curve(1.0, 2.0) > 0
        assert power_curve(0.5, 1.5) > 0

    def test_sign_preservation_negative(self):
        """Negative input produces negative output."""
        assert power_curve(-1.0, 2.0) < 0
        assert power_curve(-0.5, 1.5) < 0

    def test_symmetry(self):
        """|f(x)| == |f(-x)| for all x."""
        for x in [0.1, 0.5, 1.0, 2.0, 5.0]:
            assert abs(power_curve(x, 2.0)) == abs(power_curve(-x, 2.0))

    def test_monotonically_increasing_positive(self):
        """For positive inputs, larger input → larger output."""
        prev = 0.0
        for i in range(1, 100):
            x = i * 0.05
            result = power_curve(x, 2.0)
            assert result > prev, f"Non-monotonic at {x}: {result} <= {prev}"
            prev = result

    def test_sensitivity_multiplier(self):
        """Sensitivity scales the output linearly."""
        base = power_curve(1.0, 2.0, sensitivity=1.0)
        scaled = power_curve(1.0, 2.0, sensitivity=200.0)
        assert abs(scaled - base * 200.0) < 1e-10

    def test_exponent_1_is_linear(self):
        """Exponent 1.0 produces linear output."""
        for x in [0.5, 1.0, 2.0]:
            result = power_curve(x, 1.0, sensitivity=1.0)
            assert abs(result - x) < 1e-10

    def test_exponent_2_is_quadratic(self):
        """Exponent 2.0 produces quadratic output."""
        result = power_curve(3.0, 2.0, sensitivity=1.0)
        assert abs(result - 9.0) < 1e-10

    def test_sub_linear_near_center_with_high_exponent(self):
        """With exponent > 1, small inputs produce even smaller outputs."""
        # Input 0.2 with exponent 2.0: output = 0.04
        result = power_curve(0.2, 2.0, sensitivity=1.0)
        assert result < 0.2  # sub-linear


class TestComposition:
    """Verify dead_zone_ramp + power_curve compose correctly."""

    def test_composition_continuity(self):
        """Ramp → power curve produces continuous output across the ramp region."""
        inner, outer = 0.05, 0.125
        exponent = 2.0
        sensitivity = 200.0

        prev_output = 0.0
        for i in range(300):
            mag = i * 0.001
            ramped = dead_zone_ramp(mag, inner, outer)
            curved = power_curve(ramped, exponent, sensitivity)
            # Output should never decrease
            assert curved >= prev_output - 1e-6, (
                f"Non-monotonic composition at mag={mag}: {curved} < {prev_output}"
            )
            prev_output = curved

    def test_composition_zero_below_dead_zone(self):
        """Below dead zone, composition output is zero."""
        inner, outer = 0.05, 0.125
        for mag in [0.0, 0.01, 0.03, 0.05]:
            ramped = dead_zone_ramp(mag, inner, outer)
            curved = power_curve(ramped, 2.0, 200.0)
            assert curved == 0.0

    def test_composition_full_above_outer(self):
        """Above outer threshold, composition equals power_curve(magnitude)."""
        inner, outer = 0.05, 0.125
        mag = 1.0
        ramped = dead_zone_ramp(mag, inner, outer)
        curved = power_curve(ramped, 2.0, 200.0)
        expected = power_curve(mag, 2.0, 200.0)
        assert abs(curved - expected) < 1e-10
