"""Property-based tests for FusionEngine._check_edge_scroll().

Uses Hypothesis to validate correctness properties for edge scroll behavior:
- Property 7: Edge scroll activation after delay
- Property 8: Edge scroll deactivation within one tick
- Property 9: Edge scroll speed linear interpolation
- Property 10: Corner diagonal scroll
- Property 18: Edge scroll zone boundary computation

Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.7

Run:
    pytest tests/test_prop_edge_scroll.py -v
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
from hypothesis import given, settings, assume
from hypothesis import strategies as st

sys.path.insert(0, str(Path(__file__).parent.parent))

from fusion_engine import FusionConfig, FusionEngine


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Zone percentage in valid range [0.02, 0.20]
zone_pct_st = st.floats(min_value=0.02, max_value=0.20, allow_nan=False, allow_infinity=False)

# Screen dimensions (reasonable range)
screen_dim_st = st.integers(min_value=640, max_value=7680)

# Normalized gaze coordinates [0.0, 1.0]
norm_coord_st = st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)

# Delay in ms (valid range per requirement 3.6: 200-2000, but config allows 0+)
delay_ms_st = st.integers(min_value=0, max_value=2000)

# Speed range
min_speed_st = st.integers(min_value=1, max_value=5)
max_speed_st = st.integers(min_value=6, max_value=20)


def make_engine(
    screen_w: int = 1920,
    screen_h: int = 1080,
    zone_pct: float = 0.08,
    delay_ms: int = 500,
    min_speed: int = 1,
    max_speed: int = 10,
) -> FusionEngine:
    """Create a FusionEngine with specified edge scroll config."""
    cfg = FusionConfig(
        edge_scroll_zone_pct=zone_pct,
        edge_scroll_delay_ms=delay_ms,
        edge_scroll_min_speed=min_speed,
        edge_scroll_max_speed=max_speed,
    )
    return FusionEngine(screen_w, screen_h, config=cfg)


def gaze_in_zone(zone_pct: float, direction: str) -> tuple[float, float]:
    """Generate a gaze position that is clearly inside a specific edge zone."""
    # Place gaze at the midpoint of the zone
    mid = zone_pct / 2.0
    if direction == "left":
        return (mid, 0.5)
    elif direction == "right":
        return (1.0 - mid, 0.5)
    elif direction == "up":
        return (0.5, mid)
    elif direction == "down":
        return (0.5, 1.0 - mid)
    raise ValueError(f"Unknown direction: {direction}")


# Strategy for gaze positions strictly inside an edge zone
def in_zone_st(zone_pct: float):
    """Strategy for gaze coords that are inside at least one edge zone."""
    # Generate x in left zone OR right zone, y in top zone OR bottom zone, or combinations
    left_x = st.floats(min_value=0.0, max_value=zone_pct * 0.99, allow_nan=False, allow_infinity=False)
    right_x = st.floats(min_value=1.0 - zone_pct * 0.99, max_value=1.0, allow_nan=False, allow_infinity=False)
    top_y = st.floats(min_value=0.0, max_value=zone_pct * 0.99, allow_nan=False, allow_infinity=False)
    bottom_y = st.floats(min_value=1.0 - zone_pct * 0.99, max_value=1.0, allow_nan=False, allow_infinity=False)
    safe_x = st.floats(min_value=zone_pct + 0.01, max_value=1.0 - zone_pct - 0.01, allow_nan=False, allow_infinity=False)
    safe_y = st.floats(min_value=zone_pct + 0.01, max_value=1.0 - zone_pct - 0.01, allow_nan=False, allow_infinity=False)

    # Single edge zones (not corners)
    left_zone = st.tuples(left_x, safe_y)
    right_zone = st.tuples(right_x, safe_y)
    top_zone = st.tuples(safe_x, top_y)
    bottom_zone = st.tuples(safe_x, bottom_y)

    return st.one_of(left_zone, right_zone, top_zone, bottom_zone)


def safe_area_st(zone_pct: float):
    """Strategy for gaze coords that are NOT in any edge zone."""
    safe_x = st.floats(
        min_value=zone_pct + 0.001,
        max_value=1.0 - zone_pct - 0.001,
        allow_nan=False, allow_infinity=False,
    )
    safe_y = st.floats(
        min_value=zone_pct + 0.001,
        max_value=1.0 - zone_pct - 0.001,
        allow_nan=False, allow_infinity=False,
    )
    return st.tuples(safe_x, safe_y)


# ---------------------------------------------------------------------------
# Property 7: Edge scroll activation after delay
# ---------------------------------------------------------------------------

class TestProperty7EdgeScrollActivationAfterDelay:
    """Property 7: For any gaze position within an Edge_Scroll_Zone that persists
    for longer than the configured activation delay, the FusionEngine SHALL emit
    one SCROLL Command per tick in the direction corresponding to the occupied edge zone.

    **Validates: Requirements 3.1**
    """

    @given(
        zone_pct=zone_pct_st,
        delay_ms=delay_ms_st,
    )
    @settings(max_examples=200)
    def test_activation_after_delay_emits_scroll(self, zone_pct: float, delay_ms: int):
        """After delay elapses with gaze in zone, SCROLL is emitted."""
        engine = make_engine(zone_pct=zone_pct, delay_ms=delay_ms)

        # Place gaze in the right edge zone (midpoint of zone)
        gaze_x = 1.0 - zone_pct / 2.0
        gaze_y = 0.5

        # First call: enters zone, starts timer
        engine._check_edge_scroll(gaze_x, gaze_y)

        # Simulate delay elapsed
        engine._edge_scroll_start = time.monotonic() - (delay_ms / 1000.0 + 0.01)

        # Second call: delay has elapsed, should emit
        result = engine._check_edge_scroll(gaze_x, gaze_y)

        assert len(result) >= 1, "Expected at least one SCROLL command after delay"
        assert all(cmd.action == "SCROLL" for cmd in result)
        assert any(cmd.params["direction"] == "right" for cmd in result)

    @given(
        zone_pct=zone_pct_st,
        direction=st.sampled_from(["up", "down", "left", "right"]),
    )
    @settings(max_examples=200)
    def test_scroll_direction_matches_occupied_zone(self, zone_pct: float, direction: str):
        """The emitted SCROLL direction matches the edge zone the gaze occupies."""
        engine = make_engine(zone_pct=zone_pct, delay_ms=0)

        gaze_x, gaze_y = gaze_in_zone(zone_pct, direction)

        # First call sets start time
        engine._check_edge_scroll(gaze_x, gaze_y)
        # Second call should emit (delay=0)
        result = engine._check_edge_scroll(gaze_x, gaze_y)

        assert len(result) >= 1
        directions_emitted = [cmd.params["direction"] for cmd in result]
        assert direction in directions_emitted

    @given(zone_pct=zone_pct_st)
    @settings(max_examples=100)
    def test_no_emission_before_delay(self, zone_pct: float):
        """Before delay elapses, no SCROLL commands are emitted."""
        # Use a long delay so it won't elapse during the test
        engine = make_engine(zone_pct=zone_pct, delay_ms=60000)

        gaze_x = 1.0 - zone_pct / 2.0
        gaze_y = 0.5

        result = engine._check_edge_scroll(gaze_x, gaze_y)
        assert result == []

        # Call again immediately — delay hasn't elapsed
        result = engine._check_edge_scroll(gaze_x, gaze_y)
        assert result == []


# ---------------------------------------------------------------------------
# Property 8: Edge scroll deactivation within one tick
# ---------------------------------------------------------------------------

class TestProperty8EdgeScrollDeactivation:
    """Property 8: For any active edge scroll state, when the gaze position moves
    outside all Edge_Scroll_Zones, the FusionEngine SHALL emit zero SCROLL Commands
    on the immediately following tick.

    **Validates: Requirements 3.3**
    """

    @given(
        zone_pct=zone_pct_st,
        direction=st.sampled_from(["up", "down", "left", "right"]),
    )
    @settings(max_examples=200)
    def test_leaving_zone_stops_immediately(self, zone_pct: float, direction: str):
        """Moving gaze out of all edge zones produces empty list on next call."""
        engine = make_engine(zone_pct=zone_pct, delay_ms=0)

        # Activate scrolling
        gaze_x, gaze_y = gaze_in_zone(zone_pct, direction)
        engine._check_edge_scroll(gaze_x, gaze_y)
        result = engine._check_edge_scroll(gaze_x, gaze_y)
        assert len(result) >= 1, "Scrolling should be active"

        # Move to safe area (center of screen)
        safe_x = 0.5
        safe_y = 0.5
        result = engine._check_edge_scroll(safe_x, safe_y)
        assert result == [], "Should emit zero commands after leaving zone"

    @given(
        zone_pct=zone_pct_st,
        safe_x=st.floats(min_value=0.2, max_value=0.8, allow_nan=False, allow_infinity=False),
        safe_y=st.floats(min_value=0.2, max_value=0.8, allow_nan=False, allow_infinity=False),
    )
    @settings(max_examples=200)
    def test_any_safe_position_stops_scroll(self, zone_pct: float, safe_x: float, safe_y: float):
        """Any position in the safe area (not in any zone) stops scrolling."""
        assume(safe_x > zone_pct and safe_x < 1.0 - zone_pct)
        assume(safe_y > zone_pct and safe_y < 1.0 - zone_pct)

        engine = make_engine(zone_pct=zone_pct, delay_ms=0)

        # Activate scrolling in right zone
        gaze_x = 1.0 - zone_pct / 2.0
        engine._check_edge_scroll(gaze_x, 0.5)
        result = engine._check_edge_scroll(gaze_x, 0.5)
        assert len(result) >= 1

        # Move to safe area
        result = engine._check_edge_scroll(safe_x, safe_y)
        assert result == []

    @given(zone_pct=zone_pct_st)
    @settings(max_examples=100)
    def test_deactivation_resets_state(self, zone_pct: float):
        """After deactivation, edge_scroll_active is False and start is None."""
        engine = make_engine(zone_pct=zone_pct, delay_ms=0)

        # Activate
        gaze_x = 1.0 - zone_pct / 2.0
        engine._check_edge_scroll(gaze_x, 0.5)
        engine._check_edge_scroll(gaze_x, 0.5)

        # Deactivate
        engine._check_edge_scroll(0.5, 0.5)
        assert engine._edge_scroll_active is False
        assert engine._edge_scroll_start is None
        assert engine._edge_scroll_direction is None


# ---------------------------------------------------------------------------
# Property 9: Edge scroll speed linear interpolation
# ---------------------------------------------------------------------------

class TestProperty9EdgeScrollSpeedInterpolation:
    """Property 9: For any gaze position within an Edge_Scroll_Zone, the scroll speed
    SHALL equal a linear interpolation from edge_scroll_min_speed at the inner boundary
    to edge_scroll_max_speed at the screen edge, proportional to the depth into the zone.

    speed = min_speed + depth * (max_speed - min_speed), depth in [0, 1]

    **Validates: Requirements 3.4**
    """

    @given(
        zone_pct=zone_pct_st,
        min_speed=min_speed_st,
        max_speed=max_speed_st,
        depth_frac=st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
    )
    @settings(max_examples=300)
    def test_speed_is_linear_interpolation(
        self, zone_pct: float, min_speed: int, max_speed: int, depth_frac: float
    ):
        """Speed at any depth equals min + depth * (max - min), rounded."""
        assume(max_speed > min_speed)

        engine = make_engine(
            zone_pct=zone_pct, delay_ms=0, min_speed=min_speed, max_speed=max_speed
        )

        # Place gaze in right zone at the specified depth fraction
        # Right zone: inner boundary at (1.0 - zone_pct), screen edge at 1.0
        # depth = (gaze_x - (1.0 - zone_pct)) / zone_pct
        # So gaze_x = (1.0 - zone_pct) + depth_frac * zone_pct
        gaze_x = (1.0 - zone_pct) + depth_frac * zone_pct
        # Ensure gaze_x is in the zone (> 1.0 - zone_pct)
        assume(gaze_x > 1.0 - zone_pct)
        # Clamp to valid range
        gaze_x = min(1.0, gaze_x)
        gaze_y = 0.5

        # Activate (delay=0)
        engine._check_edge_scroll(gaze_x, gaze_y)
        result = engine._check_edge_scroll(gaze_x, gaze_y)

        assert len(result) == 1
        cmd = result[0]

        # Expected speed calculation
        actual_depth = (gaze_x - (1.0 - zone_pct)) / zone_pct
        actual_depth = max(0.0, min(1.0, actual_depth))
        expected_speed = min_speed + actual_depth * (max_speed - min_speed)
        expected_clicks = max(1, round(expected_speed))

        assert cmd.params["clicks"] == expected_clicks

    @given(
        zone_pct=zone_pct_st,
        min_speed=min_speed_st,
        max_speed=max_speed_st,
    )
    @settings(max_examples=100)
    def test_speed_at_inner_boundary_is_min(self, zone_pct: float, min_speed: int, max_speed: int):
        """At the inner boundary (depth ≈ 0), speed equals min_speed."""
        assume(max_speed > min_speed)

        engine = make_engine(
            zone_pct=zone_pct, delay_ms=0, min_speed=min_speed, max_speed=max_speed
        )

        # Just barely inside the right zone
        gaze_x = (1.0 - zone_pct) + 0.001 * zone_pct
        gaze_y = 0.5

        engine._check_edge_scroll(gaze_x, gaze_y)
        result = engine._check_edge_scroll(gaze_x, gaze_y)

        assert len(result) == 1
        # depth ≈ 0.001 → speed ≈ min_speed
        assert result[0].params["clicks"] == min_speed

    @given(
        zone_pct=zone_pct_st,
        min_speed=min_speed_st,
        max_speed=max_speed_st,
    )
    @settings(max_examples=100)
    def test_speed_at_screen_edge_is_max(self, zone_pct: float, min_speed: int, max_speed: int):
        """At the screen edge (depth = 1.0), speed equals max_speed."""
        assume(max_speed > min_speed)

        engine = make_engine(
            zone_pct=zone_pct, delay_ms=0, min_speed=min_speed, max_speed=max_speed
        )

        # At the screen edge
        gaze_x = 1.0
        gaze_y = 0.5

        engine._check_edge_scroll(gaze_x, gaze_y)
        result = engine._check_edge_scroll(gaze_x, gaze_y)

        assert len(result) == 1
        assert result[0].params["clicks"] == max_speed

    @given(
        zone_pct=zone_pct_st,
        min_speed=min_speed_st,
        max_speed=max_speed_st,
    )
    @settings(max_examples=100)
    def test_speed_always_at_least_one(self, zone_pct: float, min_speed: int, max_speed: int):
        """Scroll clicks is always at least 1 regardless of depth."""
        assume(max_speed > min_speed)

        engine = make_engine(
            zone_pct=zone_pct, delay_ms=0, min_speed=min_speed, max_speed=max_speed
        )

        # Just inside the zone
        gaze_x = (1.0 - zone_pct) + 0.0001 * zone_pct
        gaze_y = 0.5

        engine._check_edge_scroll(gaze_x, gaze_y)
        result = engine._check_edge_scroll(gaze_x, gaze_y)

        if result:
            assert result[0].params["clicks"] >= 1


# ---------------------------------------------------------------------------
# Property 10: Corner diagonal scroll
# ---------------------------------------------------------------------------

class TestProperty10CornerDiagonalScroll:
    """Property 10: For any gaze position that falls within two overlapping
    Edge_Scroll_Zones (a corner region), the FusionEngine SHALL emit SCROLL
    Commands combining both corresponding directions.

    **Validates: Requirements 3.7**
    """

    @given(
        zone_pct=zone_pct_st,
        corner=st.sampled_from([
            ("top_left", "up", "left"),
            ("top_right", "up", "right"),
            ("bottom_left", "down", "left"),
            ("bottom_right", "down", "right"),
        ]),
    )
    @settings(max_examples=200)
    def test_corner_emits_two_directions(self, zone_pct: float, corner: tuple):
        """Gaze in a corner region emits exactly 2 SCROLL commands with both directions."""
        corner_name, dir_v, dir_h = corner

        engine = make_engine(zone_pct=zone_pct, delay_ms=0)

        # Place gaze in the corner (midpoint of both zones)
        mid = zone_pct / 2.0
        if "left" in corner_name:
            gaze_x = mid
        else:
            gaze_x = 1.0 - mid
        if "top" in corner_name:
            gaze_y = mid
        else:
            gaze_y = 1.0 - mid

        # Activate (delay=0)
        engine._check_edge_scroll(gaze_x, gaze_y)
        result = engine._check_edge_scroll(gaze_x, gaze_y)

        assert len(result) == 2, f"Expected 2 commands for corner {corner_name}, got {len(result)}"
        directions = {cmd.params["direction"] for cmd in result}
        assert directions == {dir_v, dir_h}, f"Expected {{{dir_v}, {dir_h}}}, got {directions}"

    @given(
        zone_pct=zone_pct_st,
        x_depth=st.floats(min_value=0.01, max_value=1.0, allow_nan=False, allow_infinity=False),
        y_depth=st.floats(min_value=0.01, max_value=1.0, allow_nan=False, allow_infinity=False),
    )
    @settings(max_examples=200)
    def test_corner_each_direction_has_independent_speed(
        self, zone_pct: float, x_depth: float, y_depth: float
    ):
        """Each direction in a corner has speed based on its own depth into the zone."""
        engine = make_engine(zone_pct=zone_pct, delay_ms=0, min_speed=1, max_speed=10)

        # Bottom-right corner with specified depths
        gaze_x = (1.0 - zone_pct) + x_depth * zone_pct
        gaze_y = (1.0 - zone_pct) + y_depth * zone_pct
        gaze_x = min(1.0, gaze_x)
        gaze_y = min(1.0, gaze_y)

        # Ensure both are in their respective zones
        assume(gaze_x > 1.0 - zone_pct)
        assume(gaze_y > 1.0 - zone_pct)

        engine._check_edge_scroll(gaze_x, gaze_y)
        result = engine._check_edge_scroll(gaze_x, gaze_y)

        assert len(result) == 2

        right_cmd = next(c for c in result if c.params["direction"] == "right")
        down_cmd = next(c for c in result if c.params["direction"] == "down")

        # Compute expected speeds independently
        actual_x_depth = max(0.0, min(1.0, (gaze_x - (1.0 - zone_pct)) / zone_pct))
        actual_y_depth = max(0.0, min(1.0, (gaze_y - (1.0 - zone_pct)) / zone_pct))

        expected_right_speed = max(1, round(1 + actual_x_depth * 9))
        expected_down_speed = max(1, round(1 + actual_y_depth * 9))

        assert right_cmd.params["clicks"] == expected_right_speed
        assert down_cmd.params["clicks"] == expected_down_speed

    @given(zone_pct=zone_pct_st)
    @settings(max_examples=100)
    def test_corner_all_commands_are_scroll(self, zone_pct: float):
        """All commands emitted from corner regions have action SCROLL and source edge_scroll."""
        engine = make_engine(zone_pct=zone_pct, delay_ms=0)

        # Top-left corner
        mid = zone_pct / 2.0
        engine._check_edge_scroll(mid, mid)
        result = engine._check_edge_scroll(mid, mid)

        assert len(result) == 2
        for cmd in result:
            assert cmd.action == "SCROLL"
            assert cmd.source == "edge_scroll"


# ---------------------------------------------------------------------------
# Property 18: Edge scroll zone boundary computation
# ---------------------------------------------------------------------------

class TestProperty18ZoneBoundaryComputation:
    """Property 18: For any configurable edge_scroll_zone_pct value in [0.02, 0.20],
    the Edge_Scroll_Zone boundaries SHALL be computed as exactly
    (edge_scroll_zone_pct × screen_dimension) pixels from each screen edge,
    applied independently to all four edges.

    **Validates: Requirements 3.2**
    """

    @given(
        zone_pct=zone_pct_st,
        screen_w=screen_dim_st,
        screen_h=screen_dim_st,
    )
    @settings(max_examples=200)
    def test_gaze_at_boundary_is_not_in_zone(self, zone_pct: float, screen_w: int, screen_h: int):
        """Gaze at exactly the inner boundary (zone_pct from edge) is NOT in the zone.

        The zone is defined as gaze < zone_pct (for left/top) or gaze > 1-zone_pct (for right/bottom).
        At exactly zone_pct, the gaze is at the boundary and should NOT trigger scrolling.
        """
        engine = make_engine(screen_w=screen_w, screen_h=screen_h, zone_pct=zone_pct, delay_ms=0)

        # At exactly the left zone boundary (gaze_x == zone_pct)
        # This is NOT in the zone (zone is gaze_x < zone_pct)
        result = engine._check_edge_scroll(zone_pct, 0.5)
        assert result == []

        # At exactly the right zone boundary (gaze_x == 1.0 - zone_pct)
        # This is NOT in the zone (zone is gaze_x > 1.0 - zone_pct)
        result = engine._check_edge_scroll(1.0 - zone_pct, 0.5)
        assert result == []

    @given(
        zone_pct=zone_pct_st,
        screen_w=screen_dim_st,
        screen_h=screen_dim_st,
    )
    @settings(max_examples=200)
    def test_gaze_just_inside_boundary_is_in_zone(
        self, zone_pct: float, screen_w: int, screen_h: int
    ):
        """Gaze just inside the boundary triggers zone detection."""
        engine = make_engine(screen_w=screen_w, screen_h=screen_h, zone_pct=zone_pct, delay_ms=0)

        # Just inside left zone
        epsilon = zone_pct * 0.01
        gaze_x = zone_pct - epsilon

        engine._check_edge_scroll(gaze_x, 0.5)
        result = engine._check_edge_scroll(gaze_x, 0.5)

        assert len(result) >= 1
        assert any(cmd.params["direction"] == "left" for cmd in result)

    @given(
        zone_pct=zone_pct_st,
        screen_w=screen_dim_st,
        screen_h=screen_dim_st,
    )
    @settings(max_examples=200)
    def test_zone_boundaries_independent_per_edge(
        self, zone_pct: float, screen_w: int, screen_h: int
    ):
        """Zone boundaries are applied independently to all four edges using the same zone_pct."""
        engine = make_engine(screen_w=screen_w, screen_h=screen_h, zone_pct=zone_pct, delay_ms=0)

        mid = zone_pct / 2.0  # midpoint of zone

        # Test all four edges independently
        for gaze_x, gaze_y, expected_dir in [
            (mid, 0.5, "left"),
            (1.0 - mid, 0.5, "right"),
            (0.5, mid, "up"),
            (0.5, 1.0 - mid, "down"),
        ]:
            # Reset engine state between tests
            engine._edge_scroll_active = False
            engine._edge_scroll_start = None
            engine._edge_scroll_direction = None

            engine._check_edge_scroll(gaze_x, gaze_y)
            result = engine._check_edge_scroll(gaze_x, gaze_y)

            assert len(result) >= 1, f"Expected scroll for {expected_dir} zone"
            assert any(
                cmd.params["direction"] == expected_dir for cmd in result
            ), f"Expected direction {expected_dir}"

    @given(
        zone_pct=zone_pct_st,
        screen_w=screen_dim_st,
        screen_h=screen_dim_st,
    )
    @settings(max_examples=100)
    def test_zone_pct_determines_boundary_position(
        self, zone_pct: float, screen_w: int, screen_h: int
    ):
        """The zone boundary in pixels equals zone_pct * screen_dimension from each edge.

        Verifies that the normalized boundary is exactly at zone_pct from each edge.
        """
        engine = make_engine(screen_w=screen_w, screen_h=screen_h, zone_pct=zone_pct, delay_ms=0)

        # The left zone boundary in pixels should be zone_pct * screen_w
        left_boundary_px = zone_pct * screen_w
        right_boundary_px = (1.0 - zone_pct) * screen_w
        top_boundary_px = zone_pct * screen_h
        bottom_boundary_px = (1.0 - zone_pct) * screen_h

        # Verify by checking that gaze at boundary-1px (normalized) IS in zone
        # and gaze at boundary+1px (normalized) is NOT in zone
        # Left zone: boundary is at zone_pct in normalized coords
        just_inside = (zone_pct - 1.0 / screen_w)
        just_outside = (zone_pct + 1.0 / screen_w)

        # Ensure values are valid
        assume(just_inside > 0.0)
        assume(just_outside < 1.0 - zone_pct)

        # Just inside left zone should detect zone
        engine._edge_scroll_active = False
        engine._edge_scroll_start = None
        engine._edge_scroll_direction = None
        engine._check_edge_scroll(just_inside, 0.5)
        # Check that direction was set to left
        assert engine._edge_scroll_direction is not None
        assert "left" in engine._edge_scroll_direction

        # Just outside left zone should NOT detect zone
        engine._edge_scroll_active = False
        engine._edge_scroll_start = None
        engine._edge_scroll_direction = None
        engine._check_edge_scroll(just_outside, 0.5)
        assert engine._edge_scroll_direction is None
