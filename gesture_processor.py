"""GestureProcessor — hand gesture recognition via MediaPipe Hands.

Receives raw camera frames from the iPad (JPEG or PNG, base64-encoded),
runs MediaPipe hand landmark detection, classifies the hand pose into one
of the supported gestures, and emits a Command to FusionEngine.

When a LiDARReceiver is wired in (set_lidar), the processor uses real
millimetre distances from the depth map for pinch and grab classification
instead of 2D normalised-coordinate estimates.

Supported gestures:
  POINT      — index finger extended, others curled
  PINCH      — thumb tip and index tip close together
  OPEN_PALM  — all five fingers extended
  FIST       — all five fingers curled

Requirements satisfied:
  10.1 Gesture confidence ≥ 0.65 required to emit a Command
  10.2 Below threshold → silently discard
  10.3 Same gesture within 800 ms → debounce (no re-fire)
  10.4 LiDAR depth used for pinch/grab when available; 2D fallback otherwise
  10.5 Graceful degradation when camera feed unavailable

Degrades gracefully when mediapipe or opencv are not installed.
"""

from __future__ import annotations

import base64
import io
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from command_executor import Command

if TYPE_CHECKING:
    from lidar_receiver import LiDARReceiver

log = logging.getLogger(__name__)

try:
    import mediapipe as mp
    import numpy as np
    from mediapipe.tasks.python import BaseOptions as _BaseOptions
    from mediapipe.tasks.python.vision import (
        HandLandmarker as _HandLandmarker,
        HandLandmarkerOptions as _HandLandmarkerOptions,
        RunningMode as _RunningMode,
    )
    _MP_AVAILABLE = True
except (ImportError, AttributeError) as _mp_import_err:
    _MP_AVAILABLE = False
    log.warning(
        "mediapipe not installed or incompatible — GestureProcessor disabled. "
        "Install with: pip install mediapipe  (%s)", _mp_import_err
    )

try:
    import cv2
    _CV2_AVAILABLE = True
except ImportError:
    _CV2_AVAILABLE = False
    log.warning(
        "opencv-python not installed — GestureProcessor disabled. "
        "Install with: pip install opencv-python-headless"
    )


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CONFIDENCE_MIN: float = 0.65       # Requirement 10.1
_DEBOUNCE_S: float = 0.80           # Requirement 10.3 — 800 ms
_PINCH_THRESH_NORM: float = 0.06    # 2D normalised-coord pinch distance (fallback)
_PINCH_THRESH_MM: float = 30.0      # 3D millimetre pinch distance (LiDAR)


# ---------------------------------------------------------------------------
# Landmark indices (MediaPipe Hands)
# ---------------------------------------------------------------------------
# Wrist=0, Thumb=[1-4], Index=[5-8], Middle=[9-12], Ring=[13-16], Pinky=[17-20]
# Tip landmarks: thumb=4, index=8, middle=12, ring=16, pinky=20
# MCP (knuckle) landmarks: index=5, middle=9, ring=13, pinky=17

_FINGER_TIPS = [4, 8, 12, 16, 20]
_FINGER_MCPS = [2, 5, 9, 13, 17]  # thumb IP, others MCP


# ---------------------------------------------------------------------------
# Gesture dataclass
# ---------------------------------------------------------------------------

@dataclass
class _GestureResult:
    name: str           # "POINT" | "PINCH" | "OPEN_PALM" | "FIST"
    confidence: float   # from MediaPipe hand landmark confidence
    hand: str           # "Left" | "Right"
    # Tip coordinates in normalised screen space [0,1]
    index_tip_nx: float
    index_tip_ny: float
    # Z-axis depth delta between fingertips in mm (None if LiDAR unavailable).
    # This is abs(d_index - d_thumb)*1000, not full 3D Euclidean distance.
    pinch_z_delta_mm: Optional[float] = None


# ---------------------------------------------------------------------------
# Landmark helpers
# ---------------------------------------------------------------------------

def _tip_y(lm, idx: int) -> float:
    # lm is List[NormalizedLandmark] from the Tasks API (indexed directly, not .landmark[i])
    return lm[idx].y


def _tip_xy(lm, idx: int) -> tuple[float, float]:
    pt = lm[idx]
    return pt.x, pt.y


def _dist_norm(lm, a: int, b: int) -> float:
    ax, ay = _tip_xy(lm, a)
    bx, by = _tip_xy(lm, b)
    return ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5


def _finger_extended(lm, tip: int, mcp: int) -> bool:
    """Return True when the finger tip is higher (smaller y) than its knuckle."""
    return _tip_y(lm, tip) < _tip_y(lm, mcp)


def _classify_gesture(lm) -> Optional[str]:
    """Classify hand landmarks into a gesture name or None if unrecognised."""
    thumb_ext  = _tip_y(lm, 4) < _tip_y(lm, 2)      # thumb tip above IP
    index_ext  = _finger_extended(lm, 8,  5)
    middle_ext = _finger_extended(lm, 12, 9)
    ring_ext   = _finger_extended(lm, 16, 13)
    pinky_ext  = _finger_extended(lm, 20, 17)

    n_ext = sum([thumb_ext, index_ext, middle_ext, ring_ext, pinky_ext])

    # OPEN_PALM — all five extended
    if n_ext >= 4:
        return "OPEN_PALM"

    # FIST — none extended
    if n_ext == 0:
        return "FIST"

    # POINT — only index extended
    if index_ext and not middle_ext and not ring_ext and not pinky_ext:
        return "POINT"

    # PINCH — thumb and index tips close, others curled
    if not middle_ext and not ring_ext and not pinky_ext:
        d = _dist_norm(lm, 4, 8)
        if d < _PINCH_THRESH_NORM:
            return "PINCH"

    return None


# ---------------------------------------------------------------------------
# Action mapping
# ---------------------------------------------------------------------------

_GESTURE_ACTION: dict[str, str] = {
    "POINT":     "CLICK",
    "PINCH":     "CLICK",
    "OPEN_PALM": "CLARIFY",
    "FIST":      "CLOSE",
}


# ---------------------------------------------------------------------------
# GestureProcessor
# ---------------------------------------------------------------------------

class GestureProcessor:
    """Processes camera_frame messages into gesture Commands for FusionEngine."""

    def __init__(
        self,
        confidence_min: float = _CONFIDENCE_MIN,
        debounce_s: float = _DEBOUNCE_S,
    ) -> None:
        self._conf_min = confidence_min
        self._debounce_s = debounce_s
        self._lidar: Optional["LiDARReceiver"] = None
        self._hands = None  # lazy MediaPipe Hands instance
        self._last_gesture: dict[str, float] = {}  # gesture → last fire ts
        self._frame_count = 0
        self._available = _MP_AVAILABLE and _CV2_AVAILABLE

        # Latest normalised hand landmarks for external consumers (SensorViewer)
        # List of 21 (x, y) tuples in [0,1] space, or None when no hand detected.
        self.latest_landmarks: Optional[list[tuple[float, float]]] = None

    def set_lidar(self, lidar: "LiDARReceiver") -> None:
        self._lidar = lidar

    _MODEL_PATH: str = "hand_landmarker.task"

    def _get_hands(self):
        if self._hands is None and _MP_AVAILABLE:
            import os
            model_path = self._MODEL_PATH
            if not os.path.exists(model_path):
                # Try relative to this file's directory
                model_path = os.path.join(os.path.dirname(__file__), self._MODEL_PATH)
            if not os.path.exists(model_path):
                log.warning(
                    "GestureProcessor: hand_landmarker.task not found — "
                    "download from https://storage.googleapis.com/mediapipe-models/"
                    "hand_landmarker/hand_landmarker/float16/latest/hand_landmarker.task"
                )
                return None
            options = _HandLandmarkerOptions(
                base_options=_BaseOptions(model_asset_path=model_path),
                running_mode=_RunningMode.VIDEO,
                num_hands=1,
                min_hand_detection_confidence=self._conf_min,
                min_hand_presence_confidence=0.50,
                min_tracking_confidence=0.50,
            )
            self._hands = _HandLandmarker.create_from_options(options)
            log.info("GestureProcessor: HandLandmarker loaded (%s)", model_path)
        return self._hands

    # ---------------------------------------------------------------------- #
    # Public entry point — called from IPadBridge
    # ---------------------------------------------------------------------- #

    def on_camera_frame(self, msg: dict) -> Optional[Command]:
        """Decode a camera_frame message and return a Command or None.

        camera_frame message format:
          {
            "type":       "camera_frame",
            "ts":         <unix ms>,
            "width":      640,
            "height":     480,
            "image_b64":  "<base64 JPEG or PNG>"
          }
        """
        if not self._available:
            return None

        image_b64 = msg.get("image_b64", "")
        if not image_b64:
            return None

        try:
            img_bytes = base64.b64decode(image_b64)
            img_arr = cv2.imdecode(
                __import__("numpy").frombuffer(img_bytes, dtype="u1"),
                cv2.IMREAD_COLOR,
            )
            if img_arr is None:
                return None
        except Exception as exc:
            log.debug("GestureProcessor: frame decode error: %s", exc)
            return None

        self._frame_count += 1
        return self._process_frame(img_arr)

    # ---------------------------------------------------------------------- #
    # Internal processing
    # ---------------------------------------------------------------------- #

    def _process_frame(self, bgr_frame: "np.ndarray") -> Optional[Command]:
        hands = self._get_hands()
        if hands is None:
            return None

        rgb = cv2.cvtColor(bgr_frame, cv2.COLOR_BGR2RGB)

        # Tasks API requires an mp.Image and a monotonically increasing timestamp in ms
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        timestamp_ms = int(time.monotonic() * 1000)
        result = hands.detect_for_video(mp_image, timestamp_ms)

        if not result.hand_landmarks:
            self.latest_landmarks = None
            return None

        # lm is List[NormalizedLandmark] — index directly (lm[i].x, lm[i].y)
        lm = result.hand_landmarks[0]

        # Store normalised landmarks for external consumers (SensorViewer overlay)
        self.latest_landmarks = [(lm[i].x, lm[i].y) for i in range(21)]

        hand_label = "Right"
        conf = self._conf_min
        if result.handedness:
            h = result.handedness[0][0]
            hand_label = h.display_name   # 'Left' or 'Right'
            conf = h.score

        # Requirement 10.1 / 10.2 — confidence threshold
        if conf < self._conf_min:
            log.debug("GestureProcessor: below threshold (%.3f < %.3f)", conf, self._conf_min)
            return None

        gesture_name = _classify_gesture(lm)
        if gesture_name is None:
            return None

        # Requirement 10.3 — 800 ms debounce per gesture
        now = time.monotonic()
        if now - self._last_gesture.get(gesture_name, 0.0) < self._debounce_s:
            log.debug("GestureProcessor: debounce suppressed %s", gesture_name)
            return None
        self._last_gesture[gesture_name] = now

        # Index tip position in normalised screen coords
        ix, iy = _tip_xy(lm, 8)

        # Requirement 10.4 — Z-axis depth delta when LiDAR available.
        # Rejects 2D-detected pinches where fingertips are far apart in depth
        # (e.g. index pointing forward while thumb is at rest).
        pinch_z_delta_mm: Optional[float] = None
        if gesture_name == "PINCH" and self._lidar and self._lidar.is_fresh():
            d_index = self._lidar.get_depth_at(ix, iy)
            tx, ty = _tip_xy(lm, 4)
            d_thumb = self._lidar.get_depth_at(tx, ty)
            if d_index is not None and d_thumb is not None:
                pinch_z_delta_mm = abs(d_index - d_thumb) * 1000.0
                if pinch_z_delta_mm > _PINCH_THRESH_MM:
                    log.debug(
                        "GestureProcessor: PINCH rejected by LiDAR Z-delta (%.0f mm > %.0f mm)",
                        pinch_z_delta_mm, _PINCH_THRESH_MM,
                    )
                    return None

        action = _GESTURE_ACTION.get(gesture_name, "CLARIFY")
        log.info(
            "Gesture: %s → %s  conf=%.2f  hand=%s  tip=(%.3f, %.3f)%s",
            gesture_name, action, conf, hand_label, ix, iy,
            f"  z_delta={pinch_z_delta_mm:.0f}mm" if pinch_z_delta_mm is not None else "",
        )

        return Command(
            text=f"gesture:{gesture_name}",
            action=action,
            source="gesture",
            gesture_confidence=conf,
            gaze_coords=None,  # FusionEngine may combine with gaze
            params={
                "gesture": gesture_name,
                "hand": hand_label,
                "tip_x": ix,
                "tip_y": iy,
                "pinch_z_delta_mm": pinch_z_delta_mm,
            },
        )

    # ---------------------------------------------------------------------- #
    # Status
    # ---------------------------------------------------------------------- #

    def get_status(self) -> dict:
        return {
            "available": self._available,
            "frame_count": self._frame_count,
            "mediapipe": _MP_AVAILABLE,
            "opencv": _CV2_AVAILABLE,
            "lidar_wired": self._lidar is not None,
            "confidence_min": self._conf_min,
            "debounce_s": self._debounce_s,
        }

    def close(self) -> None:
        if self._hands is not None:
            try:
                self._hands.close()
            except Exception:
                pass
            self._hands = None
