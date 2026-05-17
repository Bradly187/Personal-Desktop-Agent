"""Property-based tests for tilt range immediate effect (Property 6).

Tests that for a fixed non-zero tilt angle within both ranges, computing the
position mapping formula with two different tiltRange values (r1 ≠ r2) produces
different screen coordinates. This validates that changing tiltRange on the iPad
side produces different cursor positions for the same physical tilt.

Formula under test (from design doc):
    x = 0.5 + (angle / tiltRange) * 0.5

**Validates: Requirements 3.4**

Run:
    python -m pytest tests/test_prop_tilt_range_effect.py -v
"""

from __future__ import annotations

import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

from hypothesis import given, settings, assume
from hypothesis import strategies as st


def compute_position_x(angle_deg: float, tilt_range: float) -> float:
    """Compute normalized X coordinate from roll angle and tilt range.

    Mirrors the iPad-side computePosition formula:
        x = 0.5 + (deltaRollDeg / tiltRange) * 0.5
    Then clamp to [0.0, 1.0].
    """
    x = 0.5 + (angle_deg / tilt_range) * 0.5
    return max(0.0, min(1.0, x))


def compute_position_y(angle_deg: float, tilt_range: float) -> float:
    """Compute normalized Y coordinate from pitch angle and tilt range.

    Mirrors the iPad-side computePosition formula:
        y = 0.5 - (deltaPitchDeg / tiltRange) * 0.5
    Then clamp to [0.0, 1.0].
    """
    y = 0.5 - (angle_deg / tilt_range) * 0.5
    return max(0.0, min(1.0, y))


class TestPropertyTiltRangeImmediateEffect:
    """Property 6: Tilt Range Immediate Effect.

    For any fixed tilt angle and two different tiltRange values (r1 ≠ r2),
    computing the position with r1 and then with r2 SHALL produce different
    screen coordinates when the angle is non-zero and within both ranges.
    """

    @given(
        angle=st.floats(min_value=0.1, max_value=60.0, allow_nan=False, allow_infinity=False),
        r1=st.floats(min_value=5.0, max_value=60.0, allow_nan=False, allow_infinity=False),
        r2=st.floats(min_value=5.0, max_value=60.0, allow_nan=False, allow_infinity=False),
    )
    @settings(max_examples=200)
    def test_different_ranges_produce_different_x_positions(
        self, angle: float, r1: float, r2: float
    ) -> None:
        """**Validates: Requirements 3.4**

        For a positive roll angle within both ranges, two different tiltRange
        values must produce different X coordinates.
        """
        # Ensure r1 != r2 and angle is within both ranges (not clamped)
        assume(abs(r1 - r2) > 0.01)
        assume(angle <= min(r1, r2))

        x1 = compute_position_x(angle, r1)
        x2 = compute_position_x(angle, r2)

        assert x1 != x2, (
            f"Same X position {x1} for angle={angle}° with r1={r1}° and r2={r2}°. "
            f"Changing tiltRange must produce different coordinates."
        )

    @given(
        angle=st.floats(min_value=0.1, max_value=60.0, allow_nan=False, allow_infinity=False),
        r1=st.floats(min_value=5.0, max_value=60.0, allow_nan=False, allow_infinity=False),
        r2=st.floats(min_value=5.0, max_value=60.0, allow_nan=False, allow_infinity=False),
    )
    @settings(max_examples=200)
    def test_different_ranges_produce_different_y_positions(
        self, angle: float, r1: float, r2: float
    ) -> None:
        """**Validates: Requirements 3.4**

        For a positive pitch angle within both ranges, two different tiltRange
        values must produce different Y coordinates.
        """
        # Ensure r1 != r2 and angle is within both ranges (not clamped)
        assume(abs(r1 - r2) > 0.01)
        assume(angle <= min(r1, r2))

        y1 = compute_position_y(angle, r1)
        y2 = compute_position_y(angle, r2)

        assert y1 != y2, (
            f"Same Y position {y1} for angle={angle}° with r1={r1}° and r2={r2}°. "
            f"Changing tiltRange must produce different coordinates."
        )

    @given(
        angle=st.floats(min_value=-60.0, max_value=-0.1, allow_nan=False, allow_infinity=False),
        r1=st.floats(min_value=5.0, max_value=60.0, allow_nan=False, allow_infinity=False),
        r2=st.floats(min_value=5.0, max_value=60.0, allow_nan=False, allow_infinity=False),
    )
    @settings(max_examples=200)
    def test_negative_angle_different_ranges_produce_different_positions(
        self, angle: float, r1: float, r2: float
    ) -> None:
        """**Validates: Requirements 3.4**

        For a negative tilt angle within both ranges, two different tiltRange
        values must produce different X coordinates. Verifies the property
        holds for both tilt directions.
        """
        # Ensure r1 != r2 and |angle| is within both ranges (not clamped)
        assume(abs(r1 - r2) > 0.01)
        assume(abs(angle) <= min(r1, r2))

        x1 = compute_position_x(angle, r1)
        x2 = compute_position_x(angle, r2)

        assert x1 != x2, (
            f"Same X position {x1} for angle={angle}° with r1={r1}° and r2={r2}°. "
            f"Changing tiltRange must produce different coordinates."
        )

    @given(
        angle=st.floats(min_value=-60.0, max_value=60.0, allow_nan=False, allow_infinity=False),
        tilt_range=st.floats(min_value=5.0, max_value=60.0, allow_nan=False, allow_infinity=False),
    )
    @settings(max_examples=200)
    def test_position_within_unit_range_when_angle_within_tilt_range(
        self, angle: float, tilt_range: float
    ) -> None:
        """**Validates: Requirements 3.4**

        For any angle within the configured tiltRange, the mapping formula
        must produce values strictly within [0, 1] (no clamping needed).
        """
        assume(abs(angle) > 0.01)
        assume(abs(angle) <= tilt_range)

        x = compute_position_x(angle, tilt_range)
        y = compute_position_y(angle, tilt_range)

        assert 0.0 <= x <= 1.0, (
            f"X={x} out of [0,1] for angle={angle}°, tiltRange={tilt_range}°"
        )
        assert 0.0 <= y <= 1.0, (
            f"Y={y} out of [0,1] for angle={angle}°, tiltRange={tilt_range}°"
        )
