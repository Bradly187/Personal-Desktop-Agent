"""Property-based tests for FusionEngine gaze-to-cursor mode.

Uses Hypothesis to validate correctness properties for gaze-to-cursor behavior:
- Property 13: Gaze-to-cursor EMA displacement cap
- Property 14: Gaze-to-cursor suppresses tilt and head
- Property 15: Low-confidence gaze holds cursor position
- Property 16: Dwell fires at smoothed cursor position

Validates: Requirements 5.2, 5.3, 5.5, 5.7

Run:
    pytest tests/test_prop_gaze_cursor.py -v
"""

from __future__ import annotations

import asyncio
import math
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from hypothesis import given, settings, assume
from hypothesis import strategies as st

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.command_executor import Command
from core.fusion_engine import FusionEngine, FusionConfig

SCREEN_W = 1920
SCREEN_H = 1080
SCREEN_DIAG = math.hypot(SCREEN_W, SCREEN_H)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Normalized gaze coordinates [0.0, 1.0]
norm_coord_st = st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)

# Confidence values [0.0, 1.0]
conf_st = st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)

# High confidence (above threshold)
high_conf_st = st.floats(min_value=0.55, max_value=1.0, allow_nan=False, allow_infinity=False)

# Low confidence (below threshold)
low_conf_st = st.floats(min_value=0.0, max_value=0.5499, allow_nan=False, allow_infinity=False)

# Large jumps in gaze position (to test displacement cap)
large_jump_st = st.floats(min_value=0.3, max_value=1.0, allow_nan=False, allow_infinity=False)

# Tilt values (rad/s)
tilt_st = st.floats(min_value=-2.0, max_value=2.0, allow_nan=False, allow_infinity=False)

# Head tracking values (degrees)
head_st = st.floats(min_value=-30.0, max_value=30.0, allow_nan=False, allow_infinity=False)

# EMA alpha values
alpha_st = st.floats(min_value=0.05, max_value=0.95, allow_nan=False, allow_infinity=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_engine(gaze_cursor_mode: bool = True) -> FusionEngine:
    """Create a FusionEngine with gaze-to-cursor mode configured."""
    cfg = FusionConfig()
    engine = FusionEngine(screen_width=SCREEN_W, screen_height=SCREEN_H, config=cfg)
    engine._feature_toggles["gaze_cursor_mode"] = gaze_cursor_mode
    mock_coordinator = AsyncMock()
    mock_coordinator.route = AsyncMock()
    engine.set_coordinator(mock_coordinator)
    return engine


async def _apply_gaze_cursor(engine: FusionEngine, x: float, y: float, conf: float) -> None:
    """Call _apply_gaze_cursor with pyautogui mocked."""
    with patch("pyautogui.moveTo"):
        await engine._apply_gaze_cursor(x, y, conf)


async def _tick_and_capture(engine: FusionEngine) -> Command | None:
    """Run one tick and return the Command passed to coordinator.route(), or None."""
    with patch("pyautogui.moveRel"), patch("pyautogui.moveTo"), \
         patch("pyautogui.position", return_value=(960, 540)):
        await engine._tick()
    coordinator = engine._coordinator
    if coordinator.route.called:
        return coordinator.route.call_args[0][0]
    return None


# ---------------------------------------------------------------------------
# Property 13: Gaze-to-cursor EMA displacement cap
# ---------------------------------------------------------------------------

class TestProperty13EMADisplacementCap:
    """Property 13: For any sequence of raw gaze positions fed to the gaze-to-cursor
    system, the frame-to-frame cursor displacement SHALL never exceed 5% of the
    screen diagonal in a single tick, regardless of how large the raw gaze position
    jump is.

    **Validates: Requirements 5.3**
    """

    @given(
        start_x=norm_coord_st,
        start_y=norm_coord_st,
        jump_x=norm_coord_st,
        jump_y=norm_coord_st,
    )
    @settings(max_examples=300)
    def test_displacement_never_exceeds_cap(
        self, start_x: float, start_y: float, jump_x: float, jump_y: float
    ) -> None:
        """After establishing an EMA position, any subsequent gaze jump produces
        cursor displacement <= 5% of screen diagonal."""
        engine = _make_engine()

        # Initialize EMA with first sample
        asyncio.run(_apply_gaze_cursor(engine, start_x, start_y, 0.9))
        assert engine._gaze_cursor_ema is not None
        prev_ema = engine._gaze_cursor_ema

        # Apply a potentially large jump
        asyncio.run(_apply_gaze_cursor(engine, jump_x, jump_y, 0.9))
        new_ema = engine._gaze_cursor_ema

        # Compute displacement in pixels
        dx = (new_ema[0] - prev_ema[0]) * SCREEN_W
        dy = (new_ema[1] - prev_ema[1]) * SCREEN_H
        displacement_px = math.hypot(dx, dy)

        max_allowed = 0.05 * SCREEN_DIAG

        assert displacement_px <= max_allowed + 1e-6, (
            f"Displacement {displacement_px:.2f}px exceeds cap {max_allowed:.2f}px "
            f"(start=({start_x:.3f},{start_y:.3f}), jump=({jump_x:.3f},{jump_y:.3f}))"
        )

    @given(
        start_x=norm_coord_st,
        start_y=norm_coord_st,
        num_jumps=st.integers(min_value=2, max_value=10),
        data=st.data(),
    )
    @settings(max_examples=200)
    def test_displacement_cap_holds_across_sequence(
        self, start_x: float, start_y: float, num_jumps: int, data
    ) -> None:
        """The displacement cap holds for every consecutive pair in a random sequence."""
        engine = _make_engine()

        # Initialize
        asyncio.run(_apply_gaze_cursor(engine, start_x, start_y, 0.9))
        max_allowed = 0.05 * SCREEN_DIAG

        for _ in range(num_jumps):
            prev_ema = engine._gaze_cursor_ema
            next_x = data.draw(norm_coord_st)
            next_y = data.draw(norm_coord_st)
            asyncio.run(_apply_gaze_cursor(engine, next_x, next_y, 0.9))
            new_ema = engine._gaze_cursor_ema

            dx = (new_ema[0] - prev_ema[0]) * SCREEN_W
            dy = (new_ema[1] - prev_ema[1]) * SCREEN_H
            displacement_px = math.hypot(dx, dy)

            assert displacement_px <= max_allowed + 1e-6

    @given(
        start_x=st.floats(min_value=0.0, max_value=0.1, allow_nan=False, allow_infinity=False),
        start_y=st.floats(min_value=0.0, max_value=0.1, allow_nan=False, allow_infinity=False),
    )
    @settings(max_examples=100)
    def test_maximum_possible_jump_is_capped(self, start_x: float, start_y: float) -> None:
        """A jump from near (0,0) to (1,1) — the largest possible — is still capped."""
        engine = _make_engine()

        asyncio.run(_apply_gaze_cursor(engine, start_x, start_y, 0.9))
        prev_ema = engine._gaze_cursor_ema

        # Jump to opposite corner
        asyncio.run(_apply_gaze_cursor(engine, 1.0, 1.0, 0.9))
        new_ema = engine._gaze_cursor_ema

        dx = (new_ema[0] - prev_ema[0]) * SCREEN_W
        dy = (new_ema[1] - prev_ema[1]) * SCREEN_H
        displacement_px = math.hypot(dx, dy)

        max_allowed = 0.05 * SCREEN_DIAG
        assert displacement_px <= max_allowed + 1e-6


# ---------------------------------------------------------------------------
# Property 14: Gaze-to-cursor suppresses tilt and head
# ---------------------------------------------------------------------------

class TestProperty14SuppressTiltAndHead:
    """Property 14: For any tick where Gaze_Cursor_Mode is enabled, tilt events
    and head tracking events SHALL not cause any cursor movement, even when gaze
    tracking is temporarily lost.

    **Validates: Requirements 5.2**
    """

    @given(
        rx=tilt_st,
        ry=tilt_st,
    )
    @settings(max_examples=200)
    def test_tilt_suppressed_when_gaze_cursor_active(self, rx: float, ry: float) -> None:
        """Tilt events produce no cursor movement when gaze-to-cursor mode is enabled."""
        engine = _make_engine(gaze_cursor_mode=True)
        engine.on_tilt(rx, ry)

        cmd = asyncio.run(_tick_and_capture(engine))

        # No command should be emitted for tilt
        assert cmd is None, (
            f"Tilt ({rx}, {ry}) should be suppressed but produced Command: {cmd}"
        )
        # Tilt slot should be consumed (set to None)
        assert engine._tilt is None

    @given(
        pitch=head_st,
        yaw=head_st,
    )
    @settings(max_examples=200)
    def test_head_suppressed_when_gaze_cursor_active(self, pitch: float, yaw: float) -> None:
        """Head tracking events produce no cursor movement when gaze-to-cursor mode is enabled."""
        engine = _make_engine(gaze_cursor_mode=True)
        engine.on_head(pitch, yaw)

        cmd = asyncio.run(_tick_and_capture(engine))

        # No command should be emitted for head
        assert cmd is None, (
            f"Head ({pitch}, {yaw}) should be suppressed but produced Command: {cmd}"
        )
        # Head slot should be consumed (set to None)
        assert engine._head is None

    @given(
        rx=tilt_st,
        ry=tilt_st,
        pitch=head_st,
        yaw=head_st,
    )
    @settings(max_examples=200)
    def test_both_suppressed_even_when_gaze_lost(
        self, rx: float, ry: float, pitch: float, yaw: float
    ) -> None:
        """Both tilt and head are suppressed even when gaze tracking is temporarily lost
        (no recent gaze data, EMA is stale)."""
        engine = _make_engine(gaze_cursor_mode=True)
        # Don't provide any gaze data — simulates gaze lost
        engine._gaze_cursor_ema = None
        engine._gaze_cursor_last_seen = 0.0

        # Feed tilt
        engine.on_tilt(rx, ry)
        cmd = asyncio.run(_tick_and_capture(engine))
        assert cmd is None
        assert engine._tilt is None

        # Reset coordinator mock
        engine._coordinator.route.reset_mock()

        # Feed head
        engine.on_head(pitch, yaw)
        cmd = asyncio.run(_tick_and_capture(engine))
        assert cmd is None
        assert engine._head is None

    @given(
        rx=tilt_st,
        ry=tilt_st,
    )
    @settings(max_examples=100)
    def test_tilt_works_when_gaze_cursor_disabled(self, rx: float, ry: float) -> None:
        """Tilt events DO move cursor when gaze-to-cursor mode is disabled."""
        # dead_zone_inner=0.05, outer=0.125; magnitude must exceed outer (0.125)
        # to guarantee movement after the smoothstep ramp and int-rounding
        assume(abs(rx) > 0.15 or abs(ry) > 0.15)

        engine = _make_engine(gaze_cursor_mode=False)
        engine.on_tilt(rx, ry)

        with patch("pyautogui.moveRel") as mock_move:
            asyncio.run(engine._tick())

        # pyautogui.moveRel should have been called
        assert mock_move.called, (
            f"Tilt ({rx}, {ry}) should move cursor when gaze_cursor_mode is disabled"
        )


# ---------------------------------------------------------------------------
# Property 15: Low-confidence gaze holds cursor position
# ---------------------------------------------------------------------------

class TestProperty15LowConfidenceHoldsCursor:
    """Property 15: For any gaze sample with confidence below the configured minimum
    threshold (default 0.55) while Gaze_Cursor_Mode is enabled, the cursor position
    SHALL remain at its last known position (unchanged from the previous tick).

    **Validates: Requirements 5.5**
    """

    @given(
        init_x=norm_coord_st,
        init_y=norm_coord_st,
        low_conf=low_conf_st,
        new_x=norm_coord_st,
        new_y=norm_coord_st,
    )
    @settings(max_examples=300)
    def test_low_confidence_does_not_update_ema(
        self, init_x: float, init_y: float, low_conf: float, new_x: float, new_y: float
    ) -> None:
        """When confidence is below threshold, EMA position does not change."""
        engine = _make_engine()

        # Establish initial position with high confidence
        asyncio.run(_apply_gaze_cursor(engine, init_x, init_y, 0.9))
        ema_before = engine._gaze_cursor_ema

        # Feed low-confidence gaze — should not update EMA
        asyncio.run(_apply_gaze_cursor(engine, new_x, new_y, low_conf))
        ema_after = engine._gaze_cursor_ema

        assert ema_after == ema_before, (
            f"EMA changed from {ema_before} to {ema_after} with low confidence {low_conf}"
        )

    @given(
        init_x=norm_coord_st,
        init_y=norm_coord_st,
        low_conf=low_conf_st,
    )
    @settings(max_examples=200)
    def test_low_confidence_holds_last_pixel_position(
        self, init_x: float, init_y: float, low_conf: float
    ) -> None:
        """The pixel cursor position remains unchanged after low-confidence samples."""
        engine = _make_engine()

        # Establish position
        asyncio.run(_apply_gaze_cursor(engine, init_x, init_y, 0.9))
        last_pos = engine._gaze_cursor_last

        # Feed low-confidence — cursor should not move
        with patch("pyautogui.moveTo") as mock_move:
            asyncio.run(engine._apply_gaze_cursor(0.5, 0.5, low_conf))

        # moveTo should NOT have been called
        assert not mock_move.called, (
            f"pyautogui.moveTo called with low confidence {low_conf}"
        )
        # Last position unchanged
        assert engine._gaze_cursor_last == last_pos

    @given(
        init_x=norm_coord_st,
        init_y=norm_coord_st,
        num_low_samples=st.integers(min_value=1, max_value=5),
        data=st.data(),
    )
    @settings(max_examples=200)
    def test_multiple_low_confidence_samples_hold_position(
        self, init_x: float, init_y: float, num_low_samples: int, data
    ) -> None:
        """Multiple consecutive low-confidence samples all hold the cursor."""
        engine = _make_engine()

        asyncio.run(_apply_gaze_cursor(engine, init_x, init_y, 0.9))
        ema_after_init = engine._gaze_cursor_ema

        for _ in range(num_low_samples):
            low_conf = data.draw(low_conf_st)
            new_x = data.draw(norm_coord_st)
            new_y = data.draw(norm_coord_st)
            asyncio.run(_apply_gaze_cursor(engine, new_x, new_y, low_conf))

        assert engine._gaze_cursor_ema == ema_after_init

    @given(
        init_x=norm_coord_st,
        init_y=norm_coord_st,
        high_conf=high_conf_st,
        new_x=norm_coord_st,
        new_y=norm_coord_st,
    )
    @settings(max_examples=200)
    def test_high_confidence_does_update_ema(
        self, init_x: float, init_y: float, high_conf: float, new_x: float, new_y: float
    ) -> None:
        """When confidence is at or above threshold, EMA position updates (contrast test)."""
        assume(abs(new_x - init_x) > 0.01 or abs(new_y - init_y) > 0.01)  # meaningful delta

        engine = _make_engine()

        asyncio.run(_apply_gaze_cursor(engine, init_x, init_y, 0.9))
        ema_before = engine._gaze_cursor_ema

        asyncio.run(_apply_gaze_cursor(engine, new_x, new_y, high_conf))
        ema_after = engine._gaze_cursor_ema

        # EMA should have changed (unless the EMA computation happens to land on same value)
        # With alpha=0.3 and different inputs, it will change
        if (new_x != init_x or new_y != init_y):
            assert ema_after != ema_before or (new_x == init_x and new_y == init_y), (
                f"EMA should update with high confidence {high_conf}"
            )


# ---------------------------------------------------------------------------
# Property 16: Dwell fires at smoothed cursor position
# ---------------------------------------------------------------------------

class TestProperty16DwellAtSmoothedPosition:
    """Property 16: For any dwell event that fires while Gaze_Cursor_Mode is enabled,
    the gaze_coords in the emitted Command SHALL equal the current EMA-smoothed cursor
    position, not the raw gaze position.

    **Validates: Requirements 5.7**
    """

    @given(
        ema_x=norm_coord_st,
        ema_y=norm_coord_st,
        raw_x=norm_coord_st,
        raw_y=norm_coord_st,
        action_type=st.sampled_from(["left_click", "right_click", "double_click"]),
    )
    @settings(max_examples=300)
    def test_dwell_uses_ema_not_raw(
        self, ema_x: float, ema_y: float, raw_x: float, raw_y: float, action_type: str
    ) -> None:
        """Dwell event uses EMA-smoothed position, not the raw gaze coordinates."""
        engine = _make_engine(gaze_cursor_mode=True)

        # Set EMA state directly (simulating prior gaze-to-cursor smoothing)
        engine._gaze_cursor_ema = (ema_x, ema_y)

        # Fire dwell at raw position (which should be overridden by EMA)
        engine.on_gaze_dwell(raw_x, raw_y, action_type)

        cmd = asyncio.run(_tick_and_capture(engine))

        assert cmd is not None, "Dwell should emit a Command"

        # Expected coords from EMA, not raw
        expected_px_x = round(max(0.0, min(1.0, ema_x)) * SCREEN_W)
        expected_px_y = round(max(0.0, min(1.0, ema_y)) * SCREEN_H)

        assert cmd.gaze_coords == (expected_px_x, expected_px_y), (
            f"Expected gaze_coords from EMA=({expected_px_x}, {expected_px_y}), "
            f"got {cmd.gaze_coords}. Raw was ({raw_x}, {raw_y}), EMA was ({ema_x}, {ema_y})"
        )

    @given(
        raw_x=norm_coord_st,
        raw_y=norm_coord_st,
        action_type=st.sampled_from(["left_click", "right_click", "double_click"]),
    )
    @settings(max_examples=200)
    def test_dwell_uses_raw_when_gaze_cursor_disabled(
        self, raw_x: float, raw_y: float, action_type: str
    ) -> None:
        """When gaze-to-cursor mode is disabled, dwell uses raw coordinates."""
        engine = _make_engine(gaze_cursor_mode=False)

        # Even if EMA has a value, it should be ignored when mode is off
        engine._gaze_cursor_ema = (0.1, 0.1)

        engine.on_gaze_dwell(raw_x, raw_y, action_type)
        cmd = asyncio.run(_tick_and_capture(engine))

        assert cmd is not None
        expected_px_x = round(max(0.0, min(1.0, raw_x)) * SCREEN_W)
        expected_px_y = round(max(0.0, min(1.0, raw_y)) * SCREEN_H)

        assert cmd.gaze_coords == (expected_px_x, expected_px_y), (
            f"With gaze_cursor_mode disabled, expected raw coords "
            f"({expected_px_x}, {expected_px_y}), got {cmd.gaze_coords}"
        )

    @given(
        init_x=norm_coord_st,
        init_y=norm_coord_st,
        gaze_x=norm_coord_st,
        gaze_y=norm_coord_st,
        raw_dwell_x=norm_coord_st,
        raw_dwell_y=norm_coord_st,
    )
    @settings(max_examples=200)
    def test_dwell_position_matches_cursor_position(
        self, init_x: float, init_y: float, gaze_x: float, gaze_y: float,
        raw_dwell_x: float, raw_dwell_y: float
    ) -> None:
        """The dwell landing position equals the EMA-smoothed cursor position
        established by prior gaze-to-cursor updates."""
        engine = _make_engine(gaze_cursor_mode=True)

        # Build up EMA state through actual gaze-to-cursor calls
        asyncio.run(_apply_gaze_cursor(engine, init_x, init_y, 0.9))
        asyncio.run(_apply_gaze_cursor(engine, gaze_x, gaze_y, 0.9))

        # Capture the current EMA state
        ema_pos = engine._gaze_cursor_ema
        assert ema_pos is not None

        # Fire dwell at arbitrary raw position
        engine.on_gaze_dwell(raw_dwell_x, raw_dwell_y, "left_click")
        cmd = asyncio.run(_tick_and_capture(engine))

        assert cmd is not None
        expected_px_x = round(max(0.0, min(1.0, ema_pos[0])) * SCREEN_W)
        expected_px_y = round(max(0.0, min(1.0, ema_pos[1])) * SCREEN_H)

        assert cmd.gaze_coords == (expected_px_x, expected_px_y)

    @given(
        ema_x=norm_coord_st,
        ema_y=norm_coord_st,
    )
    @settings(max_examples=100)
    def test_dwell_uses_raw_when_ema_is_none(self, ema_x: float, ema_y: float) -> None:
        """When EMA is None (no prior gaze data), dwell falls back to raw coordinates."""
        engine = _make_engine(gaze_cursor_mode=True)
        engine._gaze_cursor_ema = None  # No prior gaze-to-cursor data

        engine.on_gaze_dwell(ema_x, ema_y, "left_click")
        cmd = asyncio.run(_tick_and_capture(engine))

        assert cmd is not None
        # With no EMA, raw coords are used
        expected_px_x = round(max(0.0, min(1.0, ema_x)) * SCREEN_W)
        expected_px_y = round(max(0.0, min(1.0, ema_y)) * SCREEN_H)
        assert cmd.gaze_coords == (expected_px_x, expected_px_y)
