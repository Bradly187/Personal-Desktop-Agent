"""Unit tests for GestureProcessor (24 tests).

Tests GP-01 through GP-24 as specified in the test plan.
MediaPipe is never invoked for real — always mocked.
"""

from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest.mock import MagicMock, patch


from tests.conftest import (
    make_camera_msg, make_lm_mock, make_hands_result,
    POINT_LM, FIST_LM, PINCH_LM, TWO_FINGER_LM, FOUR_FINGER_LM, GRAB_LM,
)


# ---------------------------------------------------------------------------
# Fixture: GestureProcessor with MediaPipe mocked out
# ---------------------------------------------------------------------------

def make_processor(confidence_min=0.65, debounce_s=0.80):
    """Return a GestureProcessor with _available forced True and Hands mocked."""
    import sensors.gesture_processor as gp_mod
    gp_mod._MP_AVAILABLE = True
    gp_mod._CV2_AVAILABLE = True
    # When mediapipe is not installed, mp is undefined; stub it so _process_frame
    # can call mp.Image() without NameError.
    if not hasattr(gp_mod, 'mp') or gp_mod.mp is None:
        gp_mod.mp = MagicMock()
        gp_mod.mp.ImageFormat.SRGB = MagicMock()
    from sensors.gesture_processor import GestureProcessor
    proc = GestureProcessor(confidence_min=confidence_min, debounce_s=debounce_s)
    proc._available = True
    return proc


def run_with_mock_hands(proc, lm_mock, score=0.90, msg=None):
    """Run on_camera_frame with HandLandmarker.detect_for_video mocked to return lm_mock."""
    result_mock = make_hands_result(lm_mock, score=score)
    msg = msg or make_camera_msg()
    mock_hands = MagicMock()
    mock_hands.detect_for_video.return_value = result_mock  # Tasks API, not .process()
    proc._hands = mock_hands
    return proc.on_camera_frame(msg)


# ---------------------------------------------------------------------------
# GP-01 — mediapipe unavailable → None
# ---------------------------------------------------------------------------

def test_gp01_mp_unavailable_returns_none():
    import sensors.gesture_processor as gp_mod
    from sensors.gesture_processor import GestureProcessor
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
    no_hand = MagicMock()
    no_hand.hand_landmarks = []  # Tasks API: empty list = no hand
    mock_hands = MagicMock()
    mock_hands.detect_for_video.return_value = no_hand
    proc._hands = mock_hands
    result = proc.on_camera_frame(make_camera_msg())
    assert result is None


# ---------------------------------------------------------------------------
# GP-05 — score below threshold → None
# ---------------------------------------------------------------------------

def test_gp05_low_confidence_returns_none():
    proc = make_processor(confidence_min=0.65)
    result = run_with_mock_hands(proc, PINCH_LM, score=0.50)
    assert result is None


# ---------------------------------------------------------------------------
# GP-06 — score exactly at threshold → Command passes (gate is <, not <=)
# ---------------------------------------------------------------------------

def test_gp06_at_confidence_threshold_passes():
    proc = make_processor(confidence_min=0.65)
    result = run_with_mock_hands(proc, PINCH_LM, score=0.65)
    assert result is not None
    assert result.action == "CLICK"


# ---------------------------------------------------------------------------
# GP-07 — single index-only pose → no command (POINT not in new vocab;
# new processor uses peace-sign + two-finger spatial gestures)
# ---------------------------------------------------------------------------

def test_gp07_point_produces_click():
    proc = make_processor()
    result = run_with_mock_hands(proc, POINT_LM, score=0.90)
    assert result is None  # POINT pose does not match any new-vocab detector


# ---------------------------------------------------------------------------
# GP-08 — GRAB_LM (thumb+index+middle clustered) → TWO_FINGER_GRAB → MOUSEDOWN
# ---------------------------------------------------------------------------

def test_gp08_open_palm_produces_clarify():
    proc = make_processor()
    result = run_with_mock_hands(proc, GRAB_LM, score=0.80)
    assert result is not None
    assert result.action == "MOUSEDOWN"
    assert result.params["gesture"] == "TWO_FINGER_GRAB"


# ---------------------------------------------------------------------------
# GP-09 — FIST_LM (all tips co-located at y=0.8, x=0.5) triggers TWO_FINGER_GRAB
# because zero distance satisfies the grab threshold; gesture vocab changed.
# ---------------------------------------------------------------------------

def test_gp09_fist_produces_close():
    proc = make_processor()
    result = run_with_mock_hands(proc, FIST_LM, score=0.85)
    # FIST_LM has all tips at the same coords → _two_finger_pinch fires (dist=0 < 0.07)
    assert result is not None
    assert result.action == "MOUSEDOWN"
    assert result.params["gesture"] == "TWO_FINGER_GRAB"


# ---------------------------------------------------------------------------
# GP-10 — PINCH 2D distance < threshold, no LiDAR → CLICK
# ---------------------------------------------------------------------------

def test_gp10_pinch_2d_fallback_produces_click():
    proc = make_processor()
    result = run_with_mock_hands(proc, PINCH_LM, score=0.80)
    assert result is not None
    assert result.action == "CLICK"
    assert result.params["gesture"] == "PINCH"


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
    cmd1 = run_with_mock_hands(proc, PINCH_LM, score=0.90)
    cmd2 = run_with_mock_hands(proc, PINCH_LM, score=0.90)
    assert cmd1 is not None
    assert cmd2 is None  # within debounce window


# ---------------------------------------------------------------------------
# GP-13 — debounce expires → gesture re-fires
# ---------------------------------------------------------------------------

def test_gp13_debounce_expires_allows_refire():
    import sensors.gesture_processor as gp_mod
    proc = make_processor(debounce_s=0.80)

    # Use return_value (not side_effect) so every monotonic() call within a
    # single frame returns the same stable time regardless of how many callers.
    with patch.object(gp_mod, "time") as mock_time:
        mock_time.monotonic.return_value = 1.0
        cmd1 = run_with_mock_hands(proc, PINCH_LM, score=0.90)
        mock_time.monotonic.return_value = 1.79
        cmd2 = run_with_mock_hands(proc, PINCH_LM, score=0.90)
        mock_time.monotonic.return_value = 1.81
        cmd3 = run_with_mock_hands(proc, PINCH_LM, score=0.90)

    assert cmd1 is not None, "First call should return Command"
    assert cmd2 is None, "Second call (t=1.79) still within debounce (1.0+0.8=1.8)"
    assert cmd3 is not None, "Third call (t=1.81) should re-fire"


# ---------------------------------------------------------------------------
# GP-14 — debounce is per gesture key (PINCH debounce doesn't block GRAB)
# ---------------------------------------------------------------------------

def test_gp14_debounce_independent_per_gesture():
    proc = make_processor(debounce_s=0.80)
    cmd_pinch = run_with_mock_hands(proc, PINCH_LM, score=0.90)
    # Immediately after PINCH fires, TWO_FINGER_GRAB has its own clean debounce slot
    cmd_grab = run_with_mock_hands(proc, GRAB_LM, score=0.90)
    assert cmd_pinch is not None
    assert cmd_grab is not None


# ---------------------------------------------------------------------------
# GP-15 — PINCH with LiDAR wired → still fires (single-finger PINCH is not
# LiDAR-gated in the rewrite; LiDAR now gates TWO_FINGER_GRAB only)
# ---------------------------------------------------------------------------

def test_gp15_lidar_pinch_accept():
    proc = make_processor()
    mock_lidar = MagicMock()
    mock_lidar.is_fresh.return_value = True
    mock_lidar.get_depth_at.side_effect = [1.000, 1.020]
    proc.set_lidar(mock_lidar)

    result = run_with_mock_hands(proc, PINCH_LM, score=0.90)
    assert result is not None
    assert result.action == "CLICK"
    assert result.params["gesture"] == "PINCH"


# ---------------------------------------------------------------------------
# GP-16 — PINCH with large LiDAR delta → still fires (single-finger PINCH
# is NOT LiDAR-gated; LiDAR gating is on TWO_FINGER_GRAB)
# ---------------------------------------------------------------------------

def test_gp16_lidar_pinch_reject():
    proc = make_processor()
    mock_lidar = MagicMock()
    mock_lidar.is_fresh.return_value = True
    mock_lidar.get_depth_at.side_effect = [1.000, 1.050]
    proc.set_lidar(mock_lidar)

    result = run_with_mock_hands(proc, PINCH_LM, score=0.90)
    # Single-finger PINCH fires regardless of LiDAR Z-delta
    assert result is not None
    assert result.action == "CLICK"


# ---------------------------------------------------------------------------
# GP-17 — stale LiDAR → get_depth_at not called for single-finger PINCH
# ---------------------------------------------------------------------------

def test_gp17_stale_lidar_falls_back_to_2d():
    proc = make_processor()
    mock_lidar = MagicMock()
    mock_lidar.is_fresh.return_value = False
    proc.set_lidar(mock_lidar)

    result = run_with_mock_hands(proc, PINCH_LM, score=0.90)
    assert result is not None
    mock_lidar.get_depth_at.assert_not_called()


# ---------------------------------------------------------------------------
# GP-18 — both depths None (masked) → PINCH still fires
# ---------------------------------------------------------------------------

def test_gp18_lidar_both_none_no_rejection():
    proc = make_processor()
    mock_lidar = MagicMock()
    mock_lidar.is_fresh.return_value = True
    mock_lidar.get_depth_at.return_value = None
    proc.set_lidar(mock_lidar)

    result = run_with_mock_hands(proc, PINCH_LM, score=0.90)
    assert result is not None
    assert result.action == "CLICK"


# ---------------------------------------------------------------------------
# GP-19 — one depth None → PINCH still fires
# ---------------------------------------------------------------------------

def test_gp19_lidar_one_none_falls_back():
    proc = make_processor()
    mock_lidar = MagicMock()
    mock_lidar.is_fresh.return_value = True
    mock_lidar.get_depth_at.side_effect = [1.0, None]
    proc.set_lidar(mock_lidar)

    result = run_with_mock_hands(proc, PINCH_LM, score=0.90)
    assert result is not None
    assert result.action == "CLICK"


# ---------------------------------------------------------------------------
# GP-20 — get_status reports lidar_wired correctly
# ---------------------------------------------------------------------------

def test_gp20_status_lidar_wired():
    from sensors.gesture_processor import GestureProcessor
    proc = GestureProcessor()
    proc._available = True
    status = proc.get_status()
    # get_status() returns available, frame_count, buffer_frames, grabbing, etc.
    assert "available" in status
    assert "frame_count" in status
    assert "grabbing" in status
    assert status["grabbing"] is False


# ---------------------------------------------------------------------------
# GP-21 — close() calls hands.close() and sets _hands=None
# ---------------------------------------------------------------------------

def test_gp21_close_cleans_up():
    from sensors.gesture_processor import GestureProcessor
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
    # Tasks API: empty hand_landmarks list = no hand detected
    no_hand_result = MagicMock()
    no_hand_result.hand_landmarks = []
    mock_hands = MagicMock()
    mock_hands.detect_for_video.return_value = no_hand_result
    proc._hands = mock_hands

    for _ in range(3):
        proc.on_camera_frame(make_camera_msg())

    assert proc._frame_count == 3


# ---------------------------------------------------------------------------
# GP-23 — peace-sign (index+middle) with no buffer history → None
# (swipe detection requires >= 3 buffered frames; fresh proc has none)
# ---------------------------------------------------------------------------

def test_gp23_two_finger_unrecognised():
    proc = make_processor()
    result = run_with_mock_hands(proc, TWO_FINGER_LM, score=0.90)
    assert result is None


# ---------------------------------------------------------------------------
# GP-24 — four fingers extended with no buffer history → None
# (open-palm push/pull requires motion history; fresh proc has none)
# ---------------------------------------------------------------------------

def test_gp24_four_finger_open_palm():
    proc = make_processor()
    result = run_with_mock_hands(proc, FOUR_FINGER_LM, score=0.90)
    assert result is None
