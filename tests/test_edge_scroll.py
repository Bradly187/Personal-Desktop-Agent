"""Unit tests for FusionEngine._check_edge_scroll().

Tests the edge scroll detection logic:
- Gaze in edge zone starts activation delay timer
- After delay elapsed, emits SCROLL Command with correct direction
- Gaze leaving zone stops scrolling immediately (within one tick)
- Direction changes reset the delay timer
- Speed scales linearly with depth into zone
- Corner regions emit two SCROLL Commands (diagonal scroll)

Run:
    pytest tests/test_edge_scroll.py -v
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from fusion_engine import FusionConfig, FusionEngine


@pytest.fixture
def engine():
    """Create a FusionEngine with default config (1920x1080)."""
    return FusionEngine(1920, 1080)


@pytest.fixture
def engine_fast_delay():
    """Create a FusionEngine with a very short activation delay for testing."""
    cfg = FusionConfig(edge_scroll_delay_ms=0)
    return FusionEngine(1920, 1080, config=cfg)


class TestEdgeScrollZoneDetection:
    """Tests for edge zone boundary detection."""

    def test_gaze_in_center_returns_empty(self, engine):
        """Gaze in center of screen produces no scroll command."""
        result = engine._check_edge_scroll(0.5, 0.5)
        assert result == []

    def test_gaze_in_safe_area_returns_empty(self, engine):
        """Gaze just inside the safe area (not in any edge zone) returns empty list."""
        # Default zone is 8%, so 0.09 is just inside safe area
        result = engine._check_edge_scroll(0.09, 0.5)
        assert result == []

    def test_gaze_in_left_zone_before_delay(self, engine):
        """Gaze in left edge zone returns empty list before delay elapses."""
        # 0.04 is within the 8% left zone
        result = engine._check_edge_scroll(0.04, 0.5)
        assert result == []

    def test_gaze_in_right_zone_before_delay(self, engine):
        """Gaze in right edge zone returns empty list before delay elapses."""
        result = engine._check_edge_scroll(0.96, 0.5)
        assert result == []

    def test_gaze_in_top_zone_before_delay(self, engine):
        """Gaze in top edge zone returns empty list before delay elapses."""
        result = engine._check_edge_scroll(0.5, 0.04)
        assert result == []

    def test_gaze_in_bottom_zone_before_delay(self, engine):
        """Gaze in bottom edge zone returns empty list before delay elapses."""
        result = engine._check_edge_scroll(0.5, 0.96)
        assert result == []


class TestEdgeScrollActivation:
    """Tests for activation delay and scroll emission."""

    def test_emits_scroll_after_delay(self, engine):
        """After activation delay elapses, emits SCROLL Command."""
        # Enter the right edge zone
        engine._check_edge_scroll(0.96, 0.5)

        # Simulate time passing beyond the delay
        engine._edge_scroll_start = time.monotonic() - 1.0  # 1s ago (delay is 500ms)

        result = engine._check_edge_scroll(0.96, 0.5)
        assert len(result) == 1
        assert result[0].action == "SCROLL"
        assert result[0].params["direction"] == "right"
        assert result[0].source == "edge_scroll"

    def test_emits_scroll_up(self, engine):
        """Emits SCROLL with direction 'up' for top edge zone."""
        engine._check_edge_scroll(0.5, 0.04)
        engine._edge_scroll_start = time.monotonic() - 1.0

        result = engine._check_edge_scroll(0.5, 0.04)
        assert len(result) == 1
        assert result[0].params["direction"] == "up"

    def test_emits_scroll_down(self, engine):
        """Emits SCROLL with direction 'down' for bottom edge zone."""
        engine._check_edge_scroll(0.5, 0.96)
        engine._edge_scroll_start = time.monotonic() - 1.0

        result = engine._check_edge_scroll(0.5, 0.96)
        assert len(result) == 1
        assert result[0].params["direction"] == "down"

    def test_emits_scroll_left(self, engine):
        """Emits SCROLL with direction 'left' for left edge zone."""
        engine._check_edge_scroll(0.04, 0.5)
        engine._edge_scroll_start = time.monotonic() - 1.0

        result = engine._check_edge_scroll(0.04, 0.5)
        assert len(result) == 1
        assert result[0].params["direction"] == "left"

    def test_zero_delay_emits_immediately(self, engine_fast_delay):
        """With 0ms delay, second call in zone emits scroll immediately."""
        # First call sets start time, but since delay is 0, it should emit
        # Need a second call since the first sets the start time to now
        engine_fast_delay._check_edge_scroll(0.96, 0.5)
        result = engine_fast_delay._check_edge_scroll(0.96, 0.5)
        assert len(result) == 1
        assert result[0].action == "SCROLL"


class TestEdgeScrollDeactivation:
    """Tests for immediate stop when gaze leaves edge zones."""

    def test_leaving_zone_stops_immediately(self, engine):
        """Gaze leaving edge zone returns empty list on the very next call."""
        # Activate scrolling
        engine._check_edge_scroll(0.96, 0.5)
        engine._edge_scroll_start = time.monotonic() - 1.0
        result = engine._check_edge_scroll(0.96, 0.5)
        assert len(result) == 1  # scrolling active

        # Leave the zone
        result = engine._check_edge_scroll(0.5, 0.5)
        assert result == []

    def test_leaving_zone_resets_state(self, engine):
        """Leaving zone resets all edge scroll state."""
        engine._check_edge_scroll(0.96, 0.5)
        engine._edge_scroll_start = time.monotonic() - 1.0
        engine._check_edge_scroll(0.96, 0.5)

        # Leave zone
        engine._check_edge_scroll(0.5, 0.5)
        assert engine._edge_scroll_active is False
        assert engine._edge_scroll_start is None
        assert engine._edge_scroll_direction is None


class TestEdgeScrollDirectionChange:
    """Tests for direction change resetting the delay timer."""

    def test_direction_change_resets_delay(self, engine):
        """Changing direction resets the activation delay timer."""
        # Enter right zone and activate
        engine._check_edge_scroll(0.96, 0.5)
        engine._edge_scroll_start = time.monotonic() - 1.0
        result = engine._check_edge_scroll(0.96, 0.5)
        assert len(result) == 1  # scrolling right

        # Move to top zone — should reset delay
        result = engine._check_edge_scroll(0.5, 0.04)
        assert result == []  # delay restarted
        assert engine._edge_scroll_direction == "up"
        assert engine._edge_scroll_active is False


class TestEdgeScrollSpeed:
    """Tests for linear speed interpolation."""

    def test_min_speed_at_inner_boundary(self, engine):
        """Speed equals min_speed at the inner boundary of the zone."""
        # Inner boundary of right zone: gaze_x just above (1.0 - 0.08) = 0.92
        # At exactly the boundary, depth = 0.0
        engine._check_edge_scroll(0.921, 0.5)
        engine._edge_scroll_start = time.monotonic() - 1.0

        result = engine._check_edge_scroll(0.921, 0.5)
        assert len(result) == 1
        # depth ≈ (0.921 - 0.92) / 0.08 ≈ 0.0125 → speed ≈ 1.1 → clicks = 1
        assert result[0].params["clicks"] == 1

    def test_max_speed_at_screen_edge(self, engine):
        """Speed equals max_speed at the screen edge (gaze = 1.0)."""
        engine._check_edge_scroll(1.0, 0.5)
        engine._edge_scroll_start = time.monotonic() - 1.0

        result = engine._check_edge_scroll(1.0, 0.5)
        assert len(result) == 1
        # depth = (1.0 - 0.92) / 0.08 = 1.0 → speed = 10
        assert result[0].params["clicks"] == 10

    def test_mid_zone_speed(self, engine):
        """Speed is interpolated at mid-zone depth."""
        # Mid-point of right zone: (0.92 + 1.0) / 2 = 0.96
        engine._check_edge_scroll(0.96, 0.5)
        engine._edge_scroll_start = time.monotonic() - 1.0

        result = engine._check_edge_scroll(0.96, 0.5)
        assert len(result) == 1
        # depth ≈ 0.5 → speed ≈ 5.5 → clicks = 5 or 6 (floating point)
        assert result[0].params["clicks"] in (5, 6)

    def test_clicks_always_at_least_one(self, engine_fast_delay):
        """Scroll clicks is always at least 1."""
        engine_fast_delay._check_edge_scroll(0.921, 0.5)
        result = engine_fast_delay._check_edge_scroll(0.921, 0.5)
        if result:
            assert result[0].params["clicks"] >= 1


class TestEdgeScrollCornerDiagonal:
    """Tests for corner diagonal scroll (Requirement 3.7).

    When gaze is in two overlapping edge zones (a corner region), the method
    emits two SCROLL Commands — one for each direction.
    """

    def test_top_right_corner_emits_two_commands(self, engine):
        """Gaze in top-right corner emits both 'up' and 'right' SCROLL Commands."""
        # Top-right corner: gaze_y < zone_pct AND gaze_x > (1.0 - zone_pct)
        engine._check_edge_scroll(0.96, 0.04)
        engine._edge_scroll_start = time.monotonic() - 1.0

        result = engine._check_edge_scroll(0.96, 0.04)
        assert len(result) == 2
        directions = {cmd.params["direction"] for cmd in result}
        assert directions == {"up", "right"}
        for cmd in result:
            assert cmd.action == "SCROLL"
            assert cmd.source == "edge_scroll"

    def test_top_left_corner_emits_two_commands(self, engine):
        """Gaze in top-left corner emits both 'up' and 'left' SCROLL Commands."""
        engine._check_edge_scroll(0.04, 0.04)
        engine._edge_scroll_start = time.monotonic() - 1.0

        result = engine._check_edge_scroll(0.04, 0.04)
        assert len(result) == 2
        directions = {cmd.params["direction"] for cmd in result}
        assert directions == {"up", "left"}

    def test_bottom_right_corner_emits_two_commands(self, engine):
        """Gaze in bottom-right corner emits both 'down' and 'right' SCROLL Commands."""
        engine._check_edge_scroll(0.96, 0.96)
        engine._edge_scroll_start = time.monotonic() - 1.0

        result = engine._check_edge_scroll(0.96, 0.96)
        assert len(result) == 2
        directions = {cmd.params["direction"] for cmd in result}
        assert directions == {"down", "right"}

    def test_bottom_left_corner_emits_two_commands(self, engine):
        """Gaze in bottom-left corner emits both 'down' and 'left' SCROLL Commands."""
        engine._check_edge_scroll(0.04, 0.96)
        engine._edge_scroll_start = time.monotonic() - 1.0

        result = engine._check_edge_scroll(0.04, 0.96)
        assert len(result) == 2
        directions = {cmd.params["direction"] for cmd in result}
        assert directions == {"down", "left"}

    def test_corner_each_direction_has_independent_speed(self, engine):
        """Each direction in a corner scroll has speed based on its own depth."""
        # Bottom-right corner: deep into right zone, shallow into bottom zone
        # gaze_x = 1.0 → right depth = 1.0 → max speed (10)
        # gaze_y = 0.93 → bottom depth = (0.93 - 0.92) / 0.08 = 0.125 → speed ≈ 2.1 → clicks = 2
        engine._check_edge_scroll(1.0, 0.93)
        engine._edge_scroll_start = time.monotonic() - 1.0

        result = engine._check_edge_scroll(1.0, 0.93)
        assert len(result) == 2

        right_cmd = next(c for c in result if c.params["direction"] == "right")
        down_cmd = next(c for c in result if c.params["direction"] == "down")

        assert right_cmd.params["clicks"] == 10  # max speed at edge
        assert down_cmd.params["clicks"] in (1, 2)  # low speed near inner boundary

    def test_corner_before_delay_returns_empty(self, engine):
        """Corner gaze before activation delay returns empty list."""
        result = engine._check_edge_scroll(0.96, 0.04)
        assert result == []

    def test_corner_direction_key_tracks_combined(self, engine):
        """Direction key for corners is a combined key (both directions)."""
        engine._check_edge_scroll(0.96, 0.04)
        # Should store a combined direction key
        assert engine._edge_scroll_direction is not None
        assert "right" in engine._edge_scroll_direction
        assert "up" in engine._edge_scroll_direction

    def test_moving_from_edge_to_corner_resets_delay(self, engine):
        """Moving from a single-edge zone to a corner resets the delay timer."""
        # Start in right edge only
        engine._check_edge_scroll(0.96, 0.5)
        engine._edge_scroll_start = time.monotonic() - 1.0
        result = engine._check_edge_scroll(0.96, 0.5)
        assert len(result) == 1  # scrolling right

        # Move to top-right corner — direction changes, delay resets
        result = engine._check_edge_scroll(0.96, 0.04)
        assert result == []  # delay restarted
        assert engine._edge_scroll_active is False

    def test_moving_from_corner_to_edge_resets_delay(self, engine):
        """Moving from a corner to a single-edge zone resets the delay timer."""
        # Start in top-right corner
        engine._check_edge_scroll(0.96, 0.04)
        engine._edge_scroll_start = time.monotonic() - 1.0
        result = engine._check_edge_scroll(0.96, 0.04)
        assert len(result) == 2  # diagonal scrolling

        # Move to right edge only (leave top zone)
        result = engine._check_edge_scroll(0.96, 0.5)
        assert result == []  # delay restarted
        assert engine._edge_scroll_active is False

    def test_corner_with_zero_delay(self, engine_fast_delay):
        """Corner scroll works with zero activation delay."""
        engine_fast_delay._check_edge_scroll(0.96, 0.96)
        result = engine_fast_delay._check_edge_scroll(0.96, 0.96)
        assert len(result) == 2
        directions = {cmd.params["direction"] for cmd in result}
        assert directions == {"down", "right"}

    def test_extreme_corner_max_speed_both_directions(self, engine):
        """At the extreme corner (1.0, 1.0), both directions get max speed."""
        engine._check_edge_scroll(1.0, 1.0)
        engine._edge_scroll_start = time.monotonic() - 1.0

        result = engine._check_edge_scroll(1.0, 1.0)
        assert len(result) == 2
        for cmd in result:
            assert cmd.params["clicks"] == 10

    def test_feature_toggle_disabled_blocks_corner_scroll(self, engine):
        """When edge_scroll toggle is disabled, corner gaze returns empty list."""
        engine._feature_toggles["edge_scroll"] = False
        engine._check_edge_scroll(0.96, 0.04)
        engine._edge_scroll_start = time.monotonic() - 1.0

        result = engine._check_edge_scroll(0.96, 0.04)
        assert result == []
