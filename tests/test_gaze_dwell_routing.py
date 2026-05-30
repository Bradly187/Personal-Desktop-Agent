"""Unit tests for FusionEngine gaze dwell action routing (task 2.1).

Validates that on_gaze_dwell(x, y, action_type) correctly routes each
action type to the appropriate Command with correct action verb, params,
source, and gaze_coords. Also validates coordinate clamping and invalid
action type rejection.

Run:
    python -m pytest tests/test_gaze_dwell_routing.py -v
"""

from __future__ import annotations

import asyncio
import logging
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

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


# ---------------------------------------------------------------------------
# Action routing tests
# ---------------------------------------------------------------------------

class TestLeftClick:
    def test_left_click_emits_click_command(self):
        engine = _make_engine()
        engine.on_gaze_dwell(0.5, 0.5, "left_click")
        cmd = asyncio.run(_tick_and_capture(engine))
        assert cmd is not None
        assert cmd.action == "CLICK"
        assert cmd.params == {}
        assert cmd.source == "gaze_dwell"
        assert cmd.gaze_coords == (round(0.5 * SCREEN_W), round(0.5 * SCREEN_H))


class TestRightClick:
    def test_right_click_emits_click_with_button_right(self):
        engine = _make_engine()
        engine.on_gaze_dwell(0.3, 0.7, "right_click")
        cmd = asyncio.run(_tick_and_capture(engine))
        assert cmd is not None
        assert cmd.action == "CLICK"
        assert cmd.params == {"button": "right"}
        assert cmd.source == "gaze_dwell"
        assert cmd.gaze_coords == (round(0.3 * SCREEN_W), round(0.7 * SCREEN_H))


class TestDoubleClick:
    def test_double_click_emits_click_with_clicks_2(self):
        engine = _make_engine()
        engine.on_gaze_dwell(0.8, 0.2, "double_click")
        cmd = asyncio.run(_tick_and_capture(engine))
        assert cmd is not None
        assert cmd.action == "CLICK"
        assert cmd.params == {"clicks": "2"}
        assert cmd.source == "gaze_dwell"
        assert cmd.gaze_coords == (round(0.8 * SCREEN_W), round(0.2 * SCREEN_H))


class TestDragStart:
    def test_drag_start_emits_mousedown(self):
        engine = _make_engine()
        engine.on_gaze_dwell(0.4, 0.6, "drag_start")
        cmd = asyncio.run(_tick_and_capture(engine))
        assert cmd is not None
        assert cmd.action == "MOUSEDOWN"
        assert cmd.params == {}
        assert cmd.source == "gaze_dwell"
        assert cmd.gaze_coords == (round(0.4 * SCREEN_W), round(0.6 * SCREEN_H))


class TestDragEnd:
    def test_drag_end_emits_mouseup(self):
        engine = _make_engine()
        engine.on_gaze_dwell(0.6, 0.4, "drag_end")
        cmd = asyncio.run(_tick_and_capture(engine))
        assert cmd is not None
        assert cmd.action == "MOUSEUP"
        assert cmd.params == {}
        assert cmd.source == "gaze_dwell"
        assert cmd.gaze_coords == (round(0.6 * SCREEN_W), round(0.4 * SCREEN_H))


# ---------------------------------------------------------------------------
# Coordinate clamping tests
# ---------------------------------------------------------------------------

class TestCoordinateClamping:
    def test_negative_coords_clamped_to_zero(self):
        engine = _make_engine()
        engine.on_gaze_dwell(-0.5, -0.3, "left_click")
        cmd = asyncio.run(_tick_and_capture(engine))
        assert cmd is not None
        assert cmd.gaze_coords == (0, 0)

    def test_coords_above_one_clamped_to_screen_bounds(self):
        engine = _make_engine()
        engine.on_gaze_dwell(1.5, 2.0, "left_click")
        cmd = asyncio.run(_tick_and_capture(engine))
        assert cmd is not None
        assert cmd.gaze_coords == (round(1.0 * SCREEN_W), round(1.0 * SCREEN_H))

    def test_coords_at_boundary_pass_through(self):
        engine = _make_engine()
        engine.on_gaze_dwell(0.0, 1.0, "left_click")
        cmd = asyncio.run(_tick_and_capture(engine))
        assert cmd is not None
        assert cmd.gaze_coords == (0, SCREEN_H)

    def test_coords_within_range_unchanged(self):
        engine = _make_engine()
        engine.on_gaze_dwell(0.25, 0.75, "right_click")
        cmd = asyncio.run(_tick_and_capture(engine))
        assert cmd is not None
        assert cmd.gaze_coords == (round(0.25 * SCREEN_W), round(0.75 * SCREEN_H))


# ---------------------------------------------------------------------------
# Invalid action type rejection tests
# ---------------------------------------------------------------------------

class TestInvalidActionType:
    def test_unrecognized_action_type_discards_event(self):
        engine = _make_engine()
        engine.on_gaze_dwell(0.5, 0.5, "triple_click")
        cmd = asyncio.run(_tick_and_capture(engine))
        assert cmd is None

    def test_empty_string_action_type_discards_event(self):
        engine = _make_engine()
        engine.on_gaze_dwell(0.5, 0.5, "")
        cmd = asyncio.run(_tick_and_capture(engine))
        assert cmd is None

    def test_invalid_action_type_logs_warning(self, caplog):
        engine = _make_engine()
        with caplog.at_level(logging.WARNING):
            engine.on_gaze_dwell(0.5, 0.5, "unknown_action")
            asyncio.run(_tick_and_capture(engine))
        assert any("Unrecognized gaze dwell action_type" in r.message for r in caplog.records)
        assert any("unknown_action" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Default action_type parameter test
# ---------------------------------------------------------------------------

class TestDefaultActionType:
    def test_default_action_type_is_left_click(self):
        engine = _make_engine()
        engine.on_gaze_dwell(0.5, 0.5)  # no action_type specified
        cmd = asyncio.run(_tick_and_capture(engine))
        assert cmd is not None
        assert cmd.action == "CLICK"
        assert cmd.params == {}


# ---------------------------------------------------------------------------
# Source field consistency test
# ---------------------------------------------------------------------------

class TestSourceField:
    def test_all_action_types_set_source_gaze_dwell(self):
        for action_type in ["left_click", "right_click", "double_click", "drag_start", "drag_end"]:
            engine = _make_engine()
            engine.on_gaze_dwell(0.5, 0.5, action_type)
            cmd = asyncio.run(_tick_and_capture(engine))
            assert cmd is not None, f"No command emitted for {action_type}"
            assert cmd.source == "gaze_dwell", f"source={cmd.source} for {action_type}"
