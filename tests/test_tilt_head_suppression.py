"""Unit tests for tilt/head suppression when gaze-to-cursor mode is active (task 5.3).

Updated for the escape-hatch design (fusion_engine.py refactor):

NEW BEHAVIOUR:
  - Tilt and head ARE suppressed when gaze_cursor_mode=True AND gaze is recent
    (i.e. a gaze sample arrived within the last 300 ms)
  - Tilt and head break through as an escape hatch when gaze is lost / not recent
    so the user can regain cursor control without disabling the mode
  - Both are fully restored immediately when gaze_cursor_mode is disabled

Old behaviour (before refactor): suppression was unconditional on gaze recency.

Requirements: 5.2, 5.4
"""

import asyncio
import time
from unittest.mock import patch, AsyncMock

import pytest

from fusion_engine import FusionConfig, FusionEngine


@pytest.fixture
def engine():
    """FusionEngine with gaze_cursor_mode enabled (1920×1080)."""
    e = FusionEngine(1920, 1080)
    e._feature_toggles["gaze_cursor_mode"] = True
    return e


@pytest.fixture
def engine_disabled():
    """FusionEngine with gaze_cursor_mode disabled."""
    e = FusionEngine(1920, 1080)
    e._feature_toggles["gaze_cursor_mode"] = False
    return e


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Tilt suppression
# ---------------------------------------------------------------------------

class TestTiltSuppression:

    @patch("pyautogui.moveRel")
    def test_tilt_suppressed_when_gaze_active_and_mode_on(self, mock_move, engine):
        """Tilt is suppressed while gaze is recent and gaze_cursor_mode is on."""
        engine.on_gaze(0.5, 0.5, 0.9)      # inject a fresh gaze sample
        engine.on_tilt(0.1, 0.1)
        run(engine._tick())
        mock_move.assert_not_called()
        assert engine._tilt is None

    @patch("pyautogui.moveRel")
    def test_tilt_works_when_gaze_cursor_disabled(self, mock_move, engine_disabled):
        """Tilt moves cursor normally when gaze_cursor_mode is off."""
        engine_disabled.on_gaze(0.5, 0.5, 0.9)
        engine_disabled.on_tilt(0.0, 0.1)   # ry=0.1 > dead_zone
        run(engine_disabled._tick())
        mock_move.assert_called_once()

    @patch("pyautogui.moveRel")
    def test_tilt_allowed_as_escape_when_gaze_lost(self, mock_move, engine):
        """Tilt acts as escape hatch when gaze is not recent (no gaze data)."""
        # No on_gaze() call → gaze_is_recent=False → tilt breaks through
        engine.on_tilt(0.1, 0.1)
        run(engine._tick())
        mock_move.assert_called_once()

    @patch("pyautogui.moveRel")
    def test_tilt_restored_when_mode_disabled(self, mock_move, engine):
        """Tilt works immediately after disabling gaze_cursor_mode."""
        engine.on_gaze(0.5, 0.5, 0.9)          # gaze active → suppressed
        engine._feature_toggles["gaze_cursor_mode"] = False  # disable mode
        engine.on_tilt(0.0, 0.1)
        run(engine._tick())
        mock_move.assert_called_once()


# ---------------------------------------------------------------------------
# Head suppression
# ---------------------------------------------------------------------------

class TestHeadSuppression:

    @patch("pyautogui.moveRel")
    def test_head_suppressed_when_gaze_active_and_mode_on(self, mock_move, engine):
        """Head is suppressed while gaze is recent and gaze_cursor_mode is on."""
        engine.on_gaze(0.5, 0.5, 0.9)
        engine.on_head(5.0, 5.0)
        run(engine._tick())
        mock_move.assert_not_called()
        assert engine._head is None

    @patch("pyautogui.moveRel")
    def test_head_works_when_gaze_cursor_disabled(self, mock_move, engine_disabled):
        """Head moves cursor normally when gaze_cursor_mode is off."""
        engine_disabled.on_gaze(0.5, 0.5, 0.9)
        engine_disabled.on_head(5.0, 5.0)
        run(engine_disabled._tick())
        mock_move.assert_called_once()

    @patch("pyautogui.moveRel")
    def test_head_allowed_as_escape_when_gaze_lost(self, mock_move, engine):
        """Head acts as escape hatch when gaze is not recent."""
        engine.on_head(5.0, 5.0)
        run(engine._tick())
        mock_move.assert_called_once()

    @patch("pyautogui.moveRel")
    def test_head_restored_when_mode_disabled(self, mock_move, engine):
        """Head works immediately after disabling gaze_cursor_mode."""
        engine.on_gaze(0.5, 0.5, 0.9)
        engine._feature_toggles["gaze_cursor_mode"] = False
        engine.on_head(5.0, 5.0)
        run(engine._tick())
        mock_move.assert_called_once()


# ---------------------------------------------------------------------------
# Both suppressed together
# ---------------------------------------------------------------------------

class TestBothSuppressedTogether:

    @patch("pyautogui.moveRel")
    def test_both_suppressed_when_gaze_active(self, mock_move, engine):
        """Both tilt and head are suppressed in the same tick when gaze is recent."""
        engine.on_gaze(0.5, 0.5, 0.9)
        engine.on_tilt(0.1, 0.1)
        engine.on_head(5.0, 5.0)
        run(engine._tick())
        mock_move.assert_not_called()
        assert engine._tilt is None
        assert engine._head is None

    @patch("pyautogui.moveRel")
    def test_both_restored_simultaneously(self, mock_move, engine):
        """Both tilt and head are restored when gaze_cursor_mode is disabled."""
        engine._feature_toggles["gaze_cursor_mode"] = False
        engine.on_tilt(0.0, 0.1)
        run(engine._tick())
        assert mock_move.called
        mock_move.reset_mock()

        engine.on_head(5.0, 5.0)
        run(engine._tick())
        assert mock_move.called
