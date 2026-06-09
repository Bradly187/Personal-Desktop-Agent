"""HandPointer — relaxed fingers-together hand -> absolute cursor + dwell click.

Accessibility-first redesign (2026-06-08) for a user with rheumatoid arthritis:
the peace-sign/grab gesture vocabulary demanded painful finger articulation, so it
is replaced for cursor control by a model built around how the hand actually wants
to rest — fingers together, gross whole-hand motion, no finger poses:

  * POINTER  — the fingertip-cluster centroid maps ABSOLUTELY from a comfortable
    camera-space region to the full screen (hand position = cursor position),
    smoothed with the project's OneEuroFilter.
  * CLICK    — DWELL: hold the cursor still (within a small radius) for dwell_time;
    it clicks once, then re-arms only after the hand moves away. Zero force, no
    finger articulation.

This class is pure/logic-only and testable: it takes a normalized centroid and an
injected clock, returns an event dict, and moves the cursor through an injected
callback (so tests pass a fake and nothing touches the real desktop).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Optional

from sensors.one_euro_filter import OneEuroFilter

# Fingertip landmark indices (MediaPipe): index, middle, ring, pinky tips.
# Their mean is a stable pointer when the fingers rest together — and it's the
# part of the hand the L515 captures most cleanly.
FINGERTIP_IDS = (8, 12, 16, 20)
_THUMB_TIP, _INDEX_TIP, _MIDDLE_TIP = 4, 8, 12
_INDEX_MCP, _PINKY_MCP = 5, 17


def hand_scale(landmarks) -> float:
    """Knuckle width (index_MCP..pinky_MCP) — a scale reference that's stable
    under finger flexion, so the pinch test works at any hand-to-camera distance."""
    return math.dist(landmarks[_INDEX_MCP], landmarks[_PINKY_MCP]) or 1e-6


def thumb_finger_ratio(landmarks):
    """Distance from thumb tip to the nearest of index/middle tips, normalized by
    hand scale. Small => thumb is resting against the fingers. None if no hand."""
    if not landmarks or len(landmarks) < 21:
        return None
    d = min(math.dist(landmarks[_THUMB_TIP], landmarks[_INDEX_TIP]),
            math.dist(landmarks[_THUMB_TIP], landmarks[_MIDDLE_TIP]))
    s = hand_scale(landmarks)
    return d / s if s > 1e-6 else None


@dataclass
class ThumbClickConfig:
    close_ratio: float = 0.55   # below this (thumb near fingers) => pinched
    open_ratio: float = 0.85    # above this => released (hysteresis re-arm)


class ThumbClick:
    """Click when the thumb tip comes to rest near the index/middle tips.

    Hysteresis (close_ratio < open_ratio) + arm/disarm so one pinch = one click;
    the thumb must separate past open_ratio before another click can fire.
    """

    def __init__(self, config: Optional["ThumbClickConfig"] = None) -> None:
        self.cfg = config or ThumbClickConfig()
        self._armed = True

    def reset(self) -> None:
        self._armed = True

    def update(self, landmarks) -> dict:
        r = thumb_finger_ratio(landmarks)
        if r is None:
            return {"ratio": None, "pinched": False, "click": False}
        pinched = r < self.cfg.close_ratio
        click = False
        if pinched and self._armed:
            click = True
            self._armed = False
        elif r > self.cfg.open_ratio:
            self._armed = True
        return {"ratio": r, "pinched": pinched, "click": click}


@dataclass
class HandPointerConfig:
    # Comfortable camera-space input box (normalized) that maps to the full
    # screen. Smaller box = less hand travel needed to reach screen edges.
    in_x0: float = 0.18
    in_y0: float = 0.18
    in_x1: float = 0.82
    in_y1: float = 0.82
    # Mirror axes so movement feels natural facing the camera (tune live).
    invert_x: bool = True
    invert_y: bool = False
    # Dwell-to-click.
    dwell_enabled: bool = True
    dwell_time_s: float = 0.5
    dwell_radius_px: float = 40.0    # stay within this to accumulate dwell
    rearm_radius_px: float = 70.0    # move this far from the click point to re-arm
    # OneEuro smoothing (on screen pixels). Lower min_cutoff = smoother at rest
    # (less jitter); beta raises the cutoff with speed (keeps motion responsive).
    min_cutoff: float = 0.6
    beta: float = 0.025


def fingertip_centroid(landmarks) -> Optional[tuple]:
    """Mean (nx, ny) of the fingertip cluster from 21 MediaPipe landmarks."""
    if not landmarks or len(landmarks) < 21:
        return None
    xs = [landmarks[i][0] for i in FINGERTIP_IDS]
    ys = [landmarks[i][1] for i in FINGERTIP_IDS]
    return sum(xs) / len(xs), sum(ys) / len(ys)


class HandPointer:
    """Maps a normalized hand centroid to an absolute cursor position + dwell click.

    Args:
        screen_w, screen_h: target screen size in pixels.
        config: HandPointerConfig.
        now_fn: monotonic clock (injectable for tests).
        move_cb: called with (x, y) ints to move the cursor (e.g. pyautogui.moveTo).
                 None = don't move anything (logic-only / tests).
    """

    def __init__(self, screen_w: int, screen_h: int,
                 config: Optional[HandPointerConfig] = None,
                 now_fn: Callable[[], float] = None,
                 move_cb: Optional[Callable[[int, int], None]] = None) -> None:
        import time as _time
        self.sw = int(screen_w)
        self.sh = int(screen_h)
        self.cfg = config or HandPointerConfig()
        self._now = now_fn or _time.monotonic
        self._move_cb = move_cb
        self._fx = OneEuroFilter(self.cfg.min_cutoff, self.cfg.beta)
        self._fy = OneEuroFilter(self.cfg.min_cutoff, self.cfg.beta)
        self._anchor: Optional[tuple] = None   # (x, y) dwell anchor
        self._anchor_t: float = 0.0
        self._armed: bool = True
        self._click_pt: Optional[tuple] = None  # last click location (for re-arm)
        self._last: Optional[tuple] = None      # last smoothed cursor pos

    def reset(self) -> None:
        """Hand lost — clear dwell state and filters (keeps the cursor where it is)."""
        self._fx.reset()
        self._fy.reset()
        self._anchor = None
        self._armed = True
        self._click_pt = None
        self._last = None

    def _map_to_screen(self, nx: float, ny: float) -> tuple:
        c = self.cfg
        # Normalize within the comfortable input box, clamp to [0, 1].
        u = (nx - c.in_x0) / max(1e-6, (c.in_x1 - c.in_x0))
        v = (ny - c.in_y0) / max(1e-6, (c.in_y1 - c.in_y0))
        u = min(1.0, max(0.0, u))
        v = min(1.0, max(0.0, v))
        if c.invert_x:
            u = 1.0 - u
        if c.invert_y:
            v = 1.0 - v
        return u * (self.sw - 1), v * (self.sh - 1)

    def update(self, nx: float, ny: float, hold: bool = False) -> dict:
        """Process one hand centroid sample. Returns an event dict:

        {x, y: int cursor px; dwell_progress: 0..1; click: bool}

        hold=True freezes the cursor at its last position (no move, no filter
        update) — used while a pinch is in progress so the click lands where the
        user aimed instead of being dragged by the pinching fingers.
        """
        if nx is None or ny is None or not (math.isfinite(nx) and math.isfinite(ny)):
            return {"x": None, "y": None, "dwell_progress": 0.0, "click": False}

        if hold and self._last is not None:
            return {"x": self._last[0], "y": self._last[1],
                    "dwell_progress": 0.0, "click": False}

        t = self._now()
        rx, ry = self._map_to_screen(nx, ny)
        sx = self._fx(rx, t)
        sy = self._fy(ry, t)
        ix, iy = int(round(sx)), int(round(sy))
        self._last = (ix, iy)
        if self._move_cb is not None:
            self._move_cb(ix, iy)

        click = False
        progress = 0.0
        c = self.cfg

        if not c.dwell_enabled:
            return {"x": ix, "y": iy, "dwell_progress": 0.0, "click": False}

        # Re-arm once the hand has moved away from the last click point.
        if not self._armed and self._click_pt is not None:
            if math.dist((ix, iy), self._click_pt) > c.rearm_radius_px:
                self._armed = True

        if self._anchor is None:
            self._anchor = (ix, iy)
            self._anchor_t = t
        elif math.dist((ix, iy), self._anchor) <= c.dwell_radius_px:
            held = t - self._anchor_t
            progress = min(1.0, held / max(1e-3, c.dwell_time_s))
            if progress >= 1.0 and self._armed:
                click = True
                self._armed = False
                self._click_pt = (ix, iy)
                self._anchor = (ix, iy)
                self._anchor_t = t
        else:
            # Moved out of the dwell radius — reset the anchor to follow the hand.
            self._anchor = (ix, iy)
            self._anchor_t = t

        return {"x": ix, "y": iy, "dwell_progress": progress, "click": click}
