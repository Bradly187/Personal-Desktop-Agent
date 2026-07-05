"""GestureProcessor — two-finger spatial gesture recognition.

Designed for a user with rheumatoid arthritis whose comfortable resting pose
is index + middle finger extended (peace sign). All gestures depart from that
base state — the system never fires from a single frozen pose.

━━━ Gesture vocabulary ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Base pose:  ☮ Peace sign — index + middle extended, ring + pinky curled.
            This is the ready / resting state. No action fires from this alone.

From base → swipe:
  PEACE_SWIPE_LEFT   peace sign sweeps left   → HOTKEY Alt+←  (back / prev)
  PEACE_SWIPE_RIGHT  peace sign sweeps right  → HOTKEY Alt+→  (forward / next)
  PEACE_SWIPE_UP     peace sign sweeps up     → SCROLL up
  PEACE_SWIPE_DOWN   peace sign sweeps down   → SCROLL down

From base → grab (window management):
  TWO_FINGER_GRAB    index+middle curl to thumb  → MOUSEDOWN  (grab window)
  TWO_FINGER_RELEASE extend back to peace sign   → MOUSEUP    (pin window)

While grab is held:
  GRAB_SNAP_LEFT     push hand toward camera      → HOTKEY Win+Left
  GRAB_SNAP_RIGHT    pull hand away from camera   → HOTKEY Win+Right
  GRAB_NEXT_MONITOR  strong push (>threshold×2)   → HOTKEY Win+Shift+Right

Open palm (all five fingers extended):
  OPEN_PUSH          push open palm forward       → HOTKEY Win+Shift+Right
  OPEN_PULL          pull open palm back          → HOTKEY Win+Shift+Left
  PINCH              thumb-index close (static)   → CLICK  (single-finger fallback)

━━━ Continuous learning ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Velocity thresholds adapt to the user's observed gesture signature:
  - Every swipe that fires records (gesture, velocity) to gesture_velocity_samples.
  - ContinuousTrainer calibrates per-gesture velocity_floor = p10(observed) each
    adaptation pass, and persists to gesture_velocity_calibration.
  - On pain days (BehavioralTwinState.pain_day_active), thresholds are reduced by
    PAIN_DAY_VELOCITY_FACTOR so slower, smaller gestures still register.
  - GestureProcessor.set_velocity_thresholds(thresholds) receives the calibrated
    dict from ContinuousTrainer after each adaptation cycle.

  The peace-sign jitter (variance in the two-finger spread) is also tracked as
  a secondary pain-day signal — high jitter correlates with inflammation.

━━━ Architecture ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Rolling 500ms frame buffer → wrist XY velocity (swipes) / Z velocity (push-pull)
  Per-gesture-type 800ms debounce.
  Axis dominance ratio 1.8× prevents diagonal jitter from misclassifying.
  All motion detection requires ≥3 frames in the buffer (≥50ms of data).

  LiDAR:
    - Drives push/pull Z-velocity detection.
    - Validates TWO_FINGER_GRAB depth (thumb-index Z-delta < 30mm).
    - Peace-sign fingertip jitter computed from index+middle tip spread.

  Graceful degradation: no mediapipe → disabled; no LiDAR → XY-only (no push/pull).
"""

from __future__ import annotations

import base64
import collections
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from core.command_executor import Command

if TYPE_CHECKING:
    from sensors.lidar_receiver import LiDARReceiver

log = logging.getLogger(__name__)

try:
    import mediapipe as mp
    import numpy as np  # noqa: F401 — the import itself is the availability probe
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
        "mediapipe not installed — GestureProcessor disabled. "
        "pip install mediapipe  (%s)", _mp_import_err
    )

try:
    import cv2
    _CV2_AVAILABLE = True
except ImportError:
    _CV2_AVAILABLE = False
    log.warning("opencv-python not installed — GestureProcessor disabled.")


# ---------------------------------------------------------------------------
# Default thresholds (overridden by ContinuousTrainer calibration at runtime)
# ---------------------------------------------------------------------------

_DEFAULT_SWIPE_VELOCITY: float = 1.2   # normalised coords/second
_DEFAULT_PUSH_VELOCITY:  float = 0.30  # metres/second toward/away from camera
_PAIN_DAY_VELOCITY_FACTOR: float = 0.70  # multiply thresholds on pain days

_CONFIDENCE_MIN:   float = 0.65
_DEBOUNCE_S:       float = 0.80
_BUFFER_WINDOW_MS: float = 500.0
_SWIPE_MIN_FRAMES: int   = 3
_SWIPE_AXIS_RATIO: float = 1.8          # dominant/minor axis minimum ratio

# Grab detection (two-finger pinch)
_GRAB_THRESH_NORM: float = 0.07         # 2D distance, index/middle tips to thumb
_GRAB_THRESH_MM:   float = 30.0         # LiDAR depth delta for grab validation
_GRAB_TRANSLATE_THRESH: float = 0.04   # normalised movement to start drag

# Strong push threshold for next-monitor snap (2× base push)
_STRONG_PUSH_MULTIPLIER: float = 2.0

# Landmark indices (MediaPipe Hands 21-point model)
_WRIST       = 0
_THUMB_TIP   = 4
_INDEX_TIP   = 8
_MIDDLE_TIP  = 12
_RING_TIP    = 16
_PINKY_TIP   = 20
_THUMB_IP    = 2
_INDEX_MCP   = 5
_MIDDLE_MCP  = 9
_RING_MCP    = 13
_PINKY_MCP   = 17
_PALM_CENTRE = 9   # middle MCP ≈ palm centre


# ---------------------------------------------------------------------------
# Frame snapshot for the rolling buffer
# ---------------------------------------------------------------------------

@dataclass
class _FrameSnap:
    ts_mono:    float
    wrist_x:    float
    wrist_y:    float
    index_x:    float
    index_y:    float
    middle_x:   float
    middle_y:   float
    thumb_x:    float
    thumb_y:    float
    wrist_z_m:  Optional[float]   # LiDAR depth at wrist (metres)
    confidence: float
    # Two-finger jitter: std dev of index-middle spread over the buffer.
    # High jitter → inflammation signal for pain day engine.
    two_finger_spread: float       # normalised coords, index_tip↔middle_tip


# ---------------------------------------------------------------------------
# Landmark helpers (Tasks API: lm is List[NormalizedLandmark])
# ---------------------------------------------------------------------------

def _xy(lm, idx: int) -> tuple[float, float]:
    pt = lm[idx]
    return pt.x, pt.y

def _tip_y(lm, idx: int) -> float:
    return lm[idx].y

def _dist(lm, a: int, b: int) -> float:
    ax, ay = _xy(lm, a)
    bx, by = _xy(lm, b)
    return ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5

def _extended(lm, tip: int, mcp: int) -> bool:
    return _tip_y(lm, tip) < _tip_y(lm, mcp)

# ── Pose classifiers ──────────────────────────────────────────────────────

def _peace_sign(lm) -> bool:
    """Index + middle extended; ring + pinky curled. Thumb state free."""
    return (
        _extended(lm, _INDEX_TIP,  _INDEX_MCP) and
        _extended(lm, _MIDDLE_TIP, _MIDDLE_MCP) and
        not _extended(lm, _RING_TIP,  _RING_MCP) and
        not _extended(lm, _PINKY_TIP, _PINKY_MCP)
    )

def _open_palm(lm) -> bool:
    """Three or more fingers extended (generous threshold for push/pull)."""
    return sum(
        1 for tip, mcp in [
            (_INDEX_TIP, _INDEX_MCP), (_MIDDLE_TIP, _MIDDLE_MCP),
            (_RING_TIP, _RING_MCP),   (_PINKY_TIP, _PINKY_MCP),
        ]
        if _extended(lm, tip, mcp)
    ) >= 3

def _two_finger_pinch(lm) -> bool:
    """Both index and middle tips close to thumb — the 'grab' pose."""
    return (
        _dist(lm, _INDEX_TIP,  _THUMB_TIP) < _GRAB_THRESH_NORM and
        _dist(lm, _MIDDLE_TIP, _THUMB_TIP) < _GRAB_THRESH_NORM * 1.3
    )

def _single_finger_pinch(lm) -> bool:
    """Classic index-only pinch fallback."""
    return _dist(lm, _INDEX_TIP, _THUMB_TIP) < 0.06

def _fist(lm) -> bool:
    """All four fingers curled — used as catch gesture during SNAPPING."""
    return not any(
        _extended(lm, tip, mcp)
        for tip, mcp in [
            (_INDEX_TIP, _INDEX_MCP), (_MIDDLE_TIP, _MIDDLE_MCP),
            (_RING_TIP, _RING_MCP),   (_PINKY_TIP, _PINKY_MCP),
        ]
    )


# ---------------------------------------------------------------------------
# GestureProcessor
# ---------------------------------------------------------------------------

class GestureProcessor:
    """Two-finger spatial gesture recognition for desktop control."""

    _MODEL_PATH: str = "sensors/hand_landmarker.task"

    def __init__(
        self,
        confidence_min: float = _CONFIDENCE_MIN,
        debounce_s: float = _DEBOUNCE_S,
    ) -> None:
        self._conf_min  = confidence_min
        self._debounce_s = debounce_s
        self._lidar: Optional["LiDARReceiver"] = None
        self._hands = None
        self._available = _MP_AVAILABLE and _CV2_AVAILABLE

        # Adaptive velocity thresholds — updated by ContinuousTrainer
        self._velocity_thresholds: dict[str, float] = {}
        self._pain_day_active: bool = False

        # Rolling frame buffer
        self._buffer: collections.deque[_FrameSnap] = collections.deque()

        # Per-gesture debounce
        self._last_fired: dict[str, float] = {}

        # Gestures disabled by user assessment (GestureAssessmentSheet → iPad → bridge)
        # Maps base gesture names: "PINCH", "POINT", "OPEN_PALM", "FIST"
        self._disabled_gestures: set[str] = set()

        # Grab (MOUSEDOWN) state
        self._grabbing: bool = False
        self._grab_origin: Optional[tuple[float, float]] = None

        # Velocity sample queue for trainer to drain
        # Each entry: (gesture_name, observed_velocity)
        self._pending_velocity_samples: list[tuple[str, float]] = []

        self._frame_count = 0
        self.latest_landmarks: Optional[list[tuple[float, float]]] = None

        # FlickEngine — wired by set_flick_engine(); receives wrist position
        # every frame while the grab-snap branch is active.
        self._flick_engine = None
        # SensorViewer — wired for FlickEngine debug overlay
        self._viewer = None

    # ── Wiring ────────────────────────────────────────────────────────────

    def set_lidar(self, lidar: "LiDARReceiver") -> None:
        self._lidar = lidar

    def set_flick_engine(self, engine) -> None:
        """Wire the D7 FlickEngine. Pass None to disable flick-to-snap."""
        self._flick_engine = engine

    def set_viewer(self, viewer) -> None:
        """Wire SensorViewer for FlickEngine debug panel."""
        self._viewer = viewer

    def apply_pain_day(self, active: bool) -> None:
        """Apply or remove the pain-day velocity-floor relaxation immediately.

        Called by BehavioralTwinState on every pain-day transition so the
        x0.70 floor (_PAIN_DAY_VELOCITY_FACTOR, applied lazily in
        _swipe_threshold/_push_threshold) takes effect at once instead of
        lagging up to one ContinuousTrainer tick (~60s). The trainer keeps
        ownership of the base thresholds; this only flips the multiplier.
        """
        if active == self._pain_day_active:
            return  # idempotent
        self._pain_day_active = active
        log.info("GestureProcessor: pain-day %s", "ON" if active else "OFF")

    def set_velocity_thresholds(
        self,
        thresholds: dict[str, float],
        pain_day_active: bool = False,
    ) -> None:
        """Called by ContinuousTrainer after each calibration pass."""
        self._velocity_thresholds = thresholds
        self._pain_day_active = pain_day_active
        log.debug(
            "GestureProcessor: velocity thresholds updated (%d gestures, pain_day=%s)",
            len(thresholds), pain_day_active,
        )

    def drain_velocity_samples(self) -> list[tuple[str, float]]:
        """Return and clear the pending velocity sample queue.
        Called by ContinuousTrainer.record_success() to persist samples."""
        samples = list(self._pending_velocity_samples)
        self._pending_velocity_samples.clear()
        return samples

    # ── Threshold helpers ─────────────────────────────────────────────────

    def _swipe_threshold(self) -> float:
        base = self._velocity_thresholds.get("SWIPE", _DEFAULT_SWIPE_VELOCITY)
        return base * (_PAIN_DAY_VELOCITY_FACTOR if self._pain_day_active else 1.0)

    def _push_threshold(self) -> float:
        base = self._velocity_thresholds.get("PUSH", _DEFAULT_PUSH_VELOCITY)
        return base * (_PAIN_DAY_VELOCITY_FACTOR if self._pain_day_active else 1.0)

    # ── HandLandmarker (lazy) ─────────────────────────────────────────────

    def _get_hands(self):
        if self._hands is None and _MP_AVAILABLE:
            import os
            model_path = self._MODEL_PATH
            if not os.path.exists(model_path):
                model_path = os.path.join(os.path.dirname(__file__), self._MODEL_PATH)
            if not os.path.exists(model_path):
                log.warning("GestureProcessor: hand_landmarker.task not found")
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
            log.info("GestureProcessor: HandLandmarker loaded")
        return self._hands

    # ── Public entry point ────────────────────────────────────────────────

    def on_camera_frame(self, msg: dict) -> Optional[Command]:
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

    # ── Core frame processing ─────────────────────────────────────────────

    def _process_frame(self, bgr_frame) -> Optional[Command]:
        hands = self._get_hands()
        if hands is None:
            return None

        rgb = cv2.cvtColor(bgr_frame, cv2.COLOR_BGR2RGB)
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        ts_ms = int(time.monotonic() * 1000)
        result = hands.detect_for_video(mp_image, ts_ms)

        if not result.hand_landmarks:
            self.latest_landmarks = None
            self._prune_buffer()
            if self._grabbing:
                return self._release_grab()
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

        # LiDAR depth at wrist
        wx, wy = _xy(lm, _WRIST)
        wrist_z = None
        if self._lidar and self._lidar.is_fresh():
            wrist_z = self._lidar.get_depth_at(wx, wy)

        # Two-finger spread (jitter signal for pain day)
        ix, iy = _xy(lm, _INDEX_TIP)
        mx, my = _xy(lm, _MIDDLE_TIP)
        spread = ((ix - mx) ** 2 + (iy - my) ** 2) ** 0.5
        tx, ty = _xy(lm, _THUMB_TIP)

        snap = _FrameSnap(
            ts_mono=time.monotonic(),
            wrist_x=wx, wrist_y=wy,
            index_x=ix, index_y=iy,
            middle_x=mx, middle_y=my,
            thumb_x=tx, thumb_y=ty,
            wrist_z_m=wrist_z,
            confidence=conf,
            two_finger_spread=spread,
        )
        self._buffer.append(snap)
        self._prune_buffer()

        # ── Gesture detection (priority order) ───────────────────────────

        # 1. While actively grabbing: check for release, snap, or drag
        if self._grabbing:
            # Release: peace sign restored
            if _peace_sign(lm):
                if self._flick_engine is not None:
                    self._flick_engine.release()
                return self._release_grab(lm, conf, hand_label, ix, iy)

            # FlickEngine path: feed wrist position every frame.
            # If a snap commits, emit SNAP_WINDOW instead of a hotkey.
            if self._flick_engine is not None:
                try:
                    import win32gui as _wg
                    hwnd = _wg.GetForegroundWindow()
                except Exception:
                    hwnd = 0
                flick_result = self._flick_engine.process(
                    wrist_pos=(wx, wy),
                    pose="TWO_FINGER_GRAB",
                    timestamp=time.monotonic(),
                    hwnd=hwnd,
                )
                # Push debug snapshot to SensorViewer overlay
                if self._viewer is not None:
                    try:
                        from desktop.flick_engine import _flick_dir_from_buf, _score_zones
                        from desktop.snap_zones import get_snap_zones
                        fe = self._flick_engine
                        buf = fe._buf
                        win_c = fe._win_pos
                        fd = _flick_dir_from_buf(buf)
                        zones = get_snap_zones(0)
                        if fd and zones:
                            _, _, _ = _score_zones(fd, win_c, zones)
                            scored = sorted(
                                [(z.name, fd[0]*(z.centroid[0]-win_c[0]) + fd[1]*(z.centroid[1]-win_c[1]))
                                 for z in zones],
                                key=lambda x: x[1], reverse=True,
                            )
                        else:
                            scored = []
                        latest_speed = buf[-1].speed if buf else 0.0
                        self._viewer.push_flick_debug(
                            state_name=fe.get_state().name,
                            speed=latest_speed,
                            preview_zone=fe.get_preview_zone(),
                            zone_scores=scored,
                        )
                    except Exception:
                        pass
                if flick_result is not None:
                    log.info(
                        "FlickEngine: committed snap → zone=%s hwnd=%d",
                        flick_result.zone_name, flick_result.hwnd,
                    )
                    return Command(
                        text=f"gesture:SNAP_WINDOW:{flick_result.zone_name}",
                        action="SNAP_WINDOW",
                        source="gesture",
                        gesture_confidence=conf,
                        params={"zone": flick_result.zone_name,
                                "hwnd": flick_result.hwnd},
                    )
                return None   # FlickEngine driving; suppress other gesture output

            # Legacy GRAB_SNAP (push/pull depth, no FlickEngine)
            snap_cmd = self._detect_grab_snap(lm, conf, hand_label, ix, iy)
            if snap_cmd:
                return snap_cmd
            return None   # still grabbing; tilt drives cursor movement

        # 1b. Catch gesture: FIST while FlickEngine is mid-snap
        if self._flick_engine is not None:
            try:
                from desktop.flick_engine import FlickState as _FS
                if self._flick_engine.get_state() == _FS.SNAPPING and _fist(lm):
                    self._flick_engine.catch()
                    log.info("GestureProcessor: FIST catch during SNAPPING")
                    return None
            except Exception:
                pass

        # 2. Two-finger grab initiation (peace → curl both to thumb)
        if _two_finger_pinch(lm):
            return self._initiate_grab(lm, conf, hand_label, ix, iy)

        # 3. Peace sign swipes
        if _peace_sign(lm):
            return self._detect_peace_swipe(conf, hand_label, ix, iy)

        # 4. Open palm push/pull (monitor-level window moves)
        if _open_palm(lm):
            return self._detect_open_push_pull(conf, hand_label, ix, iy)

        # 5. Single-finger pinch fallback (index only)
        if _single_finger_pinch(lm) and not _peace_sign(lm):
            if self._debounced("PINCH"):
                return self._emit("PINCH", "CLICK", conf, hand_label, ix, iy)

        return None

    # ── Grab (MOUSEDOWN / MOUSEUP) ────────────────────────────────────────

    def _initiate_grab(self, lm, conf, hand_label, ix, iy) -> Optional[Command]:
        # LiDAR validation
        if self._lidar and self._lidar.is_fresh():
            tx, ty = _xy(lm, _THUMB_TIP)
            d_i = self._lidar.get_depth_at(ix, iy)
            d_t = self._lidar.get_depth_at(tx, ty)
            if d_i is not None and d_t is not None:
                delta_mm = abs(d_i - d_t) * 1000.0
                if delta_mm > _GRAB_THRESH_MM:
                    log.debug("TWO_FINGER_GRAB rejected — LiDAR Z-delta %.0fmm", delta_mm)
                    return None

        if not self._debounced("TWO_FINGER_GRAB"):
            return None

        self._grabbing = True
        self._grab_origin = (ix, iy)
        log.info("Gesture: TWO_FINGER_GRAB → MOUSEDOWN  hand=%s pos=(%.3f,%.3f)",
                 hand_label, ix, iy)
        return Command(
            text="gesture:TWO_FINGER_GRAB",
            action="MOUSEDOWN",
            source="gesture",
            gesture_confidence=conf,
            params={"gesture": "TWO_FINGER_GRAB", "hand": hand_label,
                    "tip_x": ix, "tip_y": iy},
        )

    def _release_grab(
        self, lm=None, conf=0.9, hand_label="Right", ix=0.0, iy=0.0
    ) -> Optional[Command]:
        self._grabbing = False
        origin = self._grab_origin
        self._grab_origin = None
        if not self._debounced("TWO_FINGER_RELEASE"):
            return None
        log.info("Gesture: TWO_FINGER_RELEASE → MOUSEUP  pos=(%.3f,%.3f)", ix, iy)
        return Command(
            text="gesture:TWO_FINGER_RELEASE",
            action="MOUSEUP",
            source="gesture",
            gesture_confidence=conf,
            params={"gesture": "TWO_FINGER_RELEASE", "hand": hand_label,
                    "tip_x": ix, "tip_y": iy,
                    "drag_distance": self._drag_distance(origin, ix, iy)},
        )

    def _detect_grab_snap(self, lm, conf, hand_label, ix, iy) -> Optional[Command]:
        """Detect push/pull while grab is held → snap window to side."""
        z_vel = self._z_velocity()
        if z_vel is None:
            return None
        threshold = self._push_threshold()
        strong_threshold = threshold * _STRONG_PUSH_MULTIPLIER

        if z_vel < -strong_threshold:
            gesture = "GRAB_NEXT_MONITOR"
            action, params = "HOTKEY", {"keys": "win+shift+right"}
        elif z_vel < -threshold:
            gesture = "GRAB_SNAP_LEFT"
            action, params = "HOTKEY", {"keys": "win+left"}
        elif z_vel > strong_threshold:
            gesture = "GRAB_PREV_MONITOR"
            action, params = "HOTKEY", {"keys": "win+shift+left"}
        elif z_vel > threshold:
            gesture = "GRAB_SNAP_RIGHT"
            action, params = "HOTKEY", {"keys": "win+right"}
        else:
            return None

        if not self._debounced(gesture):
            return None
        self._record_velocity(gesture, abs(z_vel))
        log.info("Gesture: %s → %s %s", gesture, action, params)
        return Command(
            text=f"gesture:{gesture}",
            action=action,
            source="gesture",
            gesture_confidence=conf,
            params={"gesture": gesture, "hand": hand_label, **params},
        )

    # ── Peace sign swipes ─────────────────────────────────────────────────

    def _detect_peace_swipe(self, conf, hand_label, ix, iy) -> Optional[Command]:
        if len(self._buffer) < _SWIPE_MIN_FRAMES:
            return None

        oldest = self._buffer[0]
        newest = self._buffer[-1]
        dt = newest.ts_mono - oldest.ts_mono
        if dt < 0.05:
            return None

        dx = newest.wrist_x - oldest.wrist_x
        dy = newest.wrist_y - oldest.wrist_y
        vx, vy = dx / dt, dy / dt
        speed = (vx ** 2 + vy ** 2) ** 0.5
        threshold = self._swipe_threshold()

        if speed < threshold:
            return None

        abs_vx, abs_vy = abs(vx), abs(vy)
        if abs_vx > abs_vy and abs_vx > abs_vy * _SWIPE_AXIS_RATIO:
            gesture = "PEACE_SWIPE_LEFT" if vx < 0 else "PEACE_SWIPE_RIGHT"
        elif abs_vy > abs_vx and abs_vy > abs_vx * _SWIPE_AXIS_RATIO:
            gesture = "PEACE_SWIPE_UP" if vy < 0 else "PEACE_SWIPE_DOWN"
        else:
            return None

        if not self._debounced(gesture):
            return None

        self._record_velocity(gesture, speed)
        action = _GESTURE_ACTION[gesture]
        extra  = _GESTURE_PARAMS.get(gesture, {})
        log.info("Gesture: %s → %s  vel=%.2f  hand=%s", gesture, action, speed, hand_label)
        return Command(
            text=f"gesture:{gesture}",
            action=action,
            source="gesture",
            gesture_confidence=conf,
            params={"gesture": gesture, "hand": hand_label,
                    "velocity": round(speed, 3), **extra},
        )

    # ── Open palm push/pull (monitor window moves) ─────────────────────────

    def _detect_open_push_pull(self, conf, hand_label, ix, iy) -> Optional[Command]:
        z_vel = self._z_velocity()
        if z_vel is None:
            return None
        threshold = self._push_threshold()

        if z_vel < -threshold:
            gesture = "OPEN_PUSH"
        elif z_vel > threshold:
            gesture = "OPEN_PULL"
        else:
            return None

        if not self._debounced(gesture):
            return None
        self._record_velocity(gesture, abs(z_vel))
        action = _GESTURE_ACTION[gesture]
        extra  = _GESTURE_PARAMS.get(gesture, {})
        log.info("Gesture: %s → %s  z_vel=%.2f m/s", gesture, action, z_vel)
        return Command(
            text=f"gesture:{gesture}",
            action=action,
            source="gesture",
            gesture_confidence=conf,
            params={"gesture": gesture, "hand": hand_label,
                    "velocity": round(abs(z_vel), 3), **extra},
        )

    # ── Helpers ───────────────────────────────────────────────────────────

    def _prune_buffer(self) -> None:
        cutoff = time.monotonic() - _BUFFER_WINDOW_MS / 1000.0
        while self._buffer and self._buffer[0].ts_mono < cutoff:
            self._buffer.popleft()

    def _z_velocity(self) -> Optional[float]:
        """Wrist Z-velocity (m/s) over the buffer. Positive = moving away."""
        if len(self._buffer) < _SWIPE_MIN_FRAMES:
            return None
        z_vals = [(f.ts_mono, f.wrist_z_m) for f in self._buffer if f.wrist_z_m is not None]
        if len(z_vals) < _SWIPE_MIN_FRAMES:
            return None
        dt = z_vals[-1][0] - z_vals[0][0]
        if dt < 0.05:
            return None
        dz = z_vals[-1][1] - z_vals[0][1]
        return dz / dt

    def set_disabled_gestures(self, gestures: set[str]) -> None:
        """Update the set of gestures disabled by user assessment.

        Called by IPadBridge when the iPad sends a gesture_assessment message.
        Gesture names match those from GestureAssessmentSheet:
        "PINCH", "POINT", "OPEN_PALM", "FIST"
        """
        self._disabled_gestures = {g.upper() for g in gestures}
        log.info(
            "GestureProcessor: disabled gestures updated: %s",
            self._disabled_gestures or "none",
        )

    def _debounced(self, gesture: str) -> bool:
        # Respect user-assessed gesture disabilities from onboarding
        base = gesture.split("_")[0]  # "PEACE_SWIPE_UP" → "PEACE" not matched; exact names for 4 basics
        if gesture in self._disabled_gestures or base in self._disabled_gestures:
            log.debug("GestureProcessor: %r disabled by user assessment — skipping", gesture)
            return False
        now = time.monotonic()
        if now - self._last_fired.get(gesture, 0.0) < self._debounce_s:
            return False
        self._last_fired[gesture] = now
        return True

    def _record_velocity(self, gesture: str, velocity: float) -> None:
        self._pending_velocity_samples.append((gesture, velocity))

    @staticmethod
    def _drag_distance(
        origin: Optional[tuple[float, float]], ix: float, iy: float
    ) -> float:
        if origin is None:
            return 0.0
        return ((ix - origin[0]) ** 2 + (iy - origin[1]) ** 2) ** 0.5

    def _emit(
        self, gesture: str, action: str, conf: float,
        hand_label: str, ix: float, iy: float,
    ) -> Command:
        extra = _GESTURE_PARAMS.get(gesture, {})
        return Command(
            text=f"gesture:{gesture}",
            action=action,
            source="gesture",
            gesture_confidence=conf,
            params={"gesture": gesture, "hand": hand_label,
                    "tip_x": ix, "tip_y": iy, **extra},
        )

    def compute_peace_jitter(self) -> Optional[float]:
        """Std-dev of the two-finger spread over the buffer — pain day signal.
        Higher values indicate tremor / inflammation. Returns None if buffer empty."""
        spreads = [f.two_finger_spread for f in self._buffer]
        if len(spreads) < 3:
            return None
        mean = sum(spreads) / len(spreads)
        variance = sum((s - mean) ** 2 for s in spreads) / len(spreads)
        return variance ** 0.5

    def get_status(self) -> dict:
        return {
            "available": self._available,
            "frame_count": self._frame_count,
            "buffer_frames": len(self._buffer),
            "grabbing": self._grabbing,
            "pain_day_active": self._pain_day_active,
            "velocity_thresholds": dict(self._velocity_thresholds),
            "peace_jitter": self.compute_peace_jitter(),
        }

    def close(self) -> None:
        if self._hands is not None:
            try:
                self._hands.close()
            except Exception:
                pass
            self._hands = None


# ---------------------------------------------------------------------------
# Action and parameter maps
# ---------------------------------------------------------------------------

_GESTURE_ACTION: dict[str, str] = {
    "PEACE_SWIPE_LEFT":  "HOTKEY",
    "PEACE_SWIPE_RIGHT": "HOTKEY",
    "PEACE_SWIPE_UP":    "SCROLL",
    "PEACE_SWIPE_DOWN":  "SCROLL",
    "OPEN_PUSH":         "HOTKEY",
    "OPEN_PULL":         "HOTKEY",
    "GRAB_SNAP_LEFT":    "HOTKEY",
    "GRAB_SNAP_RIGHT":   "HOTKEY",
    "GRAB_NEXT_MONITOR": "HOTKEY",
    "GRAB_PREV_MONITOR": "HOTKEY",
    "TWO_FINGER_GRAB":   "MOUSEDOWN",
    "TWO_FINGER_RELEASE":"MOUSEUP",
    "PINCH":             "CLICK",
    "SNAP_WINDOW":       "SNAP_WINDOW",
}

_GESTURE_PARAMS: dict[str, dict] = {
    "PEACE_SWIPE_LEFT":  {"keys": "alt+left"},
    "PEACE_SWIPE_RIGHT": {"keys": "alt+right"},
    "PEACE_SWIPE_UP":    {"direction": "up",   "clicks": 5},
    "PEACE_SWIPE_DOWN":  {"direction": "down", "clicks": 5},
    "OPEN_PUSH":         {"keys": "win+shift+right"},
    "OPEN_PULL":         {"keys": "win+shift+left"},
    "GRAB_SNAP_LEFT":    {"keys": "win+left"},
    "GRAB_SNAP_RIGHT":   {"keys": "win+right"},
    "GRAB_NEXT_MONITOR": {"keys": "win+shift+right"},
    "GRAB_PREV_MONITOR": {"keys": "win+shift+left"},
}
