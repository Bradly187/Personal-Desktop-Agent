"""GestureProcessor — spatial motion-based gesture recognition (Minority Report style).

Receives camera frames from the iPad, runs MediaPipe HandLandmarker to get 21
3D-ish landmarks per hand, then analyses TRAJECTORIES across a rolling frame buffer
to detect directional gestures in space.

Gesture vocabulary (motion-based, not pose-based):
  SWIPE_LEFT   — open hand moves left at speed  → SCROLL left / browser back
  SWIPE_RIGHT  — open hand moves right at speed → SCROLL right / browser forward
  SWIPE_UP     — open hand moves up at speed    → SCROLL up
  SWIPE_DOWN   — open hand moves down at speed  → SCROLL down
  PUSH         — open palm moves toward camera  → CLICK (at gaze/cursor position)
  PULL         — open palm moves away from cam  → HOTKEY (right-click / context menu)
  PINCH        — thumb-index close in 3D space  → CLICK (LiDAR depth validated)
  PINCH_DRAG   — pinch held while translating   → MOUSEDOWN → move → MOUSEUP

Design principles:
  - All gestures derived from MOTION (velocity / displacement), not static poses.
    A hand passing through a pose shape is not a gesture; intentional movement is.
  - The wrist landmark (0) drives swipe/push/pull detection — robust to finger pose.
  - Frame buffer holds the last BUFFER_WINDOW_MS of frames; velocity is estimated
    from the buffer rather than frame-to-frame deltas to smooth sensor noise.
  - LiDAR depth gives the Z-axis for push/pull and 3D pinch validation.
  - Graceful degradation at every level: no mediapipe → no gestures; no LiDAR →
    2D-only swipes still work; no camera → no gestures.

Confidence and debounce:
  - Swipe fires when velocity > SWIPE_VELOCITY_THRESHOLD over ≥ 3 frames.
  - Push/pull fires when Z-velocity > PUSH_VELOCITY_THRESHOLD over ≥ 3 frames.
  - Each gesture type has its own last-fired timestamp (800ms default debounce).
  - Pinch still uses pose-based detection (thumb-index proximity) for reliability.
"""

from __future__ import annotations

import base64
import collections
import logging
import time
from dataclasses import dataclass, field
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
    log.warning("opencv-python not installed — GestureProcessor disabled.")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CONFIDENCE_MIN: float = 0.65           # minimum HandLandmarker confidence
_DEBOUNCE_S: float = 0.80               # 800ms per gesture type
_BUFFER_WINDOW_MS: float = 500.0        # rolling frame buffer duration (ms)

# Swipe: minimum normalised-coord velocity (coords/second) to register
_SWIPE_VELOCITY_THRESHOLD: float = 1.2  # ≈ full-screen in ~0.8s
_SWIPE_MIN_FRAMES: int = 3              # need at least this many frames in buffer

# Push/pull: minimum Z-velocity (metres/second via LiDAR) to register
_PUSH_VELOCITY_THRESHOLD: float = 0.3   # 30cm/s toward or away from camera

# Pinch (static): thumb-index 2D proximity threshold
_PINCH_THRESH_NORM: float = 0.06
_PINCH_THRESH_MM: float = 30.0          # LiDAR depth delta threshold

# Minimum dominant-axis ratio to distinguish directional swipes from diagonal noise
_SWIPE_AXIS_RATIO: float = 1.8          # dominant axis must be 1.8× the other


# ---------------------------------------------------------------------------
# Landmark indices
# ---------------------------------------------------------------------------

_WRIST = 0
_THUMB_TIP = 4
_INDEX_TIP = 8
_MIDDLE_TIP = 12
_RING_TIP = 16
_PINKY_TIP = 20
_INDEX_MCP = 5
_MIDDLE_MCP = 9
_RING_MCP = 13
_PINKY_MCP = 17
_PALM_CENTRE = 9          # middle MCP approximates palm centre

_FINGER_TIPS = [4, 8, 12, 16, 20]
_FINGER_MCPS = [2, 5, 9, 13, 17]


# ---------------------------------------------------------------------------
# Frame snapshot
# ---------------------------------------------------------------------------

@dataclass
class _FrameSnap:
    """Landmark positions for a single frame in the buffer."""
    ts_mono: float                   # time.monotonic() at capture
    wrist_x: float                   # normalised [0,1]
    wrist_y: float
    palm_x: float                    # palm centre (middle MCP)
    palm_y: float
    thumb_x: float
    thumb_y: float
    index_x: float
    index_y: float
    wrist_z_m: Optional[float]       # LiDAR depth at wrist, metres
    confidence: float


# ---------------------------------------------------------------------------
# Gesture result
# ---------------------------------------------------------------------------

@dataclass
class _GestureResult:
    name: str           # gesture type string
    confidence: float
    hand: str           # "Left" | "Right"
    index_tip_nx: float
    index_tip_ny: float
    pinch_z_delta_mm: Optional[float] = None


# ---------------------------------------------------------------------------
# Per-frame landmark helpers (Tasks API: lm is List[NormalizedLandmark])
# ---------------------------------------------------------------------------

def _tip_y(lm, idx: int) -> float:
    return lm[idx].y

def _tip_xy(lm, idx: int) -> tuple[float, float]:
    pt = lm[idx]
    return pt.x, pt.y

def _dist_norm(lm, a: int, b: int) -> float:
    ax, ay = _tip_xy(lm, a)
    bx, by = _tip_xy(lm, b)
    return ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5

def _finger_extended(lm, tip: int, mcp: int) -> bool:
    return _tip_y(lm, tip) < _tip_y(lm, mcp)

def _open_palm(lm) -> bool:
    """True when 4+ fingers are extended (open palm or close to it)."""
    n_ext = sum(
        1 for tip, mcp in zip(_FINGER_TIPS[1:], _FINGER_MCPS[1:])
        if _finger_extended(lm, tip, mcp)
    )
    return n_ext >= 3

def _is_pinch(lm) -> bool:
    """True when thumb and index are close in 2D (pose-based, not motion)."""
    return _dist_norm(lm, _THUMB_TIP, _INDEX_TIP) < _PINCH_THRESH_NORM


# ---------------------------------------------------------------------------
# GestureProcessor
# ---------------------------------------------------------------------------

class GestureProcessor:
    """Converts camera frames into spatial motion gesture Commands."""

    _MODEL_PATH: str = "hand_landmarker.task"

    def __init__(
        self,
        confidence_min: float = _CONFIDENCE_MIN,
        debounce_s: float = _DEBOUNCE_S,
    ) -> None:
        self._conf_min = confidence_min
        self._debounce_s = debounce_s
        self._lidar: Optional["LiDARReceiver"] = None
        self._hands = None                       # lazy HandLandmarker instance
        self._available = _MP_AVAILABLE and _CV2_AVAILABLE

        # Rolling frame buffer for trajectory analysis
        self._buffer: collections.deque[_FrameSnap] = collections.deque()

        # Per-gesture-type debounce: gesture_name → last_fired monotonic
        self._last_fired: dict[str, float] = {}

        # Pinch-drag state
        self._pinch_active: bool = False
        self._pinch_drag_start: Optional[tuple[float, float]] = None

        self._frame_count = 0

        # Latest landmarks for SensorViewer overlay
        self.latest_landmarks: Optional[list[tuple[float, float]]] = None

    def set_lidar(self, lidar: "LiDARReceiver") -> None:
        self._lidar = lidar

    # ---------------------------------------------------------------------- #
    # Public entry point
    # ---------------------------------------------------------------------- #

    def on_camera_frame(self, msg: dict) -> Optional[Command]:
        """Decode a camera_frame message and return a Command or None."""
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
    # HandLandmarker (lazy init)
    # ---------------------------------------------------------------------- #

    def _get_hands(self):
        if self._hands is None and _MP_AVAILABLE:
            import os
            model_path = self._MODEL_PATH
            if not os.path.exists(model_path):
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
    # Per-frame processing
    # ---------------------------------------------------------------------- #

    def _process_frame(self, bgr_frame: "np.ndarray") -> Optional[Command]:
        hands = self._get_hands()
        if hands is None:
            return None

        rgb = cv2.cvtColor(bgr_frame, cv2.COLOR_BGR2RGB)
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        timestamp_ms = int(time.monotonic() * 1000)
        result = hands.detect_for_video(mp_image, timestamp_ms)

        if not result.hand_landmarks:
            self.latest_landmarks = None
            self._prune_buffer()
            # If pinch was active and hand disappeared, release the drag
            if self._pinch_active:
                return self._end_pinch_drag()
            return None

        lm = result.hand_landmarks[0]
        self.latest_landmarks = [(lm[i].x, lm[i].y) for i in range(21)]

        hand_label = "Right"
        conf = self._conf_min
        if result.handedness:
            h = result.handedness[0][0]
            hand_label = h.display_name
            conf = h.score

        if conf < self._conf_min:
            return None

        # LiDAR depth at wrist position
        wx, wy = _tip_xy(lm, _WRIST)
        wrist_z = None
        if self._lidar and self._lidar.is_fresh():
            wrist_z = self._lidar.get_depth_at(wx, wy)

        # Add to frame buffer
        px, py = _tip_xy(lm, _PALM_CENTRE)
        tx, ty = _tip_xy(lm, _THUMB_TIP)
        ix, iy = _tip_xy(lm, _INDEX_TIP)
        snap = _FrameSnap(
            ts_mono=time.monotonic(),
            wrist_x=wx, wrist_y=wy,
            palm_x=px, palm_y=py,
            thumb_x=tx, thumb_y=ty,
            index_x=ix, index_y=iy,
            wrist_z_m=wrist_z,
            confidence=conf,
        )
        self._buffer.append(snap)
        self._prune_buffer()

        # ── Gesture detection pipeline ─────────────────────────────────────
        # Order matters: pinch-drag must check before swipe (a dragging hand
        # can have lateral velocity), and push/pull before swipe (different axis).

        # 1. Pinch (static pose — most reliable for small precise interactions)
        if _is_pinch(lm):
            return self._handle_pinch(lm, conf, hand_label, ix, iy)

        # Pinch released mid-drag
        if self._pinch_active:
            return self._end_pinch_drag()

        # 2. Motion gestures (require open palm)
        if not _open_palm(lm):
            return None

        # Push / Pull (Z-axis motion via LiDAR)
        push_pull = self._detect_push_pull()
        if push_pull:
            return self._make_command(push_pull, conf, hand_label, ix, iy)

        # Swipe (XY motion)
        swipe = self._detect_swipe()
        if swipe:
            return self._make_command(swipe, conf, hand_label, ix, iy)

        return None

    # ---------------------------------------------------------------------- #
    # Trajectory analysis
    # ---------------------------------------------------------------------- #

    def _prune_buffer(self) -> None:
        """Remove frames older than BUFFER_WINDOW_MS from the left of the buffer."""
        cutoff = time.monotonic() - _BUFFER_WINDOW_MS / 1000.0
        while self._buffer and self._buffer[0].ts_mono < cutoff:
            self._buffer.popleft()

    def _detect_swipe(self) -> Optional[str]:
        """Detect a directional swipe from wrist XY velocity over the buffer."""
        if len(self._buffer) < _SWIPE_MIN_FRAMES:
            return None

        oldest = self._buffer[0]
        newest = self._buffer[-1]
        dt = newest.ts_mono - oldest.ts_mono
        if dt < 0.05:   # need at least 50ms of data
            return None

        dx = newest.wrist_x - oldest.wrist_x
        dy = newest.wrist_y - oldest.wrist_y
        vx = dx / dt       # normalised coords/second
        vy = dy / dt

        speed = (vx**2 + vy**2) ** 0.5
        if speed < _SWIPE_VELOCITY_THRESHOLD:
            return None

        # Check axis dominance to reject diagonal jitter
        abs_vx, abs_vy = abs(vx), abs(vy)
        if abs_vx > abs_vy and abs_vx > abs_vy * _SWIPE_AXIS_RATIO:
            return "SWIPE_LEFT" if vx < 0 else "SWIPE_RIGHT"
        if abs_vy > abs_vx and abs_vy > abs_vx * _SWIPE_AXIS_RATIO:
            return "SWIPE_UP" if vy < 0 else "SWIPE_DOWN"

        return None

    def _detect_push_pull(self) -> Optional[str]:
        """Detect push (toward camera, Z decreases) or pull (away, Z increases)."""
        if len(self._buffer) < _SWIPE_MIN_FRAMES:
            return None

        z_vals = [f.wrist_z_m for f in self._buffer if f.wrist_z_m is not None]
        if len(z_vals) < _SWIPE_MIN_FRAMES:
            return None   # no LiDAR data

        oldest = self._buffer[0]
        newest = self._buffer[-1]
        dt = newest.ts_mono - oldest.ts_mono
        if dt < 0.05 or oldest.wrist_z_m is None or newest.wrist_z_m is None:
            return None

        dz = newest.wrist_z_m - oldest.wrist_z_m
        vz = dz / dt   # metres/second

        if abs(vz) < _PUSH_VELOCITY_THRESHOLD:
            return None

        return "PUSH" if vz < 0 else "PULL"

    # ---------------------------------------------------------------------- #
    # Pinch detection (static pose + optional LiDAR)
    # ---------------------------------------------------------------------- #

    def _handle_pinch(
        self, lm, conf: float, hand_label: str, ix: float, iy: float
    ) -> Optional[Command]:
        # LiDAR validation: reject 2D pinches where fingers are far apart in depth
        pinch_z_delta_mm: Optional[float] = None
        if self._lidar and self._lidar.is_fresh():
            tx, ty = _tip_xy(lm, _THUMB_TIP)
            d_index = self._lidar.get_depth_at(ix, iy)
            d_thumb = self._lidar.get_depth_at(tx, ty)
            if d_index is not None and d_thumb is not None:
                pinch_z_delta_mm = abs(d_index - d_thumb) * 1000.0
                if pinch_z_delta_mm > _PINCH_THRESH_MM:
                    log.debug("PINCH rejected by LiDAR Z-delta (%.0f mm)", pinch_z_delta_mm)
                    return None

        # First pinch frame — start drag tracking
        if not self._pinch_active:
            self._pinch_active = True
            self._pinch_drag_start = (ix, iy)
            if not self._debounced("PINCH"):
                return None
            return self._emit("PINCH", conf, hand_label, ix, iy,
                              pinch_z_delta_mm=pinch_z_delta_mm,
                              action="CLICK")

        # Pinch held — if hand has moved significantly, emit MOUSEDOWN to start drag
        if self._pinch_drag_start:
            start_x, start_y = self._pinch_drag_start
            dist = ((ix - start_x)**2 + (iy - start_y)**2) ** 0.5
            if dist > 0.04 and not self._debounced("PINCH_DRAG"):
                return self._emit("PINCH_DRAG", conf, hand_label, ix, iy,
                                  action="MOUSEDOWN")

        return None

    def _end_pinch_drag(self) -> Optional[Command]:
        if not self._pinch_active:
            return None
        self._pinch_active = False
        start = self._pinch_drag_start
        self._pinch_drag_start = None
        if start is None:
            return None
        # If drag was in progress, emit MOUSEUP
        last = self._buffer[-1] if self._buffer else None
        if last is None:
            return None
        return self._emit("PINCH_DRAG_END", 1.0, "Right", last.index_x, last.index_y,
                          action="MOUSEUP")

    # ---------------------------------------------------------------------- #
    # Debounce + Command factory
    # ---------------------------------------------------------------------- #

    def _debounced(self, gesture_name: str) -> bool:
        """Return True and record the fire time if outside debounce window."""
        now = time.monotonic()
        if now - self._last_fired.get(gesture_name, 0.0) < self._debounce_s:
            return False
        self._last_fired[gesture_name] = now
        return True

    def _make_command(
        self, gesture_name: str, conf: float, hand_label: str,
        ix: float, iy: float
    ) -> Optional[Command]:
        if not self._debounced(gesture_name):
            return None
        action = _GESTURE_ACTION.get(gesture_name, "CLARIFY")
        params = _GESTURE_PARAMS.get(gesture_name, {})
        return self._emit(gesture_name, conf, hand_label, ix, iy,
                          action=action, extra_params=params)

    def _emit(
        self, gesture_name: str, conf: float, hand_label: str,
        ix: float, iy: float,
        action: str = "CLARIFY",
        pinch_z_delta_mm: Optional[float] = None,
        extra_params: Optional[dict] = None,
    ) -> Command:
        params = dict(extra_params or {})
        params.update({
            "gesture": gesture_name,
            "hand": hand_label,
            "tip_x": ix,
            "tip_y": iy,
        })
        if pinch_z_delta_mm is not None:
            params["pinch_z_delta_mm"] = pinch_z_delta_mm

        log.info("Gesture: %s → %s  conf=%.2f  hand=%s  pos=(%.3f,%.3f)",
                 gesture_name, action, conf, hand_label, ix, iy)

        return Command(
            text=f"gesture:{gesture_name}",
            action=action,
            source="gesture",
            gesture_confidence=conf,
            gaze_coords=None,
            params=params,
        )

    # ---------------------------------------------------------------------- #
    # Status / lifecycle
    # ---------------------------------------------------------------------- #

    def get_status(self) -> dict:
        return {
            "available": self._available,
            "frame_count": self._frame_count,
            "buffer_frames": len(self._buffer),
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


# ---------------------------------------------------------------------------
# Action mapping — spatial gestures → 16-verb vocabulary
# ---------------------------------------------------------------------------

_GESTURE_ACTION: dict[str, str] = {
    "SWIPE_LEFT":     "HOTKEY",      # Alt+Left (browser back / prev file)
    "SWIPE_RIGHT":    "HOTKEY",      # Alt+Right (browser forward / next file)
    "SWIPE_UP":       "SCROLL",      # scroll up
    "SWIPE_DOWN":     "SCROLL",      # scroll down
    "PUSH":           "CLICK",       # click at current cursor position
    "PULL":           "HOTKEY",      # Shift+F10 (right-click / context menu)
    "PINCH":          "CLICK",       # precise click with LiDAR depth validation
    "PINCH_DRAG":     "MOUSEDOWN",   # start drag
    "PINCH_DRAG_END": "MOUSEUP",     # end drag
}

_GESTURE_PARAMS: dict[str, dict] = {
    "SWIPE_LEFT":  {"keys": "alt+left"},
    "SWIPE_RIGHT": {"keys": "alt+right"},
    "SWIPE_UP":    {"direction": "up",   "clicks": 5},
    "SWIPE_DOWN":  {"direction": "down", "clicks": 5},
    "PULL":        {"keys": "shift+f10"},
}
