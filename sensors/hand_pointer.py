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
from collections import deque
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np

from sensors.one_euro_filter import OneEuroFilter


# --------------------------------------------------------------------------- #
# Perspective (homography) mapping — pure, testable
# --------------------------------------------------------------------------- #

def compute_homography(src: list, dst: list) -> Optional[list]:
    """Solve the 3x3 homography H mapping each src (x,y) onto each dst (x,y).

    Four point correspondences (the calibrated hand-corners → the screen
    corners) define a perspective transform that maps the user's trapezoidal
    reach region onto the rectangular screen — so a hand position *inside* the
    trapezoid projects to the right screen point instead of being clamped.

    Returns a 3x3 nested list, or None if the points are degenerate (collinear /
    coincident) so the caller can fall back to the axis-aligned box mapping.
    """
    if len(src) != 4 or len(dst) != 4:
        return None
    A, b = [], []
    for (x, y), (u, v) in zip(src, dst):
        A.append([x, y, 1, 0, 0, 0, -u * x, -u * y]); b.append(u)
        A.append([0, 0, 0, x, y, 1, -v * x, -v * y]); b.append(v)
    try:
        h = np.linalg.solve(np.array(A, dtype=float), np.array(b, dtype=float))
    except np.linalg.LinAlgError:
        return None
    H = [[h[0], h[1], h[2]], [h[3], h[4], h[5]], [h[6], h[7], 1.0]]
    if not np.all(np.isfinite(H)):
        return None
    return H


def apply_homography(H: list, x: float, y: float) -> Optional[tuple]:
    """Project (x, y) through homography H. Returns (px, py) or None if the
    point maps to/behind the projection plane (w≈0)."""
    wx = H[0][0] * x + H[0][1] * y + H[0][2]
    wy = H[1][0] * x + H[1][1] * y + H[1][2]
    w = H[2][0] * x + H[2][1] * y + H[2][2]
    if abs(w) < 1e-9:
        return None
    px, py = wx / w, wy / w
    if not (math.isfinite(px) and math.isfinite(py)):
        return None
    return px, py

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
    # NOTE: invert_x/invert_y apply ONLY to the axis-aligned box fallback. When
    # `corners` is set (perspective mode), the 4-point correspondence already
    # encodes orientation, so the invert flags are ignored.
    invert_x: bool = True
    invert_y: bool = False
    # 4 calibrated hand-corner positions in (nx, ny), ordered TL, TR, BR, BL,
    # captured by `--calibrate-corners`. When present, the pointer maps via a
    # perspective homography (trapezoid → screen rectangle) instead of the
    # axis-aligned box, removing the top-cramped / bottom-stretched distortion.
    corners: Optional[list] = None
    # Overshoot margin (fraction of screen, per edge): the calibrated reach maps
    # to a region this much LARGER than the screen, so reaching NEAR a corner
    # (not the exact calibrated extreme) still lands on it after clamping. Makes
    # corners forgiving for an RA user who can't always hit the awkward extreme.
    # ~0.05-0.08 is a good range; 0 = corners require the exact calibrated reach.
    overshoot: float = 0.0
    # Dwell-to-click.
    dwell_enabled: bool = True
    dwell_time_s: float = 0.5
    dwell_radius_px: float = 40.0    # stay within this to accumulate dwell
    rearm_radius_px: float = 70.0    # move this far from the click point to re-arm
    # OneEuro smoothing (on screen pixels). Lower min_cutoff = smoother at rest
    # (less jitter); beta raises the cutoff with speed (keeps motion responsive).
    min_cutoff: float = 0.4
    beta: float = 0.02
    # Median pre-filter window (frames) on the raw hand centroid. Rejects
    # single-frame landmark glitches that cause the cursor to "jump"; OneEuro
    # smooths jitter but smears spikes, so a small median complements it.
    # 1 = off. 5 = the session-proven value that resolved jitter (a little lag).
    median_window: int = 5
    # Cursor gravity (stickiness near clickable targets). Active only when a
    # gravity_provider is supplied to HandPointer. The cursor is gently pulled
    # toward the nearest target's center — strong when close (snaps), zero at
    # the radius — so small high-value controls (minimize / maximize / close)
    # are easy to land on and resist jitter drift. The user can always pull
    # away (the pull caps at gravity_max_pull_px and never overshoots center).
    gravity_radius_px: float = 90.0
    gravity_max_pull_px: float = 22.0


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
                 move_cb: Optional[Callable[[int, int], None]] = None,
                 gravity_provider: Optional[Callable[[int, int, float],
                                                     Optional[tuple]]] = None,
                 origin: tuple = (0, 0)) -> None:
        import time as _time
        self.sw = int(screen_w)
        self.sh = int(screen_h)
        # Screen-rect origin in OS virtual-desktop coords. (0,0) = single
        # primary monitor; for multi-monitor the validator passes the virtual
        # desktop origin/size so the hand reaches every screen.
        self.ox = int(origin[0])
        self.oy = int(origin[1])
        self.cfg = config or HandPointerConfig()
        self._now = now_fn or _time.monotonic
        self._move_cb = move_cb
        # (x, y, radius) -> nearest clickable target center (cx, cy) or None.
        # None => gravity off (the pre-existing absolute-cursor behaviour).
        self._gravity_provider = gravity_provider
        self._fx = OneEuroFilter(self.cfg.min_cutoff, self.cfg.beta)
        self._fy = OneEuroFilter(self.cfg.min_cutoff, self.cfg.beta)
        # Median pre-filter buffers (raw normalized centroid) — reject 1-frame jumps.
        self._mwin = max(1, int(self.cfg.median_window))
        self._mbufx: deque = deque(maxlen=self._mwin)
        self._mbufy: deque = deque(maxlen=self._mwin)
        # Perspective mapping: build a homography from the 4 calibrated hand
        # corners (TL, TR, BR, BL) onto the screen corners. None => box mode.
        self._H: Optional[list] = None
        self._rebuild_homography()
        self._anchor: Optional[tuple] = None   # (x, y) dwell anchor
        self._anchor_t: float = 0.0
        self._armed: bool = True
        self._click_pt: Optional[tuple] = None  # last click location (for re-arm)
        self._last: Optional[tuple] = None      # last smoothed cursor pos

    def _rebuild_homography(self) -> None:
        """(Re)build the hand→screen homography for the current screen rect.

        Expands the destination rect by the overshoot margin so the calibrated
        hand-corners map slightly BEYOND the screen; reaching near a corner then
        clamps onto it (corners become forgiving). No-op (box mode) without 4
        calibrated corners.
        """
        if self.cfg.corners and len(self.cfg.corners) == 4:
            mx = self.sw * self.cfg.overshoot
            my = self.sh * self.cfg.overshoot
            x0, y0 = self.ox - mx, self.oy - my
            x1, y1 = self.ox + self.sw - 1 + mx, self.oy + self.sh - 1 + my
            dst = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
            self._H = compute_homography([tuple(c) for c in self.cfg.corners], dst)
        else:
            self._H = None

    def set_smoothing(self, min_cutoff: Optional[float] = None,
                      beta: Optional[float] = None,
                      median_window: Optional[int] = None) -> None:
        """Live-update the smoothing params (rebuilds the OneEuro filters and,
        if changed, the median buffer). For interactive tuning of the
        jitter↔latency tradeoff: lower min_cutoff = smoother at rest (more lag);
        higher beta = less lag during fast motion; larger median = fewer 1-frame
        jumps (more lag)."""
        if min_cutoff is not None:
            self.cfg.min_cutoff = max(0.01, float(min_cutoff))
        if beta is not None:
            self.cfg.beta = max(0.0, float(beta))
        if median_window is not None:
            mw = max(1, int(median_window))
            self.cfg.median_window = mw   # keep cfg in sync (overlay + next delta)
            if mw != self._mwin:
                self._mwin = mw
                self._mbufx = deque(maxlen=mw)
                self._mbufy = deque(maxlen=mw)
        self._fx = OneEuroFilter(self.cfg.min_cutoff, self.cfg.beta)
        self._fy = OneEuroFilter(self.cfg.min_cutoff, self.cfg.beta)

    def set_screen_rect(self, origin: tuple, size: tuple) -> None:
        """Retarget the pointer to a different monitor (origin+size in virtual
        desktop coords) and rebuild the mapping. The same calibrated hand reach
        now drives the new monitor. Resets smoothing/dwell so the cursor doesn't
        fling across at the switch."""
        self.ox, self.oy = int(origin[0]), int(origin[1])
        self.sw, self.sh = int(size[0]), int(size[1])
        self._rebuild_homography()
        self.reset()

    def reset(self) -> None:
        """Hand lost — clear dwell state and filters (keeps the cursor where it is)."""
        self._fx.reset()
        self._fy.reset()
        self._mbufx.clear()
        self._mbufy.clear()
        self._anchor = None
        self._armed = True
        self._click_pt = None
        self._last = None

    def _apply_gravity(self, x: int, y: int) -> tuple:
        """Pull the cursor toward the nearest clickable target's center.

        Strength is 1.0 at the target center → 0.0 at gravity_radius_px, and the
        pull is capped at gravity_max_pull_px and never overshoots the center —
        so it assists (and gives the "sticky" feel near min/max/close) without
        hijacking; the user can always move away. No-op when no provider/target.
        """
        if self._gravity_provider is None:
            return x, y
        try:
            c = self._gravity_provider(x, y, self.cfg.gravity_radius_px)
        except Exception:
            return x, y
        if not c:
            return x, y
        cx, cy = c
        dist = math.hypot(cx - x, cy - y)
        if dist <= 1.0:
            return int(round(cx)), int(round(cy))   # settle to center
        strength = max(0.0, 1.0 - dist / float(self.cfg.gravity_radius_px))
        pull = min(self.cfg.gravity_max_pull_px, dist) * strength
        nx = x + (cx - x) * (pull / dist)
        ny = y + (cy - y) * (pull / dist)
        return int(round(nx)), int(round(ny))

    def _map_to_screen(self, nx: float, ny: float) -> tuple:
        # Perspective mode: project through the calibrated homography so a hand
        # position inside the trapezoid maps to the right screen point. Clamp to
        # the screen. Falls through to the box mapping if the projection is
        # degenerate at this point (w≈0).
        if self._H is not None:
            p = apply_homography(self._H, nx, ny)
            if p is not None:
                px = min(self.ox + self.sw - 1, max(float(self.ox), p[0]))
                py = min(self.oy + self.sh - 1, max(float(self.oy), p[1]))
                return px, py
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
        return self.ox + u * (self.sw - 1), self.oy + v * (self.sh - 1)

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

        # Median pre-filter on the raw centroid: a single glitched landmark
        # frame is rejected (the median ignores the outlier) before it can jump
        # the cursor. OneEuro below then smooths the residual jitter.
        if self._mwin > 1:
            self._mbufx.append(nx)
            self._mbufy.append(ny)
            nx = float(np.median(self._mbufx))
            ny = float(np.median(self._mbufy))

        t = self._now()
        rx, ry = self._map_to_screen(nx, ny)
        sx = self._fx(rx, t)
        sy = self._fy(ry, t)
        ix, iy = int(round(sx)), int(round(sy))
        # Gravity is applied AFTER smoothing and is NOT fed back into the filter
        # (the filter keeps tracking the true hand), so it nudges the final
        # cursor toward a nearby target without the filter chasing the snap.
        # Dwell then anchors on the sticky position → easy click on min/max/close.
        ix, iy = self._apply_gravity(ix, iy)
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
