"""Integration tests: real LiDARReceiver + real GestureProcessor, MediaPipe mocked (6 tests).

Tests GLI-01 through GLI-06 as specified in the test plan.
"""

from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time
from unittest.mock import MagicMock, patch

import numpy as np

from lidar_receiver import LiDARReceiver
from gesture_processor import GestureProcessor
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

    result_mock = make_hands_result(lm_mock, score=score)
    mock_hands = MagicMock()
    mock_hands.process.return_value = result_mock
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
    lidar = LiDARReceiver(conf_min=1)
    proc = GestureProcessor()
    proc.set_lidar(lidar)

    # Use default coords from make_pinch_lm: index=(0.51, 0.5), thumb=(0.49, 0.488)
    index_nx, index_ny = 0.51, 0.5
    thumb_nx, thumb_ny = 0.49, 0.488

    depth, conf = make_depth_with_values(
        index_ny, index_nx, 1.000,
        thumb_ny, thumb_nx, 1.020,  # 20mm diff → accept
    )
    lm = make_pinch_lm()  # defaults match the coords above

    cmd = send_depth_and_gesture(lidar, proc, depth, conf, lm)

    assert cmd is not None, "PINCH with 20mm diff should be accepted"
    assert cmd.action == "CLICK"
    assert cmd.params["pinch_mm"] is not None
    assert cmd.params["pinch_mm"] < 30.0


# ---------------------------------------------------------------------------
# GLI-02 — same setup but 40mm → PINCH rejected
# ---------------------------------------------------------------------------

def test_gli02_pinch_rejected_with_real_lidar():
    lidar = LiDARReceiver(conf_min=1)
    proc = GestureProcessor()
    proc.set_lidar(lidar)

    index_nx, index_ny = 0.51, 0.5
    thumb_nx, thumb_ny = 0.49, 0.488

    depth, conf = make_depth_with_values(
        index_ny, index_nx, 1.000,
        thumb_ny, thumb_nx, 1.040,  # 40mm diff → reject
    )
    lm = make_pinch_lm()  # defaults match the coords above

    cmd = send_depth_and_gesture(lidar, proc, depth, conf, lm)
    assert cmd is None, "PINCH with 40mm diff should be rejected"


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
    mock_hands.process.return_value = result_mock
    proc._hands = mock_hands
    proc._available = True

    cmd = proc.on_camera_frame(make_camera_msg())
    assert cmd is not None, "Stale LiDAR should fall back to 2D and accept close PINCH"
    assert cmd.params["pinch_mm"] is None


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

    # POINT landmark
    from tests.conftest import POINT_LM
    result_mock = make_hands_result(POINT_LM, score=0.90)
    mock_hands = MagicMock()
    mock_hands.process.return_value = result_mock
    proc._hands = mock_hands
    proc._available = True

    cmd = proc.on_camera_frame(make_camera_msg())
    assert cmd is not None
    assert cmd.params["gesture"] == "POINT"
    assert call_count[0] == 0, "get_depth_at should not be called for POINT gesture"


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
    mock_hands.process.return_value = result_mock
    proc._hands = mock_hands
    proc._available = True

    cmd = proc.on_camera_frame(make_camera_msg())
    # 2D distance between (0.5, 0.5) and (0.52, 0.5) = 0.02 < 0.06 threshold → pass
    assert cmd is not None, "Masked LiDAR pixels should fall back to 2D and accept"
    assert cmd.params["pinch_mm"] is None


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
