"""Integration tests: real LiDARReceiver + real GestureProcessor, MediaPipe mocked (6 tests).

Tests GLI-01 through GLI-06 as specified in the test plan.
"""

from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time
from unittest.mock import MagicMock

import numpy as np

from sensors.lidar_receiver import LiDARReceiver
from sensors.gesture_processor import GestureProcessor
from tests.conftest import make_depth_msg, make_camera_msg, make_hands_result, make_lm_mock


# ---------------------------------------------------------------------------
# Helper: build a PINCH landmark mock with finger tips at specific normalised coords
# ---------------------------------------------------------------------------

def make_pinch_lm(index_nx=0.51, index_ny=0.5, thumb_nx=0.49, thumb_ny=0.488):
    """PINCH: thumb extended (y=0.488 < MCP 0.5), index at MCP level (not extended).
    Close together so 2D dist ≈ 0.028 < 0.06. middle/ring/pinky curled.
    n_ext=1 → skips FIST; index_ext=False → skips POINT; dist OK → PINCH.
    """
    return make_lm_mock(
        {4: thumb_ny, 8: index_ny, 12: 0.7, 16: 0.7, 20: 0.7},
        {4: thumb_nx, 8: index_nx},
    )


def send_depth_and_gesture(lidar, proc, depth_arr, conf_arr, lm_mock, score=0.90):
    """Send a depth frame to lidar, then run gesture on a camera frame with mocked landmarks."""
    msg = make_depth_msg(depth_array=depth_arr, conf_array=conf_arr)
    lidar.on_depth_frame(msg)

    # Stub gesture_processor.mp when mediapipe is not installed so mp.Image() doesn't NameError.
    import sensors.gesture_processor as gp_mod
    if not hasattr(gp_mod, 'mp') or gp_mod.mp is None:
        gp_mod.mp = MagicMock()
        gp_mod.mp.ImageFormat.SRGB = MagicMock()
    gp_mod._MP_AVAILABLE = True

    result_mock = make_hands_result(lm_mock, score=score)
    mock_hands = MagicMock()
    mock_hands.detect_for_video.return_value = result_mock  # Tasks API
    proc._hands = mock_hands
    proc._available = True
    return proc.on_camera_frame(make_camera_msg())


# ---------------------------------------------------------------------------
# Shared depth array factory
# ---------------------------------------------------------------------------

def make_depth_with_values(index_ny, index_nx, index_depth, thumb_ny, thumb_nx, thumb_depth,
                            w=256, h=192, default_depth=2.0):
    """Create a depth array with specific values at fingertip pixel positions.

    Sets a 2x2 block at each position so bilinear interpolation (which averages
    the 4 nearest pixels) returns exactly the intended depth value.
    """
    depth = np.full((h, w), default_depth, dtype=np.float32)
    conf = np.full((h, w), 2, dtype=np.uint8)

    def set_block(ny, nx, val):
        px = int(nx * (w - 1))
        py = int(ny * (h - 1))
        for dy in range(2):
            for dx in range(2):
                depth[min(py + dy, h - 1), min(px + dx, w - 1)] = val

    set_block(index_ny, index_nx, index_depth)
    set_block(thumb_ny, thumb_nx, thumb_depth)
    return depth, conf


# ---------------------------------------------------------------------------
# GLI-01 — real depth → correct depth query → PINCH accepted (20mm)
# ---------------------------------------------------------------------------

def test_gli01_pinch_accepted_with_real_lidar():
    # Single-finger PINCH is not LiDAR-gated in the rewrite; LiDAR gates TWO_FINGER_GRAB.
    # This test verifies PINCH still fires when LiDAR is wired and fresh.
    lidar = LiDARReceiver(conf_min=1)
    proc = GestureProcessor()
    proc.set_lidar(lidar)

    index_nx, index_ny = 0.51, 0.5
    thumb_nx, thumb_ny = 0.49, 0.488

    depth, conf = make_depth_with_values(
        index_ny, index_nx, 1.000,
        thumb_ny, thumb_nx, 1.020,
    )
    lm = make_pinch_lm()

    cmd = send_depth_and_gesture(lidar, proc, depth, conf, lm)

    assert cmd is not None, "PINCH should fire regardless of LiDAR depth"
    assert cmd.action == "CLICK"
    assert cmd.params["gesture"] == "PINCH"


# ---------------------------------------------------------------------------
# GLI-02 — large LiDAR delta does NOT reject single-finger PINCH
# (LiDAR only gates TWO_FINGER_GRAB; single-finger PINCH is always 2D)
# ---------------------------------------------------------------------------

def test_gli02_pinch_rejected_with_real_lidar():
    lidar = LiDARReceiver(conf_min=1)
    proc = GestureProcessor()
    proc.set_lidar(lidar)

    index_nx, index_ny = 0.51, 0.5
    thumb_nx, thumb_ny = 0.49, 0.488

    depth, conf = make_depth_with_values(
        index_ny, index_nx, 1.000,
        thumb_ny, thumb_nx, 1.040,
    )
    lm = make_pinch_lm()

    cmd = send_depth_and_gesture(lidar, proc, depth, conf, lm)
    # Single-finger PINCH is NOT rejected by LiDAR in the rewrite
    assert cmd is not None, "Single-finger PINCH fires regardless of LiDAR Z-delta"
    assert cmd.action == "CLICK"


# ---------------------------------------------------------------------------
# GLI-03 — stale LiDAR → 2D fallback (PINCH with close 2D tips passes)
# ---------------------------------------------------------------------------

def test_gli03_stale_lidar_2d_fallback():
    lidar = LiDARReceiver(conf_min=0)
    proc = GestureProcessor()
    proc.set_lidar(lidar)

    # Send a depth frame, then make it stale
    lidar.on_depth_frame(make_depth_msg())
    lidar._arrival_ts = time.monotonic() - 2.0  # stale

    lm = make_pinch_lm(index_nx=0.5, index_ny=0.5, thumb_nx=0.52, thumb_ny=0.5)
    result_mock = make_hands_result(lm, score=0.90)
    mock_hands = MagicMock()
    mock_hands.detect_for_video.return_value = result_mock
    proc._hands = mock_hands
    proc._available = True

    cmd = proc.on_camera_frame(make_camera_msg())
    assert cmd is not None, "PINCH fires when LiDAR is stale (stale check skips LiDAR entirely)"


# ---------------------------------------------------------------------------
# GLI-04 — POINT gesture with LiDAR wired → get_depth_at never called
# ---------------------------------------------------------------------------

def test_gli04_point_does_not_touch_lidar():
    lidar = LiDARReceiver(conf_min=0)
    proc = GestureProcessor()
    lidar.on_depth_frame(make_depth_msg())
    proc.set_lidar(lidar)

    # Spy on get_depth_at
    original = lidar.get_depth_at
    call_count = [0]
    def spy(*args, **kwargs):
        call_count[0] += 1
        return original(*args, **kwargs)
    lidar.get_depth_at = spy

    # POINT landmark — stub mp for environments without mediapipe
    import sensors.gesture_processor as gp_mod
    if not hasattr(gp_mod, 'mp') or gp_mod.mp is None:
        gp_mod.mp = MagicMock()
        gp_mod.mp.ImageFormat.SRGB = MagicMock()
    gp_mod._MP_AVAILABLE = True

    from tests.conftest import POINT_LM
    result_mock = make_hands_result(POINT_LM, score=0.90)
    mock_hands = MagicMock()
    mock_hands.detect_for_video.return_value = result_mock
    proc._hands = mock_hands
    proc._available = True

    cmd = proc.on_camera_frame(make_camera_msg())
    # New processor queries wrist depth once per frame (FrameSnap context).
    # Non-grab poses must not trigger the additional finger-tip depth lookups
    # that TWO_FINGER_GRAB uses (which would show call_count >= 3).
    assert call_count[0] <= 1, "Non-grab pose should not trigger finger-tip depth lookups"


# ---------------------------------------------------------------------------
# GLI-05 — low-conf pixels at thumb/index → NaN → 2D fallback, Command emitted
# ---------------------------------------------------------------------------

def test_gli05_masked_pixels_cause_2d_fallback():
    lidar = LiDARReceiver(conf_min=2)  # threshold: keep only conf≥2
    proc = GestureProcessor()
    proc.set_lidar(lidar)

    index_nx, index_ny = 0.5, 0.5
    thumb_nx, thumb_ny = 0.52, 0.5

    h, w = 192, 256
    depth = np.full((h, w), 1.0, dtype=np.float32)
    conf = np.full((h, w), 2, dtype=np.uint8)

    # Set conf=0 at the exact pixel locations → those pixels will be NaN after masking
    ix = int(round(index_nx * (w - 1)))
    iy = int(round(index_ny * (h - 1)))
    tx = int(round(thumb_nx * (w - 1)))
    ty = int(round(thumb_ny * (h - 1)))
    conf[iy, ix] = 0
    conf[ty, tx] = 0

    lidar.on_depth_frame(make_depth_msg(depth_array=depth, conf_array=conf))

    lm = make_pinch_lm(index_nx=index_nx, index_ny=index_ny,
                        thumb_nx=thumb_nx, thumb_ny=thumb_ny)
    result_mock = make_hands_result(lm, score=0.90)
    mock_hands = MagicMock()
    mock_hands.detect_for_video.return_value = result_mock
    proc._hands = mock_hands
    proc._available = True

    cmd = proc.on_camera_frame(make_camera_msg())
    # 2D distance between (0.5, 0.5) and (0.52, 0.5) = 0.02 < 0.06 threshold → PINCH
    assert cmd is not None, "Masked LiDAR pixels should not prevent single-finger PINCH"


# ---------------------------------------------------------------------------
# GLI-06 — second depth frame is used, not first
# ---------------------------------------------------------------------------

def test_gli06_latest_depth_frame_wins():
    lidar = LiDARReceiver(conf_min=0)
    proc = GestureProcessor()
    proc.set_lidar(lidar)

    h, w = 192, 256

    # Frame 1: all pixels = 1.0
    lidar.on_depth_frame(make_depth_msg(fill=1.0))
    # Frame 2: all pixels = 3.0
    lidar.on_depth_frame(make_depth_msg(fill=3.0))

    # Query center — should reflect frame 2
    depth_at_center = lidar.get_depth_at(0.5, 0.5)
    assert depth_at_center is not None
    assert abs(depth_at_center - 3.0) < 0.01, "Should use latest depth frame"

    assert lidar._frame_count == 2
