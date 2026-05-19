"""Shared test fixtures and synthetic data helpers for LiDAR gesture tests."""

from __future__ import annotations

import base64
import time
from typing import Optional
from unittest.mock import MagicMock

import numpy as np
import cv2


# ---------------------------------------------------------------------------
# Depth-frame helpers
# ---------------------------------------------------------------------------

def make_depth_msg(
    fill: float = 1.5,
    conf_val: int = 2,
    w: int = 256,
    h: int = 192,
    ts: Optional[float] = None,
    depth_array: Optional[np.ndarray] = None,
    conf_array: Optional[np.ndarray] = None,
) -> dict:
    """Return a depth_frame message dict with synthetic or provided arrays."""
    depth = depth_array if depth_array is not None else np.full((h, w), fill, dtype="<f4")
    conf  = conf_array  if conf_array  is not None else np.full((h, w), conf_val, dtype="u1")
    return {
        "type": "depth_frame",
        "ts": ts if ts is not None else time.time() * 1000,
        "width": w,
        "height": h,
        "depth_b64": base64.b64encode(depth.astype("<f4").tobytes()).decode(),
        "conf_b64":  base64.b64encode(conf.astype("u1").tobytes()).decode(),
    }


# ---------------------------------------------------------------------------
# Camera-frame helpers
# ---------------------------------------------------------------------------

def make_camera_msg(w: int = 64, h: int = 64, color=(80, 150, 220)) -> dict:
    """Return a camera_frame message with a solid-colour synthetic JPEG."""
    img = np.full((h, w, 3), color, dtype=np.uint8)
    _, buf = cv2.imencode(".jpg", img)
    return {
        "type": "camera_frame",
        "ts": 0,
        "width": w,
        "height": h,
        "image_b64": base64.b64encode(buf.tobytes()).decode(),
    }


# ---------------------------------------------------------------------------
# MediaPipe landmark mocks
# ---------------------------------------------------------------------------

def make_lm_mock(
    tip_y_map: dict,
    tip_x_map: Optional[dict] = None,
    default_y: float = 0.5,
    default_x: float = 0.5,
) -> MagicMock:
    """Build a MediaPipe-like landmark mock for 21 hand points.

    tip_y_map: {landmark_index: y_value}  — overrides for specific landmarks
    tip_x_map: {landmark_index: x_value}  — overrides for specific landmarks
    """
    pts = []
    for i in range(21):
        pt = MagicMock()
        pt.y = tip_y_map.get(i, default_y)
        pt.x = (tip_x_map or {}).get(i, default_x)
        pt.z = 0.0
        pts.append(pt)
    lm = MagicMock()
    lm.landmark = pts
    return lm


def make_hands_result(lm: MagicMock, score: float = 0.90, label: str = "Right") -> MagicMock:
    """Wrap a landmark mock in a MediaPipe process() result mock."""
    result = MagicMock()
    result.multi_hand_landmarks = [lm]
    cls = MagicMock()
    cls.score = score
    cls.label = label
    handed = MagicMock()
    handed.classification = [cls]
    result.multi_handedness = [handed]
    return result


# ---------------------------------------------------------------------------
# Preset gesture landmark configurations
# All use default MCP y=0.5; tips above (y<0.5) = extended, below = curled.
# ---------------------------------------------------------------------------

# index (8) up, all others curled
POINT_LM = make_lm_mock({8: 0.2})

# all curled
FIST_LM = make_lm_mock({4: 0.8, 8: 0.8, 12: 0.8, 16: 0.8, 20: 0.8})

# all extended
PALM_LM = make_lm_mock({4: 0.2, 8: 0.2, 12: 0.2, 16: 0.2, 20: 0.2})

# PINCH: thumb extended (y=0.48 < MCP y=0.5), index NOT extended, both x-close.
# n_ext=1 → skips FIST; index_ext=False → skips POINT; dist≈0.028 < 0.06 → PINCH.
PINCH_LM = make_lm_mock(
    {4: 0.48, 8: 0.5, 12: 0.7, 16: 0.7, 20: 0.7},
    {4: 0.49, 8: 0.51},
)

# two fingers extended — unrecognised pose
TWO_FINGER_LM = make_lm_mock({8: 0.2, 12: 0.2})

# four fingers (no thumb) — should still trigger OPEN_PALM (n_ext >= 4)
FOUR_FINGER_LM = make_lm_mock({8: 0.2, 12: 0.2, 16: 0.2, 20: 0.2})
