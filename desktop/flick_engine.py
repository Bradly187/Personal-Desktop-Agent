"""desktop.flick_engine — Grab-and-Flick window physics (D7 spec).

State machine that converts raw wrist-tracking events into either a live
window-drag (GRABBED → DRAGGING) or a snap to a zone (FLICKING → SNAPPING).

See D7_flick_to_snap_spec.md for the full rationale.

Public API:
    engine = FlickEngine(screen_w, screen_h)
    engine.process(wrist_pos, pose, timestamp)  # called every camera frame
    engine.catch()                              # FIST during SNAPPING
    engine.release()                            # de-pinch during GRABBED/DRAGGING/FLICKING
    engine.get_preview_zone()                   # zone name winning right now
    engine.get_state()                          # FlickState enum value
"""

from __future__ import annotations

import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable, Deque, Optional, Tuple

from sensors.one_euro_filter import OneEuroFilter

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration constants (all values from D7.6 defaults)
# ---------------------------------------------------------------------------

_V_ON    = 0.35   # m/s — flick onset speed gate
_V_OFF   = 0.15   # m/s — speed-drop segmentation (secondary, not primary)
_TAU_COS = 0.50   # cosine alignment floor for snap commit (≈60°)
_LAMBDA  = 0.15   # reach weight; kept small so direction dominates
_N_WIN   = 5      # velocity ring buffer size (frames)

# 1€ filter defaults for live drag smoothing
_EUR_MIN_CUTOFF = 1.0
_EUR_BETA       = 0.007
_EUR_D_CUTOFF   = 1.0

# Animation duration for SNAPPING state (seconds)
_SNAP_ANIM_DURATION = 0.22   # 180–250 ms ease-out, per spec

# Deceleration window for catch (frames)
_CATCH_DECEL_FRAMES = 2


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

class FlickState(Enum):
    IDLE         = auto()
    GRABBED      = auto()
    DRAGGING     = auto()
    FLICKING     = auto()
    RESOLVE      = auto()
    SNAPPING     = auto()
    DROP_IN_PLACE = auto()
    SETTLING     = auto()


# ---------------------------------------------------------------------------
# Internal data types
# ---------------------------------------------------------------------------

@dataclass
class _Frame:
    pos: Tuple[float, float]
    t: float
    speed: float = 0.0


@dataclass
class FlickResult:
    """Returned by process() when a snap zone has been committed."""
    zone_name: str
    target_rect: Tuple[int, int, int, int]   # (left, top, right, bottom)
    hwnd: int


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class FlickEngine:
    """Wrist-tracking → window grab/drag/flick/snap state machine.

    Coordinate space: normalised [0, 1] × [0, 1] where (0,0) is top-left.
    Caller converts from camera pixels to normalised coords before calling
    process().  Snap zone rects are returned in screen pixels.

    snap_zones_fn: () → list[SnapZone] — called lazily once a flick resolves.
    move_window_fn: (hwnd, rect) → None — called when SNAPPING state fires.
    """

    def __init__(
        self,
        screen_w: int = 1920,
        screen_h: int = 1080,
        snap_zones_fn: Optional[Callable] = None,
        move_window_fn: Optional[Callable] = None,
    ) -> None:
        self._sw = screen_w
        self._sh = screen_h
        self._snap_zones_fn = snap_zones_fn
        self._move_window_fn = move_window_fn

        self._state = FlickState.IDLE
        self._hwnd: int = 0
        self._grab_offset: Tuple[float, float] = (0.0, 0.0)  # normalised

        # 1€ filter for live drag (separate X and Y)
        self._filter_x = OneEuroFilter(_EUR_MIN_CUTOFF, _EUR_BETA, _EUR_D_CUTOFF)
        self._filter_y = OneEuroFilter(_EUR_MIN_CUTOFF, _EUR_BETA, _EUR_D_CUTOFF)

        # Velocity ring buffer
        self._buf: Deque[_Frame] = deque(maxlen=_N_WIN)
        self._prev_pos: Optional[Tuple[float, float]] = None
        self._prev_t: float = 0.0

        # Flick segmentation
        self._onset_frame: Optional[_Frame] = None
        self._peak_frame:  Optional[_Frame] = None

        # Winning zone for live preview
        self._preview_zone: Optional[str] = None

        # Snap animation
        self._snap_start_pos: Tuple[float, float] = (0.0, 0.0)
        self._snap_target_zone: Optional[str] = None
        self._snap_start_t: float = 0.0

        # Window position (normalised, top-left)
        self._win_pos: Tuple[float, float] = (0.0, 0.0)

    # ── Public API ─────────────────────────────────────────────────────────

    @property
    def state(self) -> FlickState:
        return self._state

    def get_state(self) -> FlickState:
        return self._state

    def get_preview_zone(self) -> Optional[str]:
        return self._preview_zone

    def is_active(self) -> bool:
        return self._state not in (FlickState.IDLE, FlickState.SETTLING)

    def process(
        self,
        wrist_pos: Tuple[float, float],
        pose: str,
        timestamp: float,
        hwnd: int = 0,
    ) -> Optional[FlickResult]:
        """Process one camera frame.

        Args:
            wrist_pos: Normalised (x, y) wrist position in [0,1]×[0,1].
            pose: Gesture name from GestureProcessor (e.g. "TWO_FINGER_GRAB",
                  "FIST", "OPEN_PALM", "PEACE").
            timestamp: Monotonic timestamp in seconds (use time.monotonic()).
            hwnd: Win32 window handle to manipulate (0 = active window).

        Returns:
            FlickResult if a snap zone was committed this frame, else None.
        """
        # Compute per-frame velocity *before* filtering position
        speed = self._compute_speed(wrist_pos, timestamp)

        # Run position through 1€ filter
        fx = self._filter_x(wrist_pos[0], timestamp)
        fy = self._filter_y(wrist_pos[1], timestamp)
        filtered = (fx, fy)

        frame = _Frame(pos=wrist_pos, t=timestamp, speed=speed)
        self._buf.append(frame)

        self._prev_pos = wrist_pos
        self._prev_t = timestamp

        result: Optional[FlickResult] = None

        if self._state == FlickState.IDLE:
            result = self._tick_idle(pose, filtered, hwnd)

        elif self._state == FlickState.GRABBED:
            result = self._tick_grabbed(pose, filtered, speed, hwnd)

        elif self._state == FlickState.DRAGGING:
            result = self._tick_dragging(pose, filtered, speed)

        elif self._state == FlickState.FLICKING:
            result = self._tick_flicking(pose, speed)

        elif self._state == FlickState.RESOLVE:
            result = self._tick_resolve()

        elif self._state == FlickState.SNAPPING:
            self._tick_snapping(timestamp)

        elif self._state in (FlickState.DROP_IN_PLACE, FlickState.SETTLING):
            self._state = FlickState.IDLE
            self._preview_zone = None

        return result

    def catch(self) -> None:
        """FIST detected during SNAPPING — re-grab the window mid-flight."""
        if self._state != FlickState.SNAPPING:
            return
        log.info("FlickEngine: CATCH — re-grabbing window during snap")
        self._state = FlickState.GRABBED
        self._reset_velocity_state()

    def release(self) -> None:
        """De-pinch (TWO_FINGER_RELEASE / peace pose) detected.

        From GRABBED: no qualifying flick → DROP_IN_PLACE.
        From DRAGGING: no qualifying flick → DROP_IN_PLACE.
        From FLICKING: evaluate flick → RESOLVE.
        """
        if self._state in (FlickState.GRABBED, FlickState.DRAGGING):
            log.info("FlickEngine: release with no flick → DROP_IN_PLACE")
            self._state = FlickState.DROP_IN_PLACE
        elif self._state == FlickState.FLICKING:
            log.info("FlickEngine: release during flick → RESOLVE")
            self._state = FlickState.RESOLVE

    # ── State tick handlers ─────────────────────────────────────────────────

    def _tick_idle(self, pose: str, filtered: Tuple[float, float], hwnd: int) -> None:
        if pose == "TWO_FINGER_GRAB" and hwnd:
            self._hwnd = hwnd
            self._grab_offset = (
                self._win_pos[0] - filtered[0],
                self._win_pos[1] - filtered[1],
            )
            self._reset_velocity_state()
            self._reset_filters(filtered)
            self._state = FlickState.GRABBED
            log.info("FlickEngine: IDLE → GRABBED  hwnd=%d", hwnd)
        return None

    def _tick_grabbed(
        self,
        pose: str,
        filtered: Tuple[float, float],
        speed: float,
        hwnd: int,
    ) -> None:
        # Keep window latched to hand
        wx = filtered[0] + self._grab_offset[0]
        wy = filtered[1] + self._grab_offset[1]
        self._win_pos = (wx, wy)
        self._move_window_smooth(wx, wy)

        if speed >= _V_ON:
            self._onset_frame = self._buf[-1]
            self._peak_frame  = self._buf[-1]
            self._state = FlickState.FLICKING
            log.debug("FlickEngine: GRABBED → FLICKING  speed=%.3f", speed)
        else:
            self._state = FlickState.DRAGGING
        return None

    def _tick_dragging(
        self,
        pose: str,
        filtered: Tuple[float, float],
        speed: float,
    ) -> None:
        # Keep window latched to hand
        wx = filtered[0] + self._grab_offset[0]
        wy = filtered[1] + self._grab_offset[1]
        self._win_pos = (wx, wy)
        self._move_window_smooth(wx, wy)

        # Update live zone preview (cheap — runs every frame)
        zones = self._get_zones()
        if zones:
            self._preview_zone = _best_zone_name(
                _flick_dir_from_buf(self._buf),
                self._win_pos,
                zones,
            )

        if speed >= _V_ON:
            self._onset_frame = self._buf[-1]
            self._peak_frame  = self._buf[-1]
            self._state = FlickState.FLICKING
            log.debug("FlickEngine: DRAGGING → FLICKING  speed=%.3f", speed)
        return None

    def _tick_flicking(self, pose: str, speed: float) -> None:
        # Track peak speed frame
        if self._peak_frame and speed > self._peak_frame.speed:
            self._peak_frame = self._buf[-1]

        # Update live zone preview
        zones = self._get_zones()
        if zones:
            self._preview_zone = _best_zone_name(
                _flick_dir_from_buf(self._buf),
                self._win_pos,
                zones,
            )
        return None

    def _tick_resolve(self) -> Optional[FlickResult]:
        self._state = FlickState.IDLE  # default; overridden below if snap succeeds
        zones = self._get_zones()
        if not zones:
            log.warning("FlickEngine: RESOLVE — no snap zones available → DROP_IN_PLACE")
            self._state = FlickState.DROP_IN_PLACE
            return None

        flick_dir = _flick_dir_from_buf(self._buf, onset=self._onset_frame, peak=self._peak_frame)
        if flick_dir is None:
            log.info("FlickEngine: RESOLVE — could not compute flick direction → DROP_IN_PLACE")
            self._state = FlickState.DROP_IN_PLACE
            return None

        best_name, best_cos, best_zone = _score_zones(flick_dir, self._win_pos, zones, _LAMBDA)
        log.info(
            "FlickEngine: RESOLVE  dir=(%.2f,%.2f) best=%s cos=%.2f threshold=%.2f",
            flick_dir[0], flick_dir[1], best_name, best_cos, _TAU_COS,
        )

        if best_cos < _TAU_COS:
            log.info("FlickEngine: below alignment floor → DROP_IN_PLACE")
            self._state = FlickState.DROP_IN_PLACE
            self._preview_zone = None
            return None

        # Commit the snap
        self._snap_target_zone = best_name
        self._snap_start_pos = self._win_pos
        self._snap_start_t = time.monotonic()
        self._state = FlickState.SNAPPING
        log.info("FlickEngine: RESOLVE → SNAPPING zone=%s", best_name)

        if self._move_window_fn and best_zone is not None:
            try:
                self._move_window_fn(self._hwnd, best_zone.rect)
            except Exception as exc:
                log.warning("FlickEngine: move_window_fn failed: %s", exc)

        return FlickResult(
            zone_name=best_name,
            target_rect=best_zone.rect if best_zone else (0, 0, 0, 0),
            hwnd=self._hwnd,
        )

    def _tick_snapping(self, timestamp: float) -> None:
        elapsed = timestamp - self._snap_start_t
        if elapsed >= _SNAP_ANIM_DURATION:
            log.debug("FlickEngine: SNAPPING → SETTLING")
            self._state = FlickState.SETTLING
            self._preview_zone = None

    # ── Helpers ────────────────────────────────────────────────────────────

    def _compute_speed(self, pos: Tuple[float, float], t: float) -> float:
        if self._prev_pos is None or self._prev_t == 0.0:
            return 0.0
        dt = t - self._prev_t
        if dt <= 0:
            return 0.0
        dx = pos[0] - self._prev_pos[0]
        dy = pos[1] - self._prev_pos[1]
        # Convert normalised coords to approx metres assuming 0.5m viewing distance
        # (scale factor is constant; only relative comparisons matter for the gate)
        dist_norm = math.hypot(dx, dy)
        return dist_norm / dt   # normalised units/s; threshold _V_ON in same units

    def _reset_velocity_state(self) -> None:
        self._buf.clear()
        self._onset_frame = None
        self._peak_frame  = None
        self._prev_pos    = None
        self._prev_t      = 0.0

    def _reset_filters(self, pos: Tuple[float, float]) -> None:
        self._filter_x = OneEuroFilter(_EUR_MIN_CUTOFF, _EUR_BETA, _EUR_D_CUTOFF)
        self._filter_y = OneEuroFilter(_EUR_MIN_CUTOFF, _EUR_BETA, _EUR_D_CUTOFF)

    def _move_window_smooth(self, nx: float, ny: float) -> None:
        """Move the grabbed window to the filtered hand position (live drag)."""
        if not self._move_window_fn or not self._hwnd:
            return
        # Convert normalised to pixel top-left; preserve window size
        px = int(nx * self._sw)
        py = int(ny * self._sh)
        # We don't have the window size here; caller's move_window_fn handles it
        try:
            self._move_window_fn(self._hwnd, None, drag_pos=(px, py))
        except Exception as exc:
            log.debug("FlickEngine: live drag move failed: %s", exc)

    def _get_zones(self):
        if self._snap_zones_fn is None:
            return []
        try:
            return self._snap_zones_fn()
        except Exception as exc:
            log.debug("FlickEngine: snap_zones_fn failed: %s", exc)
            return []


# ---------------------------------------------------------------------------
# Scoring helpers (module-level so tests can call directly)
# ---------------------------------------------------------------------------

def _flick_dir_from_buf(
    buf: Deque[_Frame],
    onset: Optional[_Frame] = None,
    peak: Optional[_Frame] = None,
) -> Optional[Tuple[float, float]]:
    """Compute the flick direction as a displacement chord (onset → peak).

    Falls back to the oldest → newest frame in the buffer if onset/peak are
    not set (used during DRAGGING live preview).
    """
    if len(buf) < 2:
        return None

    p_start = onset.pos if onset else buf[0].pos
    p_end   = peak.pos  if peak  else buf[-1].pos

    dx = p_end[0] - p_start[0]
    dy = p_end[1] - p_start[1]
    mag = math.hypot(dx, dy)
    if mag < 1e-6:
        return None
    return (dx / mag, dy / mag)


def _score_zones(
    flick_dir: Tuple[float, float],
    win_center: Tuple[float, float],
    zones,
    lam: float = _LAMBDA,
):
    """Score all zones and return (best_name, best_cosine, best_zone).

    zones: list of SnapZone objects with .name, .centroid (normalised), .rect.
    """
    best_name  = None
    best_cos   = -2.0
    best_zone  = None

    for zone in zones:
        cx, cy = zone.centroid
        to_zone_dx = cx - win_center[0]
        to_zone_dy = cy - win_center[1]
        dist = math.hypot(to_zone_dx, to_zone_dy)
        if dist < 1e-6:
            continue
        to_zone_norm = (to_zone_dx / dist, to_zone_dy / dist)

        cos_align = flick_dir[0] * to_zone_norm[0] + flick_dir[1] * to_zone_norm[1]
        reach = lam * _reach_score(dist)
        score = cos_align + reach

        if score > best_cos:
            best_cos  = score
            best_name = zone.name
            best_zone = zone

    return best_name, best_cos, best_zone


def _best_zone_name(
    flick_dir: Optional[Tuple[float, float]],
    win_center: Tuple[float, float],
    zones,
) -> Optional[str]:
    if flick_dir is None or not zones:
        return None
    name, _, _ = _score_zones(flick_dir, win_center, zones)
    return name


def _reach_score(dist: float) -> float:
    """Saturating reach term: larger distance → slightly higher score.

    Keeps the reach contribution small so direction always dominates.
    """
    return math.tanh(dist * 2.0)
