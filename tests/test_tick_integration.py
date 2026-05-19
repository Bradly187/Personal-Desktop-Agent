"""Unit tests for FusionEngine._tick() integration of edge scroll and gaze-to-cursor.

Validates task 11.1: wiring _check_edge_scroll() and _apply_gaze_cursor() into
the 60Hz tick loop, ensuring they coexist with dwell routing and respect the
tick budget.

Requirements: 3.1, 5.1
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from command_executor import Command
from fusion_engine import FusionConfig, FusionEngine


@pytest.fixture
def engine():
    """Create a FusionEngine with a mock coordinator."""
    fe = FusionEngine(1920, 1080, FusionConfig(edge_scroll_delay_ms=0))
    mock_coord = AsyncMock()
    mock_coord.route = AsyncMock()
    fe.set_coordinator(mock_coord)
    return fe


@pytest.fixture
def engine_with_delay():
    """Create a FusionEngine with default 500ms edge scroll delay."""
    fe = FusionEngine(1920, 1080, FusionConfig())
    mock_coord = AsyncMock()
    mock_coord.route = AsyncMock()
    fe.set_coordinator(mock_coord)
    return fe


class TestEdgeScrollInTick:
    """Edge scroll is called on every tick when gaze data is available."""

    @pytest.mark.asyncio
    async def test_edge_scroll_called_when_gaze_in_edge_zone(self, engine):
        """Edge scroll emits SCROLL commands when gaze is in edge zone."""
        # Feed gaze data in the right edge zone (x > 0.92)
        engine.on_gaze(0.96, 0.5, 0.9)

        with patch("pyautogui.moveTo"), patch("pyautogui.moveRel"):
            await engine._tick()

        # Should have emitted a SCROLL command
        calls = engine._coordinator.route.call_args_list
        scroll_cmds = [c for c in calls if c[0][0].action == "SCROLL"]
        assert len(scroll_cmds) == 1
        assert scroll_cmds[0][0][0].params["direction"] == "right"

    @pytest.mark.asyncio
    async def test_edge_scroll_not_called_when_gaze_in_center(self, engine):
        """No SCROLL commands when gaze is in the safe area."""
        engine.on_gaze(0.5, 0.5, 0.9)

        with patch("pyautogui.moveTo"), patch("pyautogui.moveRel"):
            await engine._tick()

        calls = engine._coordinator.route.call_args_list
        scroll_cmds = [c for c in calls if c[0][0].action == "SCROLL"]
        assert len(scroll_cmds) == 0

    @pytest.mark.asyncio
    async def test_edge_scroll_not_called_when_disabled(self, engine):
        """Edge scroll skipped when feature toggle is disabled."""
        engine.set_feature_toggle("edge_scroll", False)
        engine.on_gaze(0.96, 0.5, 0.9)

        with patch("pyautogui.moveTo"), patch("pyautogui.moveRel"):
            await engine._tick()

        calls = engine._coordinator.route.call_args_list
        scroll_cmds = [c for c in calls if c[0][0].action == "SCROLL"]
        assert len(scroll_cmds) == 0

    @pytest.mark.asyncio
    async def test_edge_scroll_not_called_when_no_recent_gaze(self, engine):
        """Edge scroll skipped when gaze buffer has no recent data."""
        # Don't feed any gaze data — buffer is empty
        with patch("pyautogui.moveTo"), patch("pyautogui.moveRel"):
            await engine._tick()

        calls = engine._coordinator.route.call_args_list
        assert len(calls) == 0

    @pytest.mark.asyncio
    async def test_edge_scroll_resets_when_gaze_lost(self, engine):
        """Edge scroll state resets when gaze data becomes stale."""
        # First, activate edge scroll
        engine.on_gaze(0.96, 0.5, 0.9)
        with patch("pyautogui.moveTo"), patch("pyautogui.moveRel"):
            await engine._tick()

        assert engine._edge_scroll_active is True

        # Simulate stale gaze by backdating the sample
        engine._gaze_buf._buf[-1].ts = time.monotonic() - 1.0

        with patch("pyautogui.moveTo"), patch("pyautogui.moveRel"):
            await engine._tick()

        assert engine._edge_scroll_active is False


class TestGazeCursorInTick:
    """Gaze-to-cursor is called on every tick when mode is enabled."""

    @pytest.mark.asyncio
    async def test_gaze_cursor_called_when_enabled(self, engine):
        """Cursor moves to gaze position when gaze_cursor_mode is enabled."""
        engine.set_feature_toggle("gaze_cursor_mode", True)
        engine.on_gaze(0.5, 0.5, 0.9)

        with patch("pyautogui.moveTo") as mock_move, patch("pyautogui.moveRel"):
            await engine._tick()

        # Should have called moveTo with the gaze position
        mock_move.assert_called_once()
        args = mock_move.call_args[0]
        assert args[0] == 960  # 0.5 * 1920
        assert args[1] == 540  # 0.5 * 1080

    @pytest.mark.asyncio
    async def test_gaze_cursor_not_called_when_disabled(self, engine):
        """Cursor does not move when gaze_cursor_mode is disabled."""
        engine.set_feature_toggle("gaze_cursor_mode", False)
        engine.on_gaze(0.5, 0.5, 0.9)

        with patch("pyautogui.moveTo") as mock_move, patch("pyautogui.moveRel"):
            await engine._tick()

        mock_move.assert_not_called()

    @pytest.mark.asyncio
    async def test_gaze_cursor_holds_when_gaze_lost(self, engine):
        """Cursor holds at last position when gaze data is stale."""
        engine.set_feature_toggle("gaze_cursor_mode", True)

        # First, establish a position
        engine.on_gaze(0.5, 0.5, 0.9)
        with patch("pyautogui.moveTo") as mock_move, patch("pyautogui.moveRel"):
            await engine._tick()
        mock_move.assert_called_once()

        # Now make gaze stale
        engine._gaze_buf._buf[-1].ts = time.monotonic() - 1.0

        with patch("pyautogui.moveTo") as mock_move2, patch("pyautogui.moveRel"):
            await engine._tick()

        # Should NOT move cursor (confidence 0.0 triggers hold)
        mock_move2.assert_not_called()


class TestCoexistence:
    """Edge scroll, gaze-to-cursor, and dwell routing coexist without conflicts."""

    @pytest.mark.asyncio
    async def test_dwell_takes_priority_over_edge_scroll(self, engine):
        """Dwell event short-circuits tick — edge scroll not processed."""
        engine.on_gaze(0.96, 0.5, 0.9)  # In edge zone
        engine.on_gaze_dwell(0.5, 0.5, "left_click")  # Dwell event

        with patch("pyautogui.moveTo"), patch("pyautogui.moveRel"):
            await engine._tick()

        # Only the dwell click should have been emitted (priority 3 short-circuits)
        calls = engine._coordinator.route.call_args_list
        assert len(calls) == 1
        assert calls[0][0][0].action == "CLICK"
        assert calls[0][0][0].source == "gaze_dwell"

    @pytest.mark.asyncio
    async def test_edge_scroll_and_gaze_cursor_both_run(self, engine):
        """Both edge scroll and gaze-to-cursor run on the same tick."""
        engine.set_feature_toggle("gaze_cursor_mode", True)
        # Gaze in right edge zone
        engine.on_gaze(0.96, 0.5, 0.9)

        with patch("pyautogui.moveTo") as mock_move, patch("pyautogui.moveRel"):
            await engine._tick()

        # Edge scroll should emit SCROLL
        calls = engine._coordinator.route.call_args_list
        scroll_cmds = [c for c in calls if c[0][0].action == "SCROLL"]
        assert len(scroll_cmds) == 1

        # Gaze-to-cursor should move cursor
        mock_move.assert_called_once()

    @pytest.mark.asyncio
    async def test_touch_takes_priority_over_all(self, engine):
        """Touch event short-circuits — no edge scroll or gaze cursor."""
        engine.set_feature_toggle("gaze_cursor_mode", True)
        engine.on_gaze(0.96, 0.5, 0.9)
        engine.on_touch(Command(text="tap", action="CLICK", source="touch"))

        with patch("pyautogui.moveTo") as mock_move, patch("pyautogui.moveRel"):
            await engine._tick()

        # Only touch command emitted
        calls = engine._coordinator.route.call_args_list
        assert len(calls) == 1
        assert calls[0][0][0].source == "touch"
        # No cursor move
        mock_move.assert_not_called()

    @pytest.mark.asyncio
    async def test_gaze_cursor_suppresses_tilt(self, engine):
        """Tilt is suppressed when gaze-to-cursor is active."""
        engine.set_feature_toggle("gaze_cursor_mode", True)
        engine.on_gaze(0.5, 0.5, 0.9)
        engine.on_tilt(0.1, 0.1)

        with patch("pyautogui.moveTo") as mock_move, patch("pyautogui.moveRel") as mock_rel:
            await engine._tick()

        # Cursor should move via gaze-to-cursor
        mock_move.assert_called_once()
        # Tilt should NOT move cursor
        mock_rel.assert_not_called()


class TestTickBudget:
    """Edge scroll is skipped when tick exceeds 16ms budget."""

    @pytest.mark.asyncio
    async def test_edge_scroll_skipped_when_tick_exceeds_budget(self, engine):
        """Edge scroll is skipped if tick processing already took >16ms."""
        engine.on_gaze(0.96, 0.5, 0.9)

        # Monkey-patch time.monotonic to simulate elapsed time
        original_monotonic = time.monotonic
        call_count = [0]

        def mock_monotonic():
            call_count[0] += 1
            # First call (tick_start) returns normal time
            # Second call (budget check) returns tick_start + 17ms
            if call_count[0] == 1:
                return original_monotonic()
            else:
                # Return a time 17ms after the first call
                return mock_monotonic._first_time + 0.017

        mock_monotonic._first_time = original_monotonic()

        with patch("time.monotonic", side_effect=mock_monotonic), \
             patch("pyautogui.moveTo"), patch("pyautogui.moveRel"):
            await engine._tick()

        # Edge scroll should have been skipped
        calls = engine._coordinator.route.call_args_list
        scroll_cmds = [c for c in calls if c[0][0].action == "SCROLL"]
        assert len(scroll_cmds) == 0
