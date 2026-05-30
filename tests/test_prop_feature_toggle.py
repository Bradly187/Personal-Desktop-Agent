"""Property-based tests for feature toggle enforcement (Property 11).

Feature: enhanced-gaze-dwell, Property 11: Feature toggle enforcement

*For any* feature that is disabled in the Feature_Toggle_Store, and any incoming
event that would normally trigger that feature, the FusionEngine SHALL discard
the event without emitting a Command, and SHALL not alter the state of other
active features.

**Validates: Requirements 3.5, 4.2**

Run:
    pytest tests/test_prop_feature_toggle.py -v
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from hypothesis import given, settings, assume
from hypothesis import strategies as st

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.fusion_engine import FusionConfig, FusionEngine


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# All valid dwell action types
VALID_DWELL_ACTIONS = ["left_click", "right_click", "double_click", "drag_start", "drag_end"]

# All valid feature toggle keys
VALID_FEATURES = [
    "gaze_dwell_click", "gaze_dwell_right_click", "gaze_dwell_double_click",
    "gaze_dwell_drag", "edge_scroll", "gaze_cursor_mode",
]

# Mapping from action_type to its corresponding toggle key
ACTION_TO_TOGGLE = {
    "left_click": "gaze_dwell_click",
    "right_click": "gaze_dwell_right_click",
    "double_click": "gaze_dwell_double_click",
    "drag_start": "gaze_dwell_drag",
    "drag_end": "gaze_dwell_drag",
}

# Strategy for valid dwell action types
st_action_type = st.sampled_from(VALID_DWELL_ACTIONS)

# Strategy for normalized gaze coordinates [0.0, 1.0]
st_norm_coord = st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)

# Strategy for random toggle states (dict of feature -> bool)
st_toggle_states = st.fixed_dictionaries({
    feature: st.booleans() for feature in VALID_FEATURES
})

# Strategy for gaze positions in edge zones (for edge scroll testing)
st_edge_zone_coord = st.one_of(
    st.floats(min_value=0.0, max_value=0.07, allow_nan=False, allow_infinity=False),   # left/top zone
    st.floats(min_value=0.93, max_value=1.0, allow_nan=False, allow_infinity=False),   # right/bottom zone
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def create_engine_with_toggles(toggle_states: dict[str, bool]) -> FusionEngine:
    """Create a FusionEngine and apply the given toggle states."""
    engine = FusionEngine(1920, 1080)
    for feature, enabled in toggle_states.items():
        engine.set_feature_toggle(feature, enabled)
    return engine


def run_tick_and_collect_commands(engine: FusionEngine) -> list:
    """Run a single tick and collect emitted Commands via a mock coordinator."""
    commands = []

    class MockCoordinator:
        async def route(self, cmd):
            commands.append(cmd)

    engine._coordinator = MockCoordinator()

    with patch("pyautogui.moveRel"), patch("pyautogui.moveTo"):
        asyncio.run(engine._tick())

    return commands


# ---------------------------------------------------------------------------
# Property 11: Feature toggle enforcement — Dwell actions
# ---------------------------------------------------------------------------

class TestPropertyFeatureToggleDwell:
    """Property 11: Disabled dwell feature toggles discard events without Commands."""

    @given(
        action_type=st_action_type,
        x=st_norm_coord,
        y=st_norm_coord,
        toggle_states=st_toggle_states,
    )
    @settings(max_examples=200)
    def test_disabled_dwell_toggle_produces_no_command(
        self, action_type: str, x: float, y: float, toggle_states: dict[str, bool]
    ):
        """When a dwell action's toggle is disabled, no Command is emitted.

        **Validates: Requirements 3.5, 4.2**
        """
        # Determine the toggle key for this action type
        toggle_key = ACTION_TO_TOGGLE[action_type]

        # Force the relevant toggle to disabled
        toggle_states[toggle_key] = False

        engine = create_engine_with_toggles(toggle_states)

        # Feed a gaze dwell event
        engine.on_gaze_dwell(x, y, action_type)

        # Run tick and collect commands
        commands = run_tick_and_collect_commands(engine)

        # No command should be emitted for the disabled feature
        assert len(commands) == 0, (
            f"Expected no commands when {toggle_key} is disabled, "
            f"but got {len(commands)} command(s) for action_type={action_type!r}"
        )

    @given(
        action_type=st_action_type,
        x=st_norm_coord,
        y=st_norm_coord,
        toggle_states=st_toggle_states,
    )
    @settings(max_examples=200)
    def test_enabled_dwell_toggle_produces_command(
        self, action_type: str, x: float, y: float, toggle_states: dict[str, bool]
    ):
        """When a dwell action's toggle is enabled, a Command IS emitted.

        **Validates: Requirements 3.5, 4.2**
        """
        # Force the relevant toggle to enabled
        toggle_key = ACTION_TO_TOGGLE[action_type]
        toggle_states[toggle_key] = True

        engine = create_engine_with_toggles(toggle_states)

        # Feed a gaze dwell event
        engine.on_gaze_dwell(x, y, action_type)

        # Run tick and collect commands
        commands = run_tick_and_collect_commands(engine)

        # Exactly one command should be emitted
        assert len(commands) == 1, (
            f"Expected 1 command when {toggle_key} is enabled, "
            f"but got {len(commands)} for action_type={action_type!r}"
        )
        assert commands[0].source == "gaze_dwell"

    @given(
        action_type=st_action_type,
        x=st_norm_coord,
        y=st_norm_coord,
        toggle_states=st_toggle_states,
    )
    @settings(max_examples=200)
    def test_disabled_toggle_does_not_alter_other_toggles(
        self, action_type: str, x: float, y: float, toggle_states: dict[str, bool]
    ):
        """Discarding an event for a disabled feature does not alter other toggle states.

        **Validates: Requirements 3.5, 4.2**
        """
        # Force the relevant toggle to disabled
        toggle_key = ACTION_TO_TOGGLE[action_type]
        toggle_states[toggle_key] = False

        engine = create_engine_with_toggles(toggle_states)

        # Snapshot other toggle states before the event
        other_toggles_before = {
            k: v for k, v in engine._feature_toggles.items() if k != toggle_key
        }

        # Feed a gaze dwell event
        engine.on_gaze_dwell(x, y, action_type)

        # Run tick
        run_tick_and_collect_commands(engine)

        # Verify other toggles are unchanged
        other_toggles_after = {
            k: v for k, v in engine._feature_toggles.items() if k != toggle_key
        }
        assert other_toggles_before == other_toggles_after, (
            f"Other toggle states were altered when processing disabled action {action_type!r}"
        )


# ---------------------------------------------------------------------------
# Property 11: Feature toggle enforcement — Edge scroll
# ---------------------------------------------------------------------------

class TestPropertyFeatureToggleEdgeScroll:
    """Property 11: Disabled edge_scroll toggle discards scroll events."""

    @given(
        gaze_x=st_edge_zone_coord,
        gaze_y=st_norm_coord,
        toggle_states=st_toggle_states,
    )
    @settings(max_examples=200)
    def test_disabled_edge_scroll_produces_no_commands(
        self, gaze_x: float, gaze_y: float, toggle_states: dict[str, bool]
    ):
        """When edge_scroll is disabled, _check_edge_scroll returns no Commands.

        **Validates: Requirements 3.5, 4.2**
        """
        # Force edge_scroll disabled
        toggle_states["edge_scroll"] = False

        engine = create_engine_with_toggles(toggle_states)

        # Simulate being in the zone long enough (past activation delay)
        engine._edge_scroll_start = time.monotonic() - 2.0
        engine._edge_scroll_direction = "right"  # pre-set a direction

        result = engine._check_edge_scroll(gaze_x, gaze_y)

        assert result == [], (
            f"Expected no scroll commands when edge_scroll is disabled, "
            f"but got {len(result)} at gaze=({gaze_x}, {gaze_y})"
        )

    @given(
        gaze_x=st_edge_zone_coord,
        gaze_y=st_norm_coord,
        toggle_states=st_toggle_states,
    )
    @settings(max_examples=200)
    def test_disabled_edge_scroll_does_not_alter_other_toggles(
        self, gaze_x: float, gaze_y: float, toggle_states: dict[str, bool]
    ):
        """Disabling edge_scroll does not alter other feature toggle states.

        **Validates: Requirements 3.5, 4.2**
        """
        toggle_states["edge_scroll"] = False

        engine = create_engine_with_toggles(toggle_states)

        # Snapshot other toggles
        other_toggles_before = {
            k: v for k, v in engine._feature_toggles.items() if k != "edge_scroll"
        }

        # Call edge scroll check
        engine._check_edge_scroll(gaze_x, gaze_y)

        # Verify other toggles unchanged
        other_toggles_after = {
            k: v for k, v in engine._feature_toggles.items() if k != "edge_scroll"
        }
        assert other_toggles_before == other_toggles_after, (
            "Other toggle states were altered when edge_scroll was disabled"
        )

    @given(
        gaze_x=st_edge_zone_coord,
        toggle_states=st_toggle_states,
    )
    @settings(max_examples=100)
    def test_enabled_edge_scroll_can_produce_commands(
        self, gaze_x: float, toggle_states: dict[str, bool]
    ):
        """When edge_scroll is enabled and delay has elapsed, commands are produced.

        **Validates: Requirements 3.5, 4.2**
        """
        # Force edge_scroll enabled
        toggle_states["edge_scroll"] = True

        engine = create_engine_with_toggles(toggle_states)

        # Use a y coordinate in the safe area so only x-axis edge matters
        gaze_y = 0.5

        # First call to set up the direction tracking
        engine._check_edge_scroll(gaze_x, gaze_y)

        # Simulate delay elapsed
        engine._edge_scroll_start = time.monotonic() - 2.0

        result = engine._check_edge_scroll(gaze_x, gaze_y)

        # Should produce at least one scroll command since gaze is in edge zone
        assert len(result) >= 1, (
            f"Expected scroll commands when edge_scroll is enabled and in zone, "
            f"but got none at gaze_x={gaze_x}"
        )
        for cmd in result:
            assert cmd.action == "SCROLL"
            assert cmd.source == "edge_scroll"
