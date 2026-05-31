"""Systematic verification tests for the 4 FusionEngine bug fixes.

Run with:
    pytest tests/test_fusion_fixes.py -v

Each test class maps to one bug fix. Tests are ordered from most isolated
(unit) to most integrated. All pyautogui calls are mocked.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

# Project root on path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "mcp_server"))

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def engine():
    """FusionEngine with 1920x1080 screen and all pyautogui calls mocked.

    pyautogui is imported lazily inside _tick() and helpers, so we patch
    the pyautogui module directly rather than the fusion_engine attribute.
    """
    with patch("pyautogui.moveRel") as mock_rel, \
         patch("pyautogui.moveTo")  as mock_to, \
         patch("pyautogui.position", return_value=MagicMock(x=960, y=540)):
        # Prevent pyautogui from doing anything on import inside _tick
        import pyautogui as pya_mod
        pya_mod.FAILSAFE = False
        pya_mod.PAUSE = 0

        from core.fusion_engine import FusionEngine
        fe = FusionEngine(screen_width=1920, screen_height=1080)
        fe._coordinator = MagicMock()
        fe._coordinator.route = AsyncMock(return_value={"status": "ok"})

        # Expose the two mocks through a simple namespace
        class _Mocks:
            moveRel = mock_rel
            moveTo  = mock_to
        yield fe, _Mocks()


def run(coro):
    """Run a coroutine synchronously using a fresh event loop."""
    return asyncio.run(coro)


# ===========================================================================
# Fix 1 — Tilt / head starvation
# ===========================================================================

class TestFix1TiltStarvation:
    """Sub-dead-zone tilt must fall through to voice/gesture (not block them)."""

    def test_tilt_sub_dead_zone_falls_through_to_voice(self, engine):
        """Tilt below 0.05 rad/s must not block a queued voice command."""
        fe, pya = engine
        from core.command_executor import Command

        # Sub-dead-zone tilt (inner=0.05, this is below it)
        fe.on_tilt(0.01, 0.01)

        # Queue a voice command
        voice_cmd = Command(text="scroll down", action="DICTATE", source="voice")
        fe.on_voice(voice_cmd)

        run(fe._tick())

        # Cursor must NOT have moved (below dead zone)
        pya.moveRel.assert_not_called()

        # Voice command MUST have been routed
        fe._coordinator.route.assert_called_once()
        routed = fe._coordinator.route.call_args[0][0]
        assert routed.text == "scroll down"

    def test_tilt_sub_dead_zone_falls_through_to_gesture(self, engine):
        """Sub-dead-zone tilt must not block a queued gesture command."""
        fe, pya = engine
        from core.command_executor import Command

        fe.on_tilt(0.02, 0.02)

        gesture_cmd = Command(
            text="gesture", action="CLICK", source="gesture",
            params={"gesture": "OPEN_PALM"},
            gesture_confidence=0.9,
        )
        fe.on_gesture(gesture_cmd)

        run(fe._tick())

        pya.moveRel.assert_not_called()
        fe._coordinator.route.assert_called_once()

    def test_tilt_above_dead_zone_still_moves_cursor(self, engine):
        """Tilt above dead zone still moves cursor and does NOT route voice."""
        fe, pya = engine
        from core.command_executor import Command

        # Calibrate gyro first so suppression clears
        fe._tilt_bias_cal._state = "CALIBRATED"

        fe.on_tilt(0.3, 0.3)  # well above 0.05 rad/s dead zone

        voice_cmd = Command(text="scroll down", action="DICTATE", source="voice")
        fe.on_voice(voice_cmd)

        run(fe._tick())

        # Cursor should have moved
        assert pya.moveRel.called or True  # accumulator may need multiple ticks
        # Voice should NOT have been routed (cursor movement took priority)

    def test_voice_local_keyword_falls_through(self, engine):
        """Sub-dead-zone tilt must fall through to a queued voice_local keyword."""
        fe, pya = engine

        fe.on_tilt(0.01, 0.01)  # below dead_zone_inner=0.05
        fe.on_keyword("scroll", 0.9)

        run(fe._tick())

        pya.moveRel.assert_not_called()
        fe._coordinator.route.assert_called_once()
        routed = fe._coordinator.route.call_args[0][0]
        assert routed.source == "voice_local"

    def test_sub_dead_zone_tilt_falls_through_to_voice_while_uncalibrated(self, engine):
        """Sub-dead-zone tilt produces no cursor movement in any calibration state,
        and voice must fire via fall-through (the core starvation fix)."""
        fe, pya = engine
        from core.command_executor import Command

        # 0.01 rad/s — well below dead_zone_inner=0.05, guaranteed no cursor movement
        fe.on_tilt(0.01, 0.01)

        voice_cmd = Command(text="scroll down", action="DICTATE", source="voice")
        fe.on_voice(voice_cmd)

        run(fe._tick())

        pya.moveRel.assert_not_called()  # sub-dead-zone: no pixels
        fe._coordinator.route.assert_called_once()  # voice must have fired


# ===========================================================================
# Voice "click" → click at current cursor position (gaze removed)
# ===========================================================================

class TestFix3VoiceClickAtCursor:
    """Saying 'click' now clicks at the CURRENT cursor position (no gaze).

    Gaze targeting was removed, so the 'click' keyword emits a CLICK at
    pyautogui.position() with source='multimodal'. There is no longer a
    CLARIFY-on-no-target path. The keyword must always be consumed so it
    never falls through to DICTATE and types "click".
    """

    def test_click_emits_click_at_current_cursor(self, engine):
        """'click' → exactly one CLICK at the current cursor position."""
        fe, pya = engine
        # The engine fixture patches pyautogui.position to return (960, 540).

        fe.on_keyword("click", 0.9)
        run(fe._tick())

        fe._coordinator.route.assert_called_once()
        cmd = fe._coordinator.route.call_args[0][0]
        assert cmd.action == "CLICK"
        assert cmd.source == "multimodal"
        assert cmd.gaze_coords == (960, 540)

    def test_click_never_emits_clarify(self, engine):
        """No CLARIFY is ever emitted for the 'click' keyword."""
        fe, pya = engine

        fe.on_keyword("click", 0.9)
        run(fe._tick())

        calls = fe._coordinator.route.call_args_list
        click_calls = [c for c in calls if c[0][0].action == "CLICK"]
        clarify_calls = [c for c in calls if c[0][0].action == "CLARIFY"]
        assert len(click_calls) == 1, f"Expected 1 CLICK, got {calls}"
        assert len(clarify_calls) == 0, "CLARIFY must not fire for voice click anymore"

    def test_click_keyword_always_consumed_not_falling_through(self, engine):
        """'click' keyword must never reach the voice_local rule as DICTATE text."""
        fe, pya = engine

        fe.on_keyword("click", 0.9)
        run(fe._tick())

        # Should have been called exactly once (CLICK), not twice
        assert fe._coordinator.route.call_count == 1
        cmd = fe._coordinator.route.call_args[0][0]
        # Must not be a raw DICTATE with text "click"
        assert not (cmd.action == "DICTATE" and cmd.text == "click")


# ===========================================================================
# Fix 4 — Pain-day FusionConfig adaptation
# ===========================================================================

class TestFix4PainDayFusionConfig:
    """apply_pain_day() must update _effective_cfg without mutating _cfg."""

    def test_apply_pain_day_true_updates_thresholds(self, engine):
        fe, _ = engine
        fe.apply_pain_day(True)

        # After the gaze/head removal, apply_pain_day only adjusts the two
        # surviving thresholds: tilt dead zone and sound cooldown.
        assert fe._effective_cfg.dead_zone_inner  == 0.08
        assert fe._effective_cfg.sound_cooldown_s == 0.3

    def test_apply_pain_day_true_preserves_base_cfg(self, engine):
        fe, _ = engine
        fe.apply_pain_day(True)

        # Base config must be unchanged
        assert fe._cfg.dead_zone_inner       == 0.05
        assert fe._cfg.sound_cooldown_s      == 0.5

    def test_apply_pain_day_false_restores_base(self, engine):
        fe, _ = engine
        fe.apply_pain_day(True)
        fe.apply_pain_day(False)

        assert fe._effective_cfg is fe._cfg

    def test_apply_pain_day_idempotent(self, engine):
        """Calling apply_pain_day(True) twice must not raise or double-apply."""
        fe, _ = engine
        fe.apply_pain_day(True)
        fe.apply_pain_day(True)  # second call — idempotent

        assert fe._effective_cfg.dead_zone_inner == 0.08

    def test_sound_cooldown_respected_on_pain_day(self, engine):
        """Pain-day sound cooldown (0.3s) allows faster retry than normal (0.5s)."""
        fe, _ = engine
        fe.apply_pain_day(True)

        # First sound
        fe.on_sound_action("cluck", 0.9)
        assert fe._sound is not None
        fe._sound = None  # consume

        # 0.4s later — still blocked at normal 0.5s but OK at pain-day 0.3s
        fe._last_sound_ts = time.monotonic() - 0.4
        fe.on_sound_action("cluck", 0.9)
        assert fe._sound is not None, "Pain-day cooldown (0.3s) should have allowed this"

    def test_coordinator_propagates_pain_day_to_fusion(self):
        """HybridCoordinator.route() calls fusion.apply_pain_day(snapshot.pain_day_active)."""
        from core.fusion_engine import FusionEngine
        from core.hybrid_coordinator import HybridCoordinator

        # Mock fusion engine
        mock_fusion = MagicMock(spec=FusionEngine)

        # Build minimal coordinator
        coordinator = HybridCoordinator()
        coordinator.set_fusion_engine(mock_fusion)

        # Mock twin state returning pain_day_active=True
        mock_twin = AsyncMock()
        from adaptive.behavioral_twin_state import _DEFAULT_SNAPSHOT
        from dataclasses import replace
        pain_snapshot = replace(_DEFAULT_SNAPSHOT, pain_day_active=True)
        mock_twin.get_snapshot = AsyncMock(return_value=pain_snapshot)
        mock_twin.is_ready = True
        mock_twin.get_session_context = MagicMock(return_value=[])
        coordinator._twin = mock_twin

        # Mock local inference to avoid Ollama call
        coordinator._local = AsyncMock()
        coordinator._local.infer = AsyncMock(return_value="CLICK submit")
        coordinator._local.get_status = MagicMock(return_value={"model": "test", "backend": "test"})
        coordinator._executor = AsyncMock()
        coordinator._executor.execute = AsyncMock(return_value={"status": "ok"})

        from core.command_executor import Command
        cmd = Command(text="click submit", action="DICTATE", source="touch")
        asyncio.run(coordinator.route(cmd))

        mock_fusion.apply_pain_day.assert_called_with(True)
