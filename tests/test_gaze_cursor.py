"""Unit tests for FusionEngine._apply_gaze_cursor()."""

import asyncio
import math
from unittest.mock import patch

import pytest

from fusion_engine import FusionConfig, FusionEngine


@pytest.fixture
def engine():
    """Create a FusionEngine with 1920x1080 screen."""
    return FusionEngine(1920, 1080)


@pytest.fixture
def event_loop():
    """Create an event loop for tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


def run(coro, loop=None):
    """Helper to run async coroutines in tests."""
    if loop is None:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()
    return loop.run_until_complete(coro)


class TestGazeCursorState:
    """Test that state fields are initialized correctly."""

    def test_initial_state(self, engine):
        assert engine._gaze_cursor_last is None
        assert engine._gaze_cursor_ema is None
        assert engine._gaze_cursor_last_seen == 0.0


class TestEMASmoothing:
    """Test EMA smoothing behavior."""

    @patch("pyautogui.moveTo")
    def test_first_sample_initializes_ema(self, mock_move, engine):
        run(engine._apply_gaze_cursor(0.5, 0.5, 0.9))
        assert engine._gaze_cursor_ema == (0.5, 0.5)
        assert engine._gaze_cursor_last is not None

    @patch("pyautogui.moveTo")
    def test_ema_smoothing_applied(self, mock_move, engine):
        loop = asyncio.new_event_loop()
        try:
            # First sample
            loop.run_until_complete(engine._apply_gaze_cursor(0.5, 0.5, 0.9))
            # Second sample — EMA should blend
            loop.run_until_complete(engine._apply_gaze_cursor(0.6, 0.6, 0.9))
        finally:
            loop.close()
        alpha = engine._cfg.gaze_cursor_ema_alpha  # 0.3
        expected_x = alpha * 0.6 + (1 - alpha) * 0.5  # 0.53
        expected_y = alpha * 0.6 + (1 - alpha) * 0.5  # 0.53
        assert engine._gaze_cursor_ema[0] == pytest.approx(expected_x, abs=1e-6)
        assert engine._gaze_cursor_ema[1] == pytest.approx(expected_y, abs=1e-6)

    @patch("pyautogui.moveTo")
    def test_cursor_moves_to_pixel_coords(self, mock_move, engine):
        run(engine._apply_gaze_cursor(0.5, 0.5, 0.9))
        # First sample: EMA = (0.5, 0.5), pixel = (960, 540)
        mock_move.assert_called_once_with(960, 540, _pause=False)


class TestDisplacementClamping:
    """Test that frame-to-frame displacement is clamped to 5% of screen diagonal."""

    @patch("pyautogui.moveTo")
    def test_large_jump_is_clamped(self, mock_move, engine):
        loop = asyncio.new_event_loop()
        try:
            # Initialize at center
            loop.run_until_complete(engine._apply_gaze_cursor(0.5, 0.5, 0.9))
            # Jump to far corner — raw EMA would be 0.3*1.0 + 0.7*0.5 = 0.65
            # but displacement should be clamped
            loop.run_until_complete(engine._apply_gaze_cursor(1.0, 1.0, 0.9))
        finally:
            loop.close()

        ema_x, ema_y = engine._gaze_cursor_ema
        # Displacement from (0.5, 0.5) in pixels
        dx = (ema_x - 0.5) * 1920
        dy = (ema_y - 0.5) * 1080
        displacement = math.hypot(dx, dy)
        max_allowed = 0.05 * math.hypot(1920, 1080)
        assert displacement <= max_allowed + 0.01  # small float tolerance

    @patch("pyautogui.moveTo")
    def test_small_movement_not_clamped(self, mock_move, engine):
        loop = asyncio.new_event_loop()
        try:
            # Initialize at center
            loop.run_until_complete(engine._apply_gaze_cursor(0.5, 0.5, 0.9))
            # Small movement — should not be clamped
            loop.run_until_complete(engine._apply_gaze_cursor(0.51, 0.51, 0.9))
        finally:
            loop.close()
        alpha = engine._cfg.gaze_cursor_ema_alpha  # read from config — survives tuning
        expected_x = alpha * 0.51 + (1 - alpha) * 0.5
        expected_y = alpha * 0.51 + (1 - alpha) * 0.5
        assert engine._gaze_cursor_ema[0] == pytest.approx(expected_x, abs=1e-6)
        assert engine._gaze_cursor_ema[1] == pytest.approx(expected_y, abs=1e-6)


class TestConfidenceGating:
    """Test that low confidence holds cursor at last position."""

    @patch("pyautogui.moveTo")
    def test_low_confidence_does_not_move(self, mock_move, engine):
        loop = asyncio.new_event_loop()
        try:
            # First valid sample
            loop.run_until_complete(engine._apply_gaze_cursor(0.5, 0.5, 0.9))
            mock_move.reset_mock()
            # Low confidence — should not move
            loop.run_until_complete(engine._apply_gaze_cursor(0.8, 0.8, 0.3))
        finally:
            loop.close()
        mock_move.assert_not_called()
        # EMA should remain unchanged
        assert engine._gaze_cursor_ema == (0.5, 0.5)

    @patch("pyautogui.moveTo")
    def test_confidence_at_threshold_does_not_move(self, mock_move, engine):
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(engine._apply_gaze_cursor(0.5, 0.5, 0.9))
            mock_move.reset_mock()
            # Exactly at threshold (0.55) — below means < not <=
            loop.run_until_complete(engine._apply_gaze_cursor(0.8, 0.8, 0.54))
        finally:
            loop.close()
        mock_move.assert_not_called()

    @patch("pyautogui.moveTo")
    def test_confidence_above_threshold_moves(self, mock_move, engine):
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(engine._apply_gaze_cursor(0.5, 0.5, 0.9))
            mock_move.reset_mock()
            loop.run_until_complete(engine._apply_gaze_cursor(0.51, 0.51, 0.55))
        finally:
            loop.close()
        mock_move.assert_called_once()


class TestGazeLostTimeout:
    """Test that gaze lost > 500ms logs a warning."""

    @patch("pyautogui.moveTo")
    @patch("fusion_engine.time.monotonic")
    def test_gaze_lost_logs_warning(self, mock_time, mock_move, engine, caplog):
        import logging

        loop = asyncio.new_event_loop()
        try:
            # First valid sample at t=1.0
            mock_time.return_value = 1.0
            loop.run_until_complete(engine._apply_gaze_cursor(0.5, 0.5, 0.9))

            # Low confidence at t=1.6 (0.6s after last seen, > 0.5s timeout)
            mock_time.return_value = 1.6
            with caplog.at_level(logging.WARNING):
                loop.run_until_complete(engine._apply_gaze_cursor(0.8, 0.8, 0.3))
        finally:
            loop.close()

        assert "gaze lost" in caplog.text.lower()

    @patch("pyautogui.moveTo")
    @patch("fusion_engine.time.monotonic")
    def test_gaze_lost_within_timeout_no_warning(self, mock_time, mock_move, engine, caplog):
        import logging

        loop = asyncio.new_event_loop()
        try:
            # First valid sample at t=1.0
            mock_time.return_value = 1.0
            loop.run_until_complete(engine._apply_gaze_cursor(0.5, 0.5, 0.9))

            # Low confidence at t=1.3 (0.3s after last seen, < 0.5s timeout)
            mock_time.return_value = 1.3
            with caplog.at_level(logging.WARNING):
                loop.run_until_complete(engine._apply_gaze_cursor(0.8, 0.8, 0.3))
        finally:
            loop.close()

        assert "gaze lost" not in caplog.text.lower()


class TestPyautoguiErrorHandling:
    """Test graceful handling of pyautogui failures."""

    @patch("pyautogui.moveTo", side_effect=Exception("Display not available"))
    def test_pyautogui_failure_does_not_crash(self, mock_move, engine):
        # Should not raise — error is caught and logged
        run(engine._apply_gaze_cursor(0.5, 0.5, 0.9))
        # EMA state should still be updated
        assert engine._gaze_cursor_ema == (0.5, 0.5)
        assert engine._gaze_cursor_last == (960, 540)


class TestDwellAtSmoothedPosition:
    """Test that dwell events use EMA-smoothed position when gaze-to-cursor mode is active."""

    def _make_engine_with_coordinator(self):
        """Create a FusionEngine with a mocked coordinator for capturing emitted Commands."""
        from unittest.mock import AsyncMock

        engine = FusionEngine(1920, 1080)
        mock_coordinator = AsyncMock()
        mock_coordinator.route = AsyncMock()
        engine.set_coordinator(mock_coordinator)
        return engine

    async def _tick_and_capture(self, engine):
        """Run one tick and return the Command passed to coordinator.route(), or None."""
        with patch("pyautogui.moveRel"), patch("pyautogui.moveTo"), \
             patch("pyautogui.position", return_value=(960, 540)):
            await engine._tick()
        coordinator = engine._coordinator
        if coordinator.route.called:
            return coordinator.route.call_args[0][0]
        return None

    @patch("pyautogui.moveTo")
    def test_dwell_uses_ema_when_gaze_cursor_active(self, mock_move):
        """When gaze_cursor_mode is enabled and EMA is set, dwell uses smoothed coords."""
        engine = self._make_engine_with_coordinator()
        # Enable gaze_cursor_mode
        engine._feature_toggles["gaze_cursor_mode"] = True
        # Set EMA state to a known smoothed position
        engine._gaze_cursor_ema = (0.3, 0.7)

        # Fire a dwell at raw position (0.5, 0.5) — should use EMA (0.3, 0.7) instead
        engine.on_gaze_dwell(0.5, 0.5, "left_click")
        cmd = asyncio.run(self._tick_and_capture(engine))

        assert cmd is not None
        assert cmd.action == "CLICK"
        assert cmd.source == "gaze_dwell"
        # Should use EMA coords, not raw
        expected_x = round(0.3 * 1920)
        expected_y = round(0.7 * 1080)
        assert cmd.gaze_coords == (expected_x, expected_y)

    @patch("pyautogui.moveTo")
    def test_dwell_uses_raw_when_gaze_cursor_disabled(self, mock_move):
        """When gaze_cursor_mode is disabled, dwell uses raw gaze coords."""
        engine = self._make_engine_with_coordinator()
        # Disable gaze_cursor_mode
        engine._feature_toggles["gaze_cursor_mode"] = False
        # Set EMA state — should be ignored
        engine._gaze_cursor_ema = (0.3, 0.7)

        # Fire a dwell at raw position (0.5, 0.5)
        engine.on_gaze_dwell(0.5, 0.5, "left_click")
        cmd = asyncio.run(self._tick_and_capture(engine))

        assert cmd is not None
        # Should use raw coords since mode is disabled
        expected_x = round(0.5 * 1920)
        expected_y = round(0.5 * 1080)
        assert cmd.gaze_coords == (expected_x, expected_y)

    @patch("pyautogui.moveTo")
    def test_dwell_uses_raw_when_ema_is_none(self, mock_move):
        """When gaze_cursor_mode is enabled but EMA hasn't been initialized, use raw coords."""
        engine = self._make_engine_with_coordinator()
        # Enable gaze_cursor_mode but leave EMA as None
        engine._feature_toggles["gaze_cursor_mode"] = True
        engine._gaze_cursor_ema = None

        # Fire a dwell at raw position (0.5, 0.5)
        engine.on_gaze_dwell(0.5, 0.5, "left_click")
        cmd = asyncio.run(self._tick_and_capture(engine))

        assert cmd is not None
        # Should use raw coords since EMA is None
        expected_x = round(0.5 * 1920)
        expected_y = round(0.5 * 1080)
        assert cmd.gaze_coords == (expected_x, expected_y)

    @patch("pyautogui.moveTo")
    def test_dwell_at_smoothed_position_all_action_types(self, mock_move):
        """All dwell action types use EMA position when gaze-to-cursor is active."""
        for action_type in ["left_click", "right_click", "double_click", "drag_start", "drag_end"]:
            engine = self._make_engine_with_coordinator()
            engine._feature_toggles["gaze_cursor_mode"] = True
            engine._gaze_cursor_ema = (0.2, 0.8)

            engine.on_gaze_dwell(0.9, 0.1, action_type)
            cmd = asyncio.run(self._tick_and_capture(engine))

            assert cmd is not None, f"No command emitted for {action_type}"
            expected_x = round(0.2 * 1920)
            expected_y = round(0.8 * 1080)
            assert cmd.gaze_coords == (expected_x, expected_y), (
                f"{action_type}: expected ({expected_x}, {expected_y}), got {cmd.gaze_coords}"
            )

    @patch("pyautogui.moveTo")
    def test_dwell_timer_fires_normally_with_gaze_cursor(self, mock_move):
        """Dwell timer continues to fire normally — the event still produces a Command."""
        engine = self._make_engine_with_coordinator()
        engine._feature_toggles["gaze_cursor_mode"] = True
        engine._gaze_cursor_ema = (0.4, 0.6)

        # Simulate dwell firing (on_gaze_dwell is called by the iPad dwell timer)
        engine.on_gaze_dwell(0.5, 0.5, "right_click")
        cmd = asyncio.run(self._tick_and_capture(engine))

        # Command should still be emitted (timer fires normally)
        assert cmd is not None
        assert cmd.action == "CLICK"
        assert cmd.params == {"button": "right"}
