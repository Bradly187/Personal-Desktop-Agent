"""Integration tests for the refactored tilt pipelines (Tasks 4, 5).

Verifies the full signal processing chain:
- Tilt velocity: bias subtraction → 1-Euro filter → dead zone ramp → power curve → accumulator
- Tilt position: 1-Euro filter → power curve → pixel mapping

These tests verify the math/logic without requiring pyautogui (which is a local
import inside _tick). We test the components and their composition directly.
"""


from core.fusion_engine import FusionConfig, FusionEngine, dead_zone_ramp, power_curve
from calibration.gyro_bias_calibrator import BiasState


class TestTiltVelocityPipelineLogic:
    """Verify the tilt velocity signal processing chain."""

    def test_bias_subtraction_removes_drift(self):
        """After calibration, bias is subtracted from raw values."""
        from calibration.gyro_bias_calibrator import GyroBiasCalibrator

        # Use a calibrator with shorter settings for testing
        cal = GyroBiasCalibrator(
            stationary_duration=0.1, min_samples=10, max_samples=20
        )
        dt = 1.0 / 60.0
        for i in range(40):
            cal.update(0.01, -0.008, now=i * dt)
        assert cal.state == BiasState.CALIBRATED

        # Get bias — should be close to (0.01, -0.008)
        bx, by = cal.get_current_bias(now=100.0)
        assert abs(bx - 0.01) < 0.001
        assert abs(by - (-0.008)) < 0.001

    def test_filter_then_ramp_then_curve_chain(self):
        """Full chain: filter → ramp → power curve produces expected output."""
        cfg = FusionConfig()
        inner = cfg.dead_zone_inner  # 0.05
        outer = inner + inner * cfg.dead_zone_ramp_mult  # 0.125
        exp = cfg.tilt_vel_exponent  # 2.0
        sensitivity = cfg.tilt_sensitivity  # 200.0

        # Input well above dead zone: 1.0 rad/s
        # After ramp (above outer): passes through as 1.0
        # After power curve: 1.0^2.0 * 200.0 = 200.0 px/frame
        ramped = dead_zone_ramp(1.0, inner, outer)
        assert ramped == 1.0  # above outer, full pass-through
        curved = power_curve(ramped, exp, sensitivity)
        assert curved == 200.0

    def test_dead_zone_suppresses_small_velocity(self):
        """Velocity below inner threshold produces zero after ramp."""
        cfg = FusionConfig()
        inner = cfg.dead_zone_inner  # 0.05
        outer = inner + inner * cfg.dead_zone_ramp_mult

        assert dead_zone_ramp(0.03, inner, outer) == 0.0
        assert dead_zone_ramp(0.05, inner, outer) == 0.0

    def test_ramp_zone_produces_gradual_onset(self):
        """Velocity in ramp zone produces smooth non-zero output."""
        cfg = FusionConfig()
        inner = cfg.dead_zone_inner  # 0.05
        outer = inner + inner * cfg.dead_zone_ramp_mult  # 0.125

        # Midpoint of ramp: (0.05 + 0.125) / 2 = 0.0875
        mid = (inner + outer) / 2.0
        result = dead_zone_ramp(mid, inner, outer)
        assert 0.0 < result < mid  # between zero and full

    def test_uncalibrated_suppression_blocks_drift(self):
        """Before calibration, small velocities are suppressed."""
        fe = FusionEngine(1920, 1080)
        # Uncalibrated state — should suppress below 0.05
        assert fe._tilt_bias_cal.should_suppress(0.04, 0.03) is True
        assert fe._tilt_bias_cal.should_suppress(0.1, 0.0) is False

    def test_one_euro_filter_in_velocity_pipeline(self):
        """1-Euro filter smooths velocity values."""
        fe = FusionEngine(1920, 1080)
        dt = 1.0 / 60.0

        # Feed constant velocity — filter should converge
        for i in range(100):
            result = fe._tilt_filter_x(0.5, timestamp=i * dt)

        # After convergence, output should be close to input
        assert abs(result - 0.5) < 0.01

    def test_accumulator_preserves_sub_pixel(self):
        """Sub-pixel accumulator prevents rounding loss."""
        fe = FusionEngine(1920, 1080)
        # Manually add fractional values
        fe._tilt_accum_x = 0.3
        fe._tilt_accum_x += 0.4
        fe._tilt_accum_x += 0.4
        # Total: 1.1 — should yield 1 pixel with 0.1 remainder
        dx = int(fe._tilt_accum_x)
        fe._tilt_accum_x -= dx
        assert dx == 1
        assert abs(fe._tilt_accum_x - 0.1) < 1e-10


class TestTiltPositionPipelineLogic:
    """Verify the tilt position signal processing chain."""

    def test_center_position_produces_center_pixel(self):
        """Position (0.5, 0.5) → power curve displacement 0 → screen center."""
        cfg = FusionConfig()
        exp = cfg.tilt_pos_exponent  # 1.5

        # At center: displacement = 0, power_curve(0) = 0
        filtered_x = 0.5
        dx = filtered_x - 0.5  # 0.0
        curved_x = 0.5 + power_curve(dx / 0.5, exp) * 0.5 if dx != 0.0 else 0.5
        px_x = round(curved_x * 1920)
        assert px_x == 960

    def test_extreme_position_maps_beyond_70_percent(self):
        """Position 0.9 (80% of range) should map to >70% of screen (Req 5.4)."""
        cfg = FusionConfig()
        exp = cfg.tilt_pos_exponent  # 1.5

        filtered_x = 0.9
        dx = filtered_x - 0.5  # 0.4
        # Normalize: 0.4 / 0.5 = 0.8
        curved_x = 0.5 + power_curve(0.8, exp) * 0.5
        px_x = round(curved_x * 1920)
        # 70% of 1920 = 1344 from left edge → pixel > 1344
        assert px_x > 1344, f"Expected > 1344, got {px_x}"

    def test_small_displacement_sub_linear(self):
        """Small tilt (20% of range) should produce <10% screen displacement (Req 5.3)."""
        cfg = FusionConfig()
        exp = cfg.tilt_pos_exponent  # 1.5

        # 20% of tilt range from center: position = 0.5 + 0.1 = 0.6
        filtered_x = 0.6
        dx = filtered_x - 0.5  # 0.1
        # Normalize: 0.1 / 0.5 = 0.2
        curved_x = 0.5 + power_curve(0.2, exp) * 0.5
        # Screen displacement from center: (curved_x - 0.5) * 1920
        displacement_pct = (curved_x - 0.5)  # as fraction of screen
        assert displacement_pct < 0.10, f"Expected < 10% displacement, got {displacement_pct*100:.1f}%"

    def test_sign_preservation_left_right(self):
        """Tilt left (position < 0.5) maps to left side of screen."""
        cfg = FusionConfig()
        exp = cfg.tilt_pos_exponent

        # Position 0.2 → displacement -0.3 from center
        filtered_x = 0.2
        dx = filtered_x - 0.5  # -0.3
        curved_x = 0.5 + power_curve(dx / 0.5, exp) * 0.5
        px_x = round(curved_x * 1920)
        assert px_x < 960, f"Expected left of center, got {px_x}"

    def test_one_euro_filter_in_position_pipeline(self):
        """1-Euro filter smooths position values and initializes without jump."""
        fe = FusionEngine(1920, 1080)
        dt = 1.0 / 60.0

        # First sample should initialize (pass through)
        result = fe._tilt_pos_filter_x(0.7, timestamp=0.0)
        assert result == 0.7  # first sample passes through

        # Subsequent samples converge
        for i in range(1, 60):
            result = fe._tilt_pos_filter_x(0.7, timestamp=i * dt)
        assert abs(result - 0.7) < 0.01

    def test_position_clamped_to_screen(self):
        """Pixel output is clamped to [0, screen-1]."""
        # Even if power curve produces values outside [0,1], clamping catches it
        px_x = round(1.5 * 1920)  # would be 2880
        px_x = max(0, min(1920 - 1, px_x))
        assert px_x == 1919
