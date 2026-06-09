"""Unit tests for the RealSense L515 sidecar encode/convert helpers.

These run under the main app's Python 3.14 env — they import only the pure
helpers (numpy + cv2), never pyrealsense2 or the camera. The key test is the
round-trip: a depth frame encoded by the sidecar must decode correctly through
the real LiDARReceiver.on_depth_frame() and yield the right metres from
get_depth_at(). That proves the sidecar<->consumer wire contract.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import base64

import numpy as np
import pytest

from sensors.lidar_receiver import LiDARReceiver
from sensors.realsense_publisher import (
    depth_units_to_metres,
    synth_confidence,
    encode_depth_frame,
    encode_camera_frame,
    downscale_depth,
    rotate_frame,
)


# --------------------------------------------------------------------------- #
# depth_units_to_metres
# --------------------------------------------------------------------------- #

def test_depth_units_to_metres_scaling():
    z16 = np.array([[0, 1000], [4000, 8000]], dtype=np.uint16)
    out = depth_units_to_metres(z16, 0.00025)  # L515-ish scale
    assert out.dtype == np.float32
    np.testing.assert_allclose(out, [[0.0, 0.25], [1.0, 2.0]], rtol=1e-6)


def test_depth_units_to_metres_does_not_overflow_uint16():
    # Naive uint16 * float would be fine, but ensure we cast before scaling.
    z16 = np.full((2, 2), 65535, dtype=np.uint16)
    out = depth_units_to_metres(z16, 0.001)
    np.testing.assert_allclose(out, 65.535, rtol=1e-5)


# --------------------------------------------------------------------------- #
# synth_confidence
# --------------------------------------------------------------------------- #

def test_synth_confidence_masks_invalid_zero():
    depth = np.array([[0.0, 1.2], [0.0, 3.4]], dtype=np.float32)
    conf = synth_confidence(depth)
    assert conf.dtype == np.uint8
    np.testing.assert_array_equal(conf, [[0, 2], [0, 2]])


def test_synth_confidence_nan_is_invalid():
    depth = np.array([[np.nan, 0.5]], dtype=np.float32)
    conf = synth_confidence(depth)
    np.testing.assert_array_equal(conf, [[0, 2]])


# --------------------------------------------------------------------------- #
# encode_depth_frame schema
# --------------------------------------------------------------------------- #

def test_encode_depth_frame_schema():
    depth = np.full((4, 6), 1.5, dtype=np.float32)
    conf = synth_confidence(depth)
    msg = encode_depth_frame(depth, conf, ts_ms=123456.0)
    assert msg["type"] == "depth_frame"
    assert msg["width"] == 6 and msg["height"] == 4
    assert msg["ts"] == 123456.0
    # base64 fields decode to the expected byte lengths (float32 vs uint8).
    assert len(base64.b64decode(msg["depth_b64"])) == 4 * 6 * 4
    assert len(base64.b64decode(msg["conf_b64"])) == 4 * 6 * 1


# --------------------------------------------------------------------------- #
# encode_camera_frame
# --------------------------------------------------------------------------- #

def test_encode_camera_frame_decodes_back():
    import cv2

    bgr = np.zeros((48, 64, 3), dtype=np.uint8)
    bgr[:, :, 1] = 200  # green-ish
    msg = encode_camera_frame(bgr, jpeg_quality=90)
    assert msg["type"] == "camera_frame"
    raw = base64.b64decode(msg["image_b64"])
    decoded = cv2.imdecode(np.frombuffer(raw, dtype="u1"), cv2.IMREAD_COLOR)
    assert decoded is not None
    assert decoded.shape == (48, 64, 3)


# --------------------------------------------------------------------------- #
# downscale_depth
# --------------------------------------------------------------------------- #

def test_downscale_depth_changes_shape():
    depth = np.full((480, 640), 2.0, dtype=np.float32)
    out = downscale_depth(depth, 320, 240)
    assert out.shape == (240, 320)


def test_downscale_depth_noop_when_same_size():
    depth = np.full((240, 320), 2.0, dtype=np.float32)
    out = downscale_depth(depth, 320, 240)
    assert out.shape == (240, 320)


def test_downscale_depth_nearest_preserves_invalid_zeros():
    # A column of invalid (0) pixels must stay exactly 0 after nearest-neighbour
    # downscale — no bleed from bilinear interpolation.
    depth = np.full((4, 4), 3.0, dtype=np.float32)
    depth[:, 0] = 0.0
    out = downscale_depth(depth, 2, 2)
    assert (out == 0.0).any()
    assert not np.isnan(out).any()


# --------------------------------------------------------------------------- #
# rotate_frame
# --------------------------------------------------------------------------- #

def test_rotate_frame_zero_is_noop():
    arr = np.arange(12, dtype=np.float32).reshape(3, 4)
    out = rotate_frame(arr, 0)
    np.testing.assert_array_equal(out, arr)


def test_rotate_frame_180_is_double_flip():
    arr = np.arange(12, dtype=np.float32).reshape(3, 4)
    out = rotate_frame(arr, 180)
    np.testing.assert_array_equal(out, arr[::-1, ::-1])


def test_rotate_frame_180_twice_is_identity():
    arr = np.random.default_rng(0).random((4, 6)).astype(np.float32)
    np.testing.assert_array_equal(rotate_frame(rotate_frame(arr, 180), 180), arr)


# --------------------------------------------------------------------------- #
# End-to-end round-trip: sidecar encode -> real LiDARReceiver -> get_depth_at
# --------------------------------------------------------------------------- #

def test_roundtrip_through_lidar_receiver():
    # Build a depth frame with a known value, encode it as the sidecar would,
    # feed it into the real consumer, and read it back at a normalized coord.
    depth = np.full((192, 256), 1.75, dtype=np.float32)
    conf = synth_confidence(depth)
    msg = encode_depth_frame(depth, conf, ts_ms=1000.0)

    lidar = LiDARReceiver()
    lidar.on_depth_frame(msg)

    assert lidar.latest_depth is not None
    assert lidar.latest_depth.shape == (192, 256)
    # Centre of frame should read back the encoded metres.
    d = lidar.get_depth_at(0.5, 0.5)
    assert d == pytest.approx(1.75, abs=1e-4)


def test_roundtrip_invalid_pixels_become_nan():
    # Invalid (zero) depth -> conf 0 -> LiDARReceiver masks to NaN -> get_depth_at None.
    depth = np.zeros((192, 256), dtype=np.float32)  # all invalid
    conf = synth_confidence(depth)
    msg = encode_depth_frame(depth, conf, ts_ms=1000.0)

    lidar = LiDARReceiver()
    lidar.on_depth_frame(msg)

    assert lidar.get_depth_at(0.5, 0.5) is None
    # The whole frame should be NaN (all masked).
    assert np.all(np.isnan(lidar.latest_depth))
