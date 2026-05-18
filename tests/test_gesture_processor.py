"""Unit tests for GestureProcessor (24 tests).

Tests GP-01 through GP-24 as specified in the test plan.
MediaPipe is never invoked for real — always mocked.
"""

from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from tests.conftest import (
    make_camera_msg, make_lm_mock, make_hands_result,
    POINT_LM, FIST_LM, PALM_LM, PINCH_LM, TWO_FINGER_LM, FOUR_FINGER_LM,
)


# ---------------------------------------------------------------------------
# Fixture: GestureProcessor with MediaPipe mocked out
# ---------------------------------------------------------------------------

def make_processor(confidence_min=0.65, debounce_s=0.80):
    """Return a GestureProcessor with _available forced True and Hands mocked."""
    import gesture_processor as gp_mod
    # Ensure module flags are set for the import
    gp_mod._MP_AVAILABLE = True
    gp_mod._CV2_AVAILABLE = True
    from gesture_processor import GestureProcessor
    proc = GestureProcessor(confidence_min=confidence_min, debounce_s=debounce_s)
    proc._available = True
    return proc


def run_with_mock_hands(proc, lm_mock, score=0.90, msg=None):
    """Run on_camera_frame with MediaPipe hands.process mocked to return lm_mock."""
    result_mock = make_hands_result(lm_mock, score=score)
    msg = msg or make_camera_msg()
    mock_hands = MagicMock()
    mock_hands.process.return_value = result_mock
    proc._hands = mock_hands
    return proc.on_camera_frame(msg)


# ---------------------------------------------------------------------------
# GP-01 — mediapipe unavailable → None
# ---------------------------------------------------------------------------

def test_gp01_mp_unavailable_returns_none():
    import gesture_processor as gp_mod
    from gesture_processor import GestureProcessor
    original = gp_mod._MP_AVAILABLE
    try:
        gp_mod._MP_AVAILABLE = False
        proc = GestureProcessor()
        proc._available = False
        result = proc.on_camera_frame(make_camera_msg())
        assert result is None
    finally:
        gp_mod._MP_AVAILABLE = original


# ---------------------------------------------------------------------------
# GP-02 — missing image_b64 key → None
# ---------------------------------------------------------------------------

def test_gp02_missing_image_b64_returns_none():
    proc = make_processor()
    result = proc.on_camera_frame({"type": "camera_frame", "ts": 0})
    assert result is None


# ---------------------------------------------------------------------------
# GP-03 — corrupt JPEG bytes → None (no crash)
# ---------------------------------------------------------------------------

def test_gp03_corrupt_jpeg_returns_none():
    import base64
    proc = make_processor()
    bad_b64 = base64.b64encode(b"not a valid image at all").decode()
    msg = {"type": "camera_frame", "ts": 0, "image_b64": bad_b64}
    result = proc.on_camera_frame(msg)
    assert result is None


# ---------------------------------------------------------------------------
# GP-04 — no hands detected → None
# ---------------------------------------------------------------------------

def test_gp04_no_hand_detected_returns_none():
    proc = make_processor()
    mock_hands = MagicMock()
    mock_hands.process.return_value = MagicMock(multi_hand_landmarks=None, multi_handedness=None)
    proc._hands = mock_hands
    result = proc.on_camera_frame(make_camera_msg())
    assert result is None


# ---------------------------------------------------------------------------
# GP-05 — score below threshold → None
# ---------------------------------------------------------------------------

def test_gp05_low_confidence_returns_none():
    proc = make_processor(confidence_min=0.65)
    result = run_with_mock_hands(proc, POINT_LM, score=0.50)
    assert result is None


# ---------------------------------------------------------------------------
# GP-06 — score exactly at threshold → Command passes (gate is <, not <=)
# ---------------------------------------------------------------------------

def test_gp06_at_confidence_threshold_passes():
    proc = make_processor(confidence_min=0.65)
    result = run_with_mock_hands(proc, POINT_LM, score=0.65)
    assert result is not None
    assert result.action == "CLICK"


# ---------------------------------------------------------------------------
# GP-07 — POINT → CLICK
# ---------------------------------------------------------------------------

def test_gp07_point_produces_click():
    proc = make_processor()
    result = run_with_mock_hands(proc, POINT_LM, score=0.90)
    assert result is not None
    assert result.action == "CLICK"
    assert result.params["gesture"] == "POINT"
    assert result.source == "gesture"
    assert abs(result.gesture_confidence - 0.90) < 1e-4


# ---------------------------------------------------------------------------
# GP-08 — OPEN_PALM → CLARIFY
# ---------------------------------------------------------------------------

def test_gp08_open_palm_produces_clarify():
    proc = make_processor()
    result = run_with_mock_hands(proc, PALM_LM, score=0.80)
    assert result is not None
    assert result.action == "CLARIFY"
    assert result.params["gesture"] == "OPEN_PALM"


# ---------------------------------------------------------------------------
# GP-09 — FIST → CLOSE
# ---------------------------------------------------------------------------

def test_gp09_fist_produces_close():
    proc = make_processor()
    result = run_with_mock_hands(proc, FIST_LM, score=0.85)
    assert result is not None
    assert result.action == "CLOSE"
    assert result.params["gesture"] == "FIST"


# ---------------------------------------------------------------------------
# GP-10 — PINCH 2D distance < threshold, no LiDAR → CLICK
# ---------------------------------------------------------------------------

def test_gp10_pinch_2d_fallback_produces_click():
    proc = make_processor()
    result = run_with_mock_hands(proc, PINCH_LM, score=0.80)
    assert result is not None
    assert result.action == "CLICK"
    assert result.params["gesture"] == "PINCH"
    assert result.params["pinch_mm"] is None


# ---------------------------------------------------------------------------
# GP-11 — PINCH 2D distance > threshold, no LiDAR → None
# ---------------------------------------------------------------------------

def test_gp11_pinch_2d_too_far_returns_none():
    proc = make_processor()
    # Thumb extended (y=0.4 < MCP 0.5), index NOT extended.
    # middle/ring/pinky curled. n_ext=1 → reaches PINCH branch.
    # But thumb(x=0.2) vs index(x=0.8): dist ≈ 0.61 >> 0.06 threshold → not PINCH → None.
    far_pinch_lm = make_lm_mock(
        {4: 0.4, 8: 0.5, 12: 0.7, 16: 0.7, 20: 0.7},
        {4: 0.2, 8: 0.8},
    )
    result = run_with_mock_hands(proc, far_pinch_lm, score=0.90)
    assert result is None


# ---------------------------------------------------------------------------
# GP-12 — debounce suppresses second call within 800 ms
# ---------------------------------------------------------------------------

def test_gp12_debounce_suppresses_second_call():
    proc = make_processor(debounce_s=0.80)
    cmd1 = run_with_mock_hands(proc, POINT_LM, score=0.90)
    # No time advance → still within debounce window
    cmd2 = run_with_mock_hands(proc, POINT_LM, score=0.90)
    assert cmd1 is not None
    assert cmd2 is None


# ---------------------------------------------------------------------------
# GP-13 — debounce expires → gesture re-fires
# ---------------------------------------------------------------------------

def test_gp13_debounce_expires_allows_refire():
    import gesture_processor as gp_mod
    proc = make_processor(debounce_s=0.80)
    # Start at 1.0 so first call (1.0 - 0.0 = 1.0 > 0.80) passes debounce.
    t_values = iter([1.0, 1.79, 1.81])

    with patch.object(gp_mod, "time") as mock_time:
        mock_time.monotonic.side_effect = lambda: next(t_values)
        cmd1 = run_with_mock_hands(proc, POINT_LM, score=0.90)
        cmd2 = run_with_mock_hands(proc, POINT_LM, score=0.90)
        cmd3 = run_with_mock_hands(proc, POINT_LM, score=0.90)

    assert cmd1 is not None, "First call should return Command"
    assert cmd2 is None, "Second call (t=0.79) still within debounce"
    assert cmd3 is not None, "Third call (t=0.81) should re-fire"


# ---------------------------------------------------------------------------
# GP-14 — debounce is per gesture (POINT then FIST both pass)
# ---------------------------------------------------------------------------

def test_gp14_debounce_independent_per_gesture():
    proc = make_processor(debounce_s=0.80)
    cmd_point = run_with_mock_hands(proc, POINT_LM, score=0.90)
    cmd_fist = run_with_mock_hands(proc, FIST_LM, score=0.90)
    assert cmd_point is not None
    assert cmd_fist is not None


# ---------------------------------------------------------------------------
# GP-15 — PINCH + fresh LiDAR + 20mm → Command with pinch_mm≈20
# ---------------------------------------------------------------------------

def test_gp15_lidar_pinch_accept():
    proc = make_processor()
    mock_lidar = MagicMock()
    mock_lidar.is_fresh.return_value = True
    mock_lidar.get_depth_at.side_effect = [1.000, 1.020]  # 20mm diff
    proc.set_lidar(mock_lidar)

    result = run_with_mock_hands(proc, PINCH_LM, score=0.90)
    assert result is not None
    assert result.action == "CLICK"
    assert result.params["pinch_mm"] is not None
    assert abs(result.params["pinch_mm"] - 20.0) < 1.0


# ---------------------------------------------------------------------------
# GP-16 — PINCH + fresh LiDAR + 50mm → rejected (None)
# ---------------------------------------------------------------------------

def test_gp16_lidar_pinch_reject():
    proc = make_processor()
    mock_lidar = MagicMock()
    mock_lidar.is_fresh.return_value = True
    mock_lidar.get_depth_at.side_effect = [1.000, 1.050]  # 50mm diff
    proc.set_lidar(mock_lidar)

    result = run_with_mock_hands(proc, PINCH_LM, score=0.90)
    assert result is None


# ---------------------------------------------------------------------------
# GP-17 — stale LiDAR → 2D fallback, get_depth_at never called
# ---------------------------------------------------------------------------

def test_gp17_stale_lidar_falls_back_to_2d():
    proc = make_processor()
    mock_lidar = MagicMock()
    mock_lidar.is_fresh.return_value = False
    proc.set_lidar(mock_lidar)

    result = run_with_mock_hands(proc, PINCH_LM, score=0.90)
    assert result is not None
    mock_lidar.get_depth_at.assert_not_called()
    assert result.params["pinch_mm"] is None


# ---------------------------------------------------------------------------
# GP-18 — both depths None (masked) → Command, no rejection
# ---------------------------------------------------------------------------

def test_gp18_lidar_both_none_no_rejection():
    proc = make_processor()
    mock_lidar = MagicMock()
    mock_lidar.is_fresh.return_value = True
    mock_lidar.get_depth_at.return_value = None  # masked region
    proc.set_lidar(mock_lidar)

    result = run_with_mock_hands(proc, PINCH_LM, score=0.90)
    assert result is not None
    assert result.params["pinch_mm"] is None


# ---------------------------------------------------------------------------
# GP-19 — one depth None → Command, no pinch_mm
# ---------------------------------------------------------------------------

def test_gp19_lidar_one_none_falls_back():
    proc = make_processor()
    mock_lidar = MagicMock()
    mock_lidar.is_fresh.return_value = True
    mock_lidar.get_depth_at.side_effect = [1.0, None]  # index OK, thumb masked
    proc.set_lidar(mock_lidar)

    result = run_with_mock_hands(proc, PINCH_LM, score=0.90)
    assert result is not None
    assert result.params["pinch_mm"] is None


# ---------------------------------------------------------------------------
# GP-20 — get_status reports lidar_wired correctly
# ---------------------------------------------------------------------------

def test_gp20_status_lidar_wired():
    from gesture_processor import GestureProcessor
    proc = GestureProcessor()
    proc._available = True
    assert proc.get_status()["lidar_wired"] is False
    proc.set_lidar(MagicMock())
    assert proc.get_status()["lidar_wired"] is True


# ---------------------------------------------------------------------------
# GP-21 — close() calls hands.close() and sets _hands=None
# ---------------------------------------------------------------------------

def test_gp21_close_cleans_up():
    from gesture_processor import GestureProcessor
    proc = GestureProcessor()
    mock_hands = MagicMock()
    proc._hands = mock_hands
    proc.close()
    mock_hands.close.assert_called_once()
    assert proc._hands is None


# ---------------------------------------------------------------------------
# GP-22 — frame_count increments on valid JPEG even with no hand
# ---------------------------------------------------------------------------

def test_gp22_frame_count_increments():
    proc = make_processor()
    no_hand_result = MagicMock(multi_hand_landmarks=None, multi_handedness=None)
    mock_hands = MagicMock()
    mock_hands.process.return_value = no_hand_result
    proc._hands = mock_hands

    for _ in range(3):
        proc.on_camera_frame(make_camera_msg())

    assert proc._frame_count == 3


# ---------------------------------------------------------------------------
# GP-23 — two-finger gesture → unrecognised → None
# ---------------------------------------------------------------------------

def test_gp23_two_finger_unrecognised():
    from gesture_processor import _classify_gesture
    result = _classify_gesture(TWO_FINGER_LM)
    assert result is None


# ---------------------------------------------------------------------------
# GP-24 — four fingers (no thumb) → OPEN_PALM (n_ext >= 4)
# ---------------------------------------------------------------------------

def test_gp24_four_finger_open_palm():
    from gesture_processor import _classify_gesture
    result = _classify_gesture(FOUR_FINGER_LM)
    assert result == "OPEN_PALM"
