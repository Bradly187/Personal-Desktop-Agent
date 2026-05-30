"""Unit tests for LiDARReceiver (16 tests).

Tests LR-01 through LR-16 as specified in the test plan.
"""

from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import base64
import time
from unittest.mock import patch

import numpy as np
import pytest

from sensors.lidar_receiver import LiDARReceiver
from tests.conftest import make_depth_msg


# ---------------------------------------------------------------------------
# LR-01 — decode stores float32 (H, W) array
# ---------------------------------------------------------------------------

def test_lr01_decode_stores_array():
    lidar = LiDARReceiver(conf_min=0)
    h, w = 192, 256
    depth_arr = np.fromfunction(lambda r, c: r * 0.01 + c * 0.001, (h, w), dtype=np.float32)
    conf_arr = np.full((h, w), 2, dtype=np.uint8)
    msg = make_depth_msg(depth_array=depth_arr, conf_array=conf_arr, w=w, h=h)
    lidar.on_depth_frame(msg)

    assert lidar.latest_depth is not None
    assert lidar.latest_depth.shape == (192, 256)
    assert lidar.latest_depth.dtype == np.float32
    expected = 10 * 0.01 + 20 * 0.001  # row=10, col=20
    assert abs(lidar.latest_depth[10, 20] - expected) < 1e-4
    assert lidar._frame_count == 1


# ---------------------------------------------------------------------------
# LR-02 — low-confidence pixels become NaN
# ---------------------------------------------------------------------------

def test_lr02_confidence_masking_nan():
    lidar = LiDARReceiver(conf_min=2)
    h, w = 4, 4
    depth_arr = np.full((h, w), 1.5, dtype=np.float32)
    # checkerboard: even indices conf=1 (below min), odd=2 (at/above min)
    conf_arr = np.zeros((h, w), dtype=np.uint8)
    conf_arr[0::2, 0::2] = 1  # even rows even cols → conf=1
    conf_arr[1::2, 1::2] = 1
    conf_arr[0::2, 1::2] = 2  # even rows odd cols → conf=2
    conf_arr[1::2, 0::2] = 2
    msg = make_depth_msg(depth_array=depth_arr, conf_array=conf_arr, w=w, h=h)
    lidar.on_depth_frame(msg)

    assert np.isnan(lidar.latest_depth[0, 0])   # conf=1 → masked
    assert abs(lidar.latest_depth[0, 1] - 1.5) < 1e-5   # conf=2 → kept


# ---------------------------------------------------------------------------
# LR-03 — conf_min=0 keeps all pixels
# ---------------------------------------------------------------------------

def test_lr03_conf_min_zero_keeps_all():
    lidar = LiDARReceiver(conf_min=0)
    msg = make_depth_msg(fill=2.0, conf_val=0)
    lidar.on_depth_frame(msg)
    assert not np.any(np.isnan(lidar.latest_depth))
    result = lidar.get_depth_at(0.5, 0.5)
    assert result is not None
    assert abs(result - 2.0) < 0.01


# ---------------------------------------------------------------------------
# LR-04 — bilinear interpolation on linear gradient
# ---------------------------------------------------------------------------

def test_lr04_bilinear_linear_gradient():
    lidar = LiDARReceiver(conf_min=0)
    h, w = 192, 256
    # row-gradient: 0.0 at top, 1.0 at bottom
    depth_arr = np.tile(np.linspace(0.0, 1.0, h, dtype=np.float32).reshape(h, 1), (1, w))
    conf_arr = np.full((h, w), 2, dtype=np.uint8)
    msg = make_depth_msg(depth_array=depth_arr, conf_array=conf_arr, w=w, h=h)
    lidar.on_depth_frame(msg)

    result = lidar.get_depth_at(0.0, 0.5)  # x=left edge, y=vertical center
    assert result is not None
    assert abs(result - 0.5) < 0.01


# ---------------------------------------------------------------------------
# LR-05 — all NaN neighbors → returns None
# ---------------------------------------------------------------------------

def test_lr05_all_nan_returns_none():
    lidar = LiDARReceiver(conf_min=2)
    h, w = 4, 4
    depth_arr = np.full((h, w), 1.0, dtype=np.float32)
    conf_arr = np.zeros((h, w), dtype=np.uint8)  # all conf=0 → all NaN
    msg = make_depth_msg(depth_array=depth_arr, conf_array=conf_arr, w=w, h=h)
    lidar.on_depth_frame(msg)

    result = lidar.get_depth_at(0.5, 0.5)
    assert result is None


# ---------------------------------------------------------------------------
# LR-06 — partial NaN: averages only valid samples
# ---------------------------------------------------------------------------

def test_lr06_partial_nan_averages_valid():
    lidar = LiDARReceiver(conf_min=0)
    h, w = 4, 4
    depth_arr = np.full((h, w), 99.0, dtype=np.float32)
    # Manually inject NaN into latest_depth to simulate masked pixels
    # Use conf_min=0 to get through decode, then patch directly
    msg = make_depth_msg(fill=1.0, conf_val=0, w=w, h=h)
    lidar.on_depth_frame(msg)

    # Patch: set all pixels NaN except top-left = 2.0
    lidar.latest_depth[:] = np.nan
    lidar.latest_depth[0, 0] = 2.0

    # Query top-left corner — only one valid sample
    result = lidar.get_depth_at(0.0, 0.0)
    assert result is not None
    assert abs(result - 2.0) < 1e-4


# ---------------------------------------------------------------------------
# LR-07 — corner coords (1.0, 1.0) clamp correctly, no IndexError
# ---------------------------------------------------------------------------

def test_lr07_corner_clamps_no_index_error():
    lidar = LiDARReceiver(conf_min=0)
    h, w = 192, 256
    depth_arr = np.full((h, w), 1.0, dtype=np.float32)
    depth_arr[h - 1, w - 1] = 9.9
    conf_arr = np.full((h, w), 2, dtype=np.uint8)
    msg = make_depth_msg(depth_array=depth_arr, conf_array=conf_arr, w=w, h=h)
    lidar.on_depth_frame(msg)

    result = lidar.get_depth_at(1.0, 1.0)
    assert result is not None
    # The bottom-right pixel dominates at (1.0, 1.0)
    assert result > 1.0


# ---------------------------------------------------------------------------
# LR-08 — get_depth_at before any frame → None
# ---------------------------------------------------------------------------

def test_lr08_no_frame_returns_none():
    lidar = LiDARReceiver()
    assert lidar.get_depth_at(0.5, 0.5) is None


# ---------------------------------------------------------------------------
# LR-09 — is_fresh True immediately after frame (Bug 1 regression test)
# ---------------------------------------------------------------------------

def test_lr09_is_fresh_true_after_frame():
    lidar = LiDARReceiver(conf_min=0)
    msg = make_depth_msg()
    lidar.on_depth_frame(msg)
    # Must be True immediately — Bug 1 caused this to always return False
    assert lidar.is_fresh(0.5) is True


# ---------------------------------------------------------------------------
# LR-10 — is_fresh False after timeout
# ---------------------------------------------------------------------------

def test_lr10_is_fresh_false_after_timeout():
    lidar = LiDARReceiver(conf_min=0)
    msg = make_depth_msg()
    lidar.on_depth_frame(msg)
    # Simulate old arrival
    lidar._recv_mono = time.monotonic() - 1.0
    assert lidar.is_fresh(0.5) is False


# ---------------------------------------------------------------------------
# LR-11 — is_fresh False on fresh instance (no frame received)
# ---------------------------------------------------------------------------

def test_lr11_is_fresh_false_before_frame():
    lidar = LiDARReceiver()
    assert lidar.is_fresh(0.5) is False


# ---------------------------------------------------------------------------
# LR-12 — missing depth_b64 key → no crash, latest_depth stays None
# ---------------------------------------------------------------------------

def test_lr12_missing_depth_key_no_crash():
    lidar = LiDARReceiver()
    bad_msg = {"type": "depth_frame", "ts": 0, "width": 256, "height": 192}
    lidar.on_depth_frame(bad_msg)
    assert lidar.latest_depth is None
    assert lidar._frame_count == 0


# ---------------------------------------------------------------------------
# LR-13 — wrong buffer size → reshape ValueError caught
# ---------------------------------------------------------------------------

def test_lr13_wrong_buffer_size_no_crash():
    lidar = LiDARReceiver()
    short_bytes = b"\x00" * 100  # way too short for 256×192×4
    conf_bytes = b"\x02" * (256 * 192)
    msg = {
        "type": "depth_frame",
        "ts": 0,
        "width": 256,
        "height": 192,
        "depth_b64": base64.b64encode(short_bytes).decode(),
        "conf_b64": base64.b64encode(conf_bytes).decode(),
    }
    lidar.on_depth_frame(msg)
    assert lidar.latest_depth is None


# ---------------------------------------------------------------------------
# LR-14 — non-numeric width → caught, no crash
# ---------------------------------------------------------------------------

def test_lr14_non_numeric_width_no_crash():
    lidar = LiDARReceiver()
    depth_bytes = b"\x00" * (256 * 192 * 4)
    conf_bytes = b"\x02" * (256 * 192)
    msg = {
        "type": "depth_frame",
        "ts": 0,
        "width": "bad",
        "height": 192,
        "depth_b64": base64.b64encode(depth_bytes).decode(),
        "conf_b64": base64.b64encode(conf_bytes).decode(),
    }
    lidar.on_depth_frame(msg)
    assert lidar.latest_depth is None


# ---------------------------------------------------------------------------
# LR-15 — get_status reports correct shape after frame
# ---------------------------------------------------------------------------

def test_lr15_get_status_shape():
    lidar = LiDARReceiver(conf_min=0)
    lidar.on_depth_frame(make_depth_msg())
    status = lidar.get_status()
    assert status["shape"] == [192, 256]
    assert status["frame_count"] == 1
    assert status["available"] is True


# ---------------------------------------------------------------------------
# LR-16 — numpy unavailable → graceful degradation
# ---------------------------------------------------------------------------

def test_lr16_numpy_unavailable_graceful():
    import sensors.lidar_receiver as lr_mod
    original = lr_mod._NP_AVAILABLE
    try:
        lr_mod._NP_AVAILABLE = False
        lidar = LiDARReceiver()
        lidar._available = False
        lidar.latest_depth = None
        # All public methods must not raise
        lidar.on_depth_frame(make_depth_msg())
        assert lidar.get_depth_at(0.5, 0.5) is None
        assert lidar.is_fresh(0.5) is False
    finally:
        lr_mod._NP_AVAILABLE = original
