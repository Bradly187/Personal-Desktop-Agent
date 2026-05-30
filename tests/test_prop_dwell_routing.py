"""Property-based tests for FusionEngine gaze dwell action-to-Command mapping.

Uses Hypothesis to generate random valid action types and normalized coordinates,
verifying that the FusionEngine emits Commands with the correct action verb, params,
source, and gaze_coords for every valid input combination.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.6**

Run:
    python -m pytest tests/test_prop_dwell_routing.py -v
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

from hypothesis import given, settings
from hypothesis import strategies as st

from core.command_executor import Command
from core.fusion_engine import FusionEngine, FusionConfig

SCREEN_W = 1920
SCREEN_H = 1080

# Expected mapping from action_type to (Command.action, Command.params)
EXPECTED_MAPPING: dict[str, tuple[str, dict]] = {
    "left_click": ("CLICK", {}),
    "right_click": ("CLICK", {"button": "right"}),
    "double_click": ("CLICK", {"clicks": "2"}),
    "drag_start": ("MOUSEDOWN", {}),
    "drag_end": ("MOUSEUP", {}),
}

VALID_ACTIONS = list(EXPECTED_MAPPING.keys())


def _make_engine() -> FusionEngine:
    """Create a FusionEngine with a mocked coordinator for capturing emitted Commands."""
    engine = FusionEngine(screen_width=SCREEN_W, screen_height=SCREEN_H)
    mock_coordinator = AsyncMock()
    mock_coordinator.route = AsyncMock()
    engine.set_coordinator(mock_coordinator)
    return engine


async def _tick_and_capture(engine: FusionEngine) -> Command | None:
    """Run one tick and return the Command passed to coordinator.route(), or None."""
    with patch("pyautogui.moveRel"), patch("pyautogui.position", return_value=(960, 540)):
        await engine._tick()
    coordinator = engine._coordinator
    if coordinator.route.called:
        return coordinator.route.call_args[0][0]
    return None


class TestPropertyDwellActionMapping:
    """Property 1: Dwell action-to-Command mapping.

    For any valid action type in {"left_click", "right_click", "double_click",
    "drag_start", "drag_end"} and any normalized coordinates (x, y) in [0.0, 1.0]²,
    the FusionEngine SHALL emit exactly one Command with the correct action verb,
    correct params, source="gaze_dwell", and gaze_coords equal to
    (round(x × screen_width), round(y × screen_height)).
    """

    @given(
        action_type=st.sampled_from(VALID_ACTIONS),
        x=st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
        y=st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
    )
    @settings(max_examples=200)
    def test_action_to_command_mapping(self, action_type: str, x: float, y: float) -> None:
        """**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.6**

        For any valid action_type and normalized coords in [0.0, 1.0]²:
        - Exactly one Command is emitted
        - Command.action matches the expected verb
        - Command.params matches the expected params
        - Command.source == "gaze_dwell"
        - Command.gaze_coords == (round(x * 1920), round(y * 1080))
        """
        engine = _make_engine()
        engine.on_gaze_dwell(x, y, action_type)
        cmd = asyncio.run(_tick_and_capture(engine))

        # Exactly one Command emitted
        assert cmd is not None, (
            f"No Command emitted for action_type={action_type!r}, x={x}, y={y}"
        )

        # Correct action verb
        expected_action, expected_params = EXPECTED_MAPPING[action_type]
        assert cmd.action == expected_action, (
            f"Expected action={expected_action!r} for {action_type!r}, got {cmd.action!r}"
        )

        # Correct params
        assert cmd.params == expected_params, (
            f"Expected params={expected_params!r} for {action_type!r}, got {cmd.params!r}"
        )

        # Source is always "gaze_dwell"
        assert cmd.source == "gaze_dwell", (
            f"Expected source='gaze_dwell', got {cmd.source!r}"
        )

        # Correct gaze_coords computation
        expected_px_x = round(x * SCREEN_W)
        expected_px_y = round(y * SCREEN_H)
        assert cmd.gaze_coords == (expected_px_x, expected_px_y), (
            f"Expected gaze_coords=({expected_px_x}, {expected_px_y}), "
            f"got {cmd.gaze_coords} for x={x}, y={y}"
        )
