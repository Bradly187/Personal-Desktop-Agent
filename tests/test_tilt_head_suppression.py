"""Unit tests for tilt/head suppression when gaze-to-cursor mode is active (task 5.3).

Validates:
- When gaze_cursor_mode enabled, tilt (priority 6) and head (priority 7) are suppressed
- Both remain suppressed even when gaze is temporarily lost
- Restore tilt/head immediately when mode is disabled

Requirements: 5.2, 5.4
"""

import asyncio
from unittest.mock import patch, AsyncMock

import pytest

from fusion_engine import FusionConfig, FusionEngine


@pytest.fixture
def engine():
    """Create a FusionEngine with 1920x1080 screen and gaze_cursor_mode enabled."""
    e = FusionEngine(1920, 1080)
    e._feature_toggles["gaze_cursor_mode"] = True
    return e


@pytest.fixture
def engine_disabled():
    """Create a FusionEngine with gaze_cursor_mode disabled."""
    e = FusionEngine(1920, 1080)
    e._feature_toggles["gaze_cursor_mode"] = False
    return e


def run(coro):
    """Helper to run async coroutines in tests."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestTiltSuppression:
    """Test that tilt events are suppressed when gaze_cursor_mode is enabled."""

    @patch("pyautogui.moveRel")
    def test_tilt_suppressed_when_gaze_cursor_enabled(self, mock_move, engine):
        """Tilt events should be consumed and discarded when gaze_cursor_mode is on."""
        engine.on_tilt(0.1, 0.1)
        run(engine._tick())
        mock_move.assert_not_called()
        # Tilt slot should be cleared
        assert engine._tilt is None

    @patch("pyautogui.moveRel")
    def test_tilt_works_when_gaze_cursor_disabled(self, mock_move, engine_disabled):
        """Tilt events should move cursor normally when gaze_cursor_mode is off."""
        engine_disabled.on_tilt(0.0, 0.1)  # ry=0.1 > dead_zone
        run(engine_disabled._tick())
        mock_move.assert_called_once()

    @patch("pyautogui.moveRel")
    def test_tilt_suppressed_even_when_gaze_lost(self, mock_move, engine):
        """Tilt stays suppressed even if gaze tracking is temporarily lost."""
        # Simulate gaze loss by not providing any gaze data
        # The suppression is based on the toggle, not gaze availability
        engine._gaze_cursor_last_seen = 0.0  # gaze was never seen
        engine.on_tilt(0.1, 0.1)
        run(engine._tick())
        mock_move.assert_not_called()

    @patch("pyautogui.moveRel")
    def test_tilt_restored_when_mode_disabled(self, mock_move, engine):
        """Tilt should work again immediately after disabling gaze_cursor_mode."""
        # Start with mode enabled — tilt suppressed
        engine.on_tilt(0.0, 0.1)
        run(engine._tick())
        mock_move.assert_not_called()

        # Disable mode — tilt should work
        engine._feature_toggles["gaze_cursor_mode"] = False
        engine.on_tilt(0.0, 0.1)
        run(engine._tick())
        mock_move.assert_called_once()


class TestHeadSuppression:
    """Test that head events are suppressed when gaze_cursor_mode is enabled."""

    @patch("pyautogui.moveRel")
    def test_head_suppressed_when_gaze_cursor_enabled(self, mock_move, engine):
        """Head events should be consumed and discarded when gaze_cursor_mode is on."""
        engine.on_head(5.0, 5.0)
        run(engine._tick())
        mock_move.assert_not_called()
        # Head slot should be cleared
        assert engine._head is None

    @patch("pyautogui.moveRel")
    def test_head_works_when_gaze_cursor_disabled(self, mock_move, engine_disabled):
        """Head events should move cursor normally when gaze_cursor_mode is off."""
        engine_disabled.on_head(5.0, 5.0)
        run(engine_disabled._tick())
        mock_move.assert_called_once()

    @patch("pyautogui.moveRel")
    def test_head_suppressed_even_when_gaze_lost(self, mock_move, engine):
        """Head stays suppressed even if gaze tracking is temporarily lost."""
        engine._gaze_cursor_last_seen = 0.0
        engine.on_head(5.0, 5.0)
        run(engine._tick())
        mock_move.assert_not_called()

    @patch("pyautogui.moveRel")
    def test_head_restored_when_mode_disabled(self, mock_move, engine):
        """Head should work again immediately after disabling gaze_cursor_mode."""
        # Start with mode enabled — head suppressed
        engine.on_head(5.0, 5.0)
        run(engine._tick())
        mock_move.assert_not_called()

        # Disable mode — head should work
        engine._feature_toggles["gaze_cursor_mode"] = False
        engine.on_head(5.0, 5.0)
        run(engine._tick())
        mock_move.assert_called_once()


class TestBothSuppressedTogether:
    """Test that both tilt and head are suppressed simultaneously."""

    @patch("pyautogui.moveRel")
    def test_both_suppressed_simultaneously(self, mock_move, engine):
        """Both tilt and head should be suppressed in the same tick."""
        engine.on_tilt(0.1, 0.1)
        engine.on_head(5.0, 5.0)
        run(engine._tick())
        mock_move.assert_not_called()
        assert engine._tilt is None
        assert engine._head is None

    @patch("pyautogui.moveRel")
    def test_both_restored_simultaneously(self, mock_move, engine):
        """Both tilt and head should be restored when mode is disabled."""
        engine._feature_toggles["gaze_cursor_mode"] = False
        engine.on_tilt(0.0, 0.1)
        run(engine._tick())
        assert mock_move.called
        mock_move.reset_mock()

        engine.on_head(5.0, 5.0)
        run(engine._tick())
        assert mock_move.called
