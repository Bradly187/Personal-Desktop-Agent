"""Property-based tests for FusionEngine coordinate clamping invariant.

Uses Hypothesis to generate floats in [-1.0, 2.0] range for both x and y,
verifying that gaze_coords are always clamped within [0, screen_width] × [0, screen_height]
regardless of out-of-bounds input.

**Validates: Requirements 2.8**

Run:
    python -m pytest tests/test_prop_coordinate_clamping.py -v
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


class TestPropertyCoordinateClamping:
    """Property 2: Coordinate clamping invariant.

    For any normalized coordinates (x, y) where either value is outside [0.0, 1.0],
    the FusionEngine SHALL clamp both values to [0.0, 1.0] before computing pixel
    coordinates, such that the resulting gaze_coords are always within
    [0, screen_width] × [0, screen_height].
    """

    @given(
        x=st.floats(min_value=-1.0, max_value=2.0, allow_nan=False, allow_infinity=False),
        y=st.floats(min_value=-1.0, max_value=2.0, allow_nan=False, allow_infinity=False),
    )
    @settings(max_examples=200)
    def test_gaze_coords_always_within_screen_bounds(self, x: float, y: float) -> None:
        """**Validates: Requirements 2.8**

        For any floats x, y in [-1.0, 2.0], feeding to FusionEngine.on_gaze_dwell(x, y, "left_click")
        and running _tick(), the emitted Command's gaze_coords[0] must be in [0, 1920]
        and gaze_coords[1] must be in [0, 1080].
        """
        engine = _make_engine()
        engine.on_gaze_dwell(x, y, "left_click")
        cmd = asyncio.run(_tick_and_capture(engine))

        assert cmd is not None, (
            f"No Command emitted for x={x}, y={y}"
        )

        px_x, px_y = cmd.gaze_coords

        assert 0 <= px_x <= SCREEN_W, (
            f"gaze_coords[0]={px_x} out of bounds [0, {SCREEN_W}] for input x={x}"
        )
        assert 0 <= px_y <= SCREEN_H, (
            f"gaze_coords[1]={px_y} out of bounds [0, {SCREEN_H}] for input y={y}"
        )
