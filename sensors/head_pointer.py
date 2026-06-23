"""HeadPointer — nose-ray + L515 depth -> absolute cursor (no click).

Accessibility-first cursor control for a user with rheumatoid arthritis. The
hand-pointer (fingertip centroid + homography + gravity + thumb-pinch) was
removed: too many coupled variables. Head pose is a single rigid transform, so
the pointer here is built from exactly two signals:

  * head-forward direction  — where the face is aimed (from MediaPipe Face
    Landmarker's 4x4 facial transformation matrix), projected gnomonically onto
    a unit plane to a 2D pointing feature (u, v).
  * nose depth (metres)     — measured by the L515 at the nose tip; used as a
    first-order PARALLAX correction so leaning/shifting the head sideways does
    not drag the aim point. This is the "+depth" half of the nose-ray: it pins
    the ray ORIGIN, while head-forward gives the ray DIRECTION.

There is intentionally NO click logic in this class. Clicking is the existing
voice "click" path (FusionEngine priority 2 — clicks at the current cursor),
which keeps cursor control and clicking fully decoupled.

This class is pure/logic-only and testable: it takes a head-forward vector (+
optional nose position and depth), an injected clock, and moves the cursor
through an injected callback, so tests pass fakes and nothing touches the real
desktop. Camera/MediaPipe live in sensors/face_tracker.py.
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
# (relocated from the deleted hand_pointer.py; still the right tool for mapping
#  a calibrated 4-corner reach region onto the screen rectangle)
# --------------------------------------------------------------------------- #

def compute_homography(src: list, dst: list) -> Optional[list]:
    """Solve the 3x3 homography H mapping each src (x, y) onto each dst (x, y).

    Four point correspondences (the calibrated look-at-corner pointing features →
    the screen corners) define a perspective transform that maps the user's
    trapezoidal "look region" onto the rectangular screen, so an aim inside the
    region projects to the right screen point instead of being clamped.

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


# --------------------------------------------------------------------------- #
# Head-pose helpers — pure, testable
# --------------------------------------------------------------------------- #

# Forward axis of MediaPipe's canonical face model, in face-local coordinates.
# The exact sign/axis convention does NOT need to be correct here: the 4-point
# corner calibration (look TL/TR/BR/BL) absorbs any flip or axis swap into the
# homography. We keep +Z as "out of the face" and let calibration do the rest.
_FACE_FORWARD_LOCAL = (0.0, 0.0, 1.0)


def head_forward(matrix) -> Optional[tuple]:
    """Extract the head-forward unit vector from a 4x4 facial transformation
    matrix (row-major, as MediaPipe returns). Returns (fx, fy, fz) in camera
    space, or None if the matrix is missing/degenerate.

    forward = R · face_local_forward, where R is the top-left 3x3 rotation.
    """
    if matrix is None:
        return None
    try:
        m = np.asarray(matrix, dtype=float)
    except (TypeError, ValueError):
        return None
    if m.shape != (4, 4) or not np.all(np.isfinite(m)):
        return None
    r = m[:3, :3]
    v = r @ np.asarray(_FACE_FORWARD_LOCAL, dtype=float)
    n = float(np.linalg.norm(v))
    if n < 1e-9 or not math.isfinite(n):
        return None
    return float(v[0] / n), float(v[1] / n), float(v[2] / n)


def gnomonic(forward) -> Optional[tuple]:
    """Project a head-forward direction onto the unit plane z=1: the pinhole
    image of where the face points. Returns (u, v) = (fx/fz, fy/fz), or None
    when the direction is parallel to the plane (fz≈0) or non-finite.

    NOTE: the pointer uses head_angles() instead — gnomonic's fx/fz blows up
    (tan) near large head turns, which is non-monotonic and breaks calibration.
    Kept for reference / tests.
    """
    if forward is None or len(forward) < 3:
        return None
    fx, fy, fz = forward[0], forward[1], forward[2]
    if not (math.isfinite(fx) and math.isfinite(fy) and math.isfinite(fz)):
        return None
    if abs(fz) < 1e-6:
        return None
    u, v = fx / fz, fy / fz
    if not (math.isfinite(u) and math.isfinite(v)):
        return None
    return u, v


def head_angles(forward) -> Optional[tuple]:
    """The pointing feature: head yaw & pitch (radians) from the forward vector.

    yaw   = atan2(fx, fz)            — horizontal head turn, left↔right
    pitch = atan2(fy, hypot(fx, fz)) — vertical head tilt, up↔down

    Unlike gnomonic's fx/fz, atan2 is bounded and MONOTONIC across the usable
    range (no tan blow-up at large angles), so the neutral pose always sits
    between the left/right (and up/down) extremes and the box/center-anchor
    mapping stays well-behaved. Returns (yaw, pitch) or None if degenerate.
    """
    if forward is None or len(forward) < 3:
        return None
    fx, fy, fz = forward[0], forward[1], forward[2]
    if not (math.isfinite(fx) and math.isfinite(fy) and math.isfinite(fz)):
        return None
    yaw = math.atan2(fx, fz)
    pitch = math.atan2(fy, math.hypot(fx, fz))
    if not (math.isfinite(yaw) and math.isfinite(pitch)):
        return None
    return yaw, pitch


def _seg(val: float, lo: float, mid: float, hi: float) -> float:
    """Piecewise-linear normalize: lo→0.0, mid→0.5, hi→1.0, clamped to [0, 1].
    Each side has independent gain, so an asymmetric input (mid not the midpoint
    of lo/hi) still maps the center to 0.5. Sign/order agnostic."""
    if (val - mid) * (hi - mid) >= 0:        # on the 'hi' side of mid
        denom = hi - mid
        s = 0.5 + 0.5 * (val - mid) / denom if abs(denom) > 1e-6 else 0.5
    else:                                     # on the 'lo' side of mid
        denom = mid - lo
        s = 0.5 - 0.5 * (mid - val) / denom if abs(denom) > 1e-6 else 0.5
    return min(1.0, max(0.0, s))


@dataclass
class HeadPointerConfig:
    # Axis-aligned box (in gnomonic-feature space) used as the fallback when no
    # 4-corner calibration is present. These bound a comfortable head-aim range;
    # tune live. Mirror flags apply ONLY to this box fallback (the corner
    # homography already encodes orientation, so the flags are ignored there).
    in_x0: float = -0.45
    in_y0: float = -0.30
    in_x1: float = 0.45
    in_y1: float = 0.30
    invert_x: bool = False
    invert_y: bool = False
    # Optional center anchor (gnomonic feature when facing screen center). When
    # set, the box mapping is PIECEWISE-linear: in_x0→screen-left, center_u→
    # screen-center, in_x1→screen-right, with independent gain on each side. This
    # corrects an asymmetric head-yaw→feature curve where the neutral pose is not
    # the midpoint of the left/right extremes (so "face center" → cursor center).
    center_u: Optional[float] = None
    center_v: Optional[float] = None
    # 4 calibrated pointing features (gnomonic u, v) captured while the user
    # looks at each screen corner, ordered TL, TR, BR, BL. When present the
    # pointer maps via a perspective homography instead of the box.
    corners: Optional[list] = None
    # Overshoot margin (fraction of screen, per edge): the calibrated look-region
    # maps to a region this much LARGER than the screen, so aiming NEAR a corner
    # (not the exact calibrated extreme) still lands on it after clamping —
    # forgiving for a user who can't always hold the awkward extreme aim.
    overshoot: float = 0.06
    # Depth parallax compensation (the "+depth" of the nose-ray). 0 = pure
    # head-orientation. >0 shifts the pointing feature by the nose's lateral
    # displacement from the calibration reference, divided by current depth —
    # so leaning sideways or moving closer does not drag the aim. nose_ref is
    # the (nx, ny) captured at the centre of calibration.
    depth_comp: float = 0.15
    nose_ref: Optional[tuple] = None
    # Valid head-distance gate (metres). Depth outside this range is treated as
    # no measurement (parallax term drops to 0) — rejects spurious returns.
    depth_min_m: float = 0.30
    depth_max_m: float = 1.50
    # OneEuro smoothing (on screen pixels). Lower min_cutoff = smoother at rest
    # (less jitter); beta raises the cutoff with speed (keeps motion responsive).
    min_cutoff: float = 0.4
    beta: float = 0.02
    # Median pre-filter window (frames) on the raw pointing feature. Rejects
    # single-frame landmark glitches that would jump the cursor. 1 = off.
    median_window: int = 5


class HeadPointer:
    """Maps a head-forward direction (+ optional nose depth) to an absolute
    cursor position. No click logic — voice "click" handles clicking.

    Args:
        screen_w, screen_h: target screen size in pixels.
        config: HeadPointerConfig.
        now_fn: monotonic clock (injectable for tests).
        move_cb: called with (x, y) ints to move the cursor (e.g. SetCursorPos).
                 None = don't move anything (logic-only / tests).
        origin: screen-rect origin in OS virtual-desktop coords (multi-monitor).
    """

    def __init__(self, screen_w: int, screen_h: int,
                 config: Optional[HeadPointerConfig] = None,
                 now_fn: Callable[[], float] = None,
                 move_cb: Optional[Callable[[int, int], None]] = None,
                 origin: tuple = (0, 0)) -> None:
        import time as _time
        self.sw = int(screen_w)
        self.sh = int(screen_h)
        self.ox = int(origin[0])
        self.oy = int(origin[1])
        self.cfg = config or HeadPointerConfig()
        self._now = now_fn or _time.monotonic
        self._move_cb = move_cb
        self._fx = OneEuroFilter(self.cfg.min_cutoff, self.cfg.beta)
        self._fy = OneEuroFilter(self.cfg.min_cutoff, self.cfg.beta)
        self._mwin = max(1, int(self.cfg.median_window))
        self._mbufu: deque = deque(maxlen=self._mwin)
        self._mbufv: deque = deque(maxlen=self._mwin)
        self._H: Optional[list] = None
        self._rebuild_homography()
        self._last: Optional[tuple] = None   # last smoothed cursor pos

    def _rebuild_homography(self) -> None:
        """(Re)build the look-feature→screen homography for the current screen
        rect, expanding the destination by the overshoot margin so calibrated
        corner aims map slightly BEYOND the screen (corners become forgiving).
        No-op (box mode) without 4 calibrated corners.
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
        if changed, the median buffer) — for interactive jitter↔latency tuning."""
        if min_cutoff is not None:
            self.cfg.min_cutoff = max(0.01, float(min_cutoff))
        if beta is not None:
            self.cfg.beta = max(0.0, float(beta))
        if median_window is not None:
            mw = max(1, int(median_window))
            self.cfg.median_window = mw
            if mw != self._mwin:
                self._mwin = mw
                self._mbufu = deque(maxlen=mw)
                self._mbufv = deque(maxlen=mw)
        self._fx = OneEuroFilter(self.cfg.min_cutoff, self.cfg.beta)
        self._fy = OneEuroFilter(self.cfg.min_cutoff, self.cfg.beta)

    def set_screen_rect(self, origin: tuple, size: tuple) -> None:
        """Retarget the pointer to a different monitor (origin+size in virtual
        desktop coords) and rebuild the mapping. Resets smoothing so the cursor
        doesn't fling across at the switch."""
        self.ox, self.oy = int(origin[0]), int(origin[1])
        self.sw, self.sh = int(size[0]), int(size[1])
        self._rebuild_homography()
        self.reset()

    def reset(self) -> None:
        """Face lost — clear filters (keeps the cursor where it is)."""
        self._fx.reset()
        self._fy.reset()
        self._mbufu.clear()
        self._mbufv.clear()
        self._last = None

    def _parallax(self, u: float, v: float,
                  nose_nxy: Optional[tuple], depth_m: Optional[float]) -> tuple:
        """Shift the pointing feature by the nose's lateral displacement from the
        calibration reference, scaled by 1/depth. First-order correction for the
        ray ORIGIN moving (lean/shift): a sideways head move at distance d shifts
        the screen intersection by ∝ (lateral move)/d. No-op without depth_comp,
        a nose_ref, a nose sample, or an in-range depth (graceful → pure
        orientation).
        """
        c = self.cfg
        if (c.depth_comp <= 0.0 or c.nose_ref is None or nose_nxy is None
                or depth_m is None):
            return u, v
        if not (math.isfinite(depth_m) and c.depth_min_m <= depth_m <= c.depth_max_m):
            return u, v
        du = (nose_nxy[0] - c.nose_ref[0])
        dv = (nose_nxy[1] - c.nose_ref[1])
        k = c.depth_comp / depth_m
        return u + k * du, v + k * dv

    def _map_to_screen(self, u: float, v: float) -> tuple:
        # Perspective mode: project the calibrated look-feature through the
        # homography, then clamp to the screen. Falls through to the box mapping
        # if the projection is degenerate at this point (w≈0).
        if self._H is not None:
            p = apply_homography(self._H, u, v)
            if p is not None:
                px = min(self.ox + self.sw - 1, max(float(self.ox), p[0]))
                py = min(self.oy + self.sh - 1, max(float(self.oy), p[1]))
                return px, py
        c = self.cfg
        # Center-anchored piecewise mapping when a center is calibrated: lo→0.0,
        # mid→0.5, hi→1.0 with independent per-side gain (handles an asymmetric
        # head→feature curve). Otherwise a signed two-point linear map (in_x0/in_x1
        # may be reversed; the signed denominator resolves orientation, no invert).
        if c.center_u is not None:
            s = _seg(u, c.in_x0, c.center_u, c.in_x1)
        else:
            span_x = c.in_x1 - c.in_x0
            span_x = span_x if abs(span_x) > 1e-6 else 1e-6
            s = min(1.0, max(0.0, (u - c.in_x0) / span_x))
        if c.center_v is not None:
            t = _seg(v, c.in_y0, c.center_v, c.in_y1)
        else:
            span_y = c.in_y1 - c.in_y0
            span_y = span_y if abs(span_y) > 1e-6 else 1e-6
            t = min(1.0, max(0.0, (v - c.in_y0) / span_y))
        if c.invert_x:
            s = 1.0 - s
        if c.invert_y:
            t = 1.0 - t
        return self.ox + s * (self.sw - 1), self.oy + t * (self.sh - 1)

    def update(self, forward, nose_nxy: Optional[tuple] = None,
               depth_m: Optional[float] = None) -> dict:
        """Process one head-pose sample. Returns {x, y: int cursor px} (x/y None
        when the sample is unusable — the cursor is left where it is).

        forward  — head-forward (fx, fy, fz) in camera space (None => no face).
        nose_nxy — nose-tip normalized image position (nx, ny) for parallax.
        depth_m  — measured nose depth in metres for parallax (None => orientation
                   only).
        """
        feat = head_angles(forward)
        if feat is None:
            return {"x": None, "y": None}
        u, v = self._parallax(feat[0], feat[1], nose_nxy, depth_m)

        # Median pre-filter on the raw feature: reject a single glitched frame
        # before it can jump the cursor; OneEuro below smooths the residual.
        if self._mwin > 1:
            self._mbufu.append(u)
            self._mbufv.append(v)
            u = float(np.median(self._mbufu))
            v = float(np.median(self._mbufv))

        t = self._now()
        rx, ry = self._map_to_screen(u, v)
        sx = self._fx(rx, t)
        sy = self._fy(ry, t)
        ix, iy = int(round(sx)), int(round(sy))
        self._last = (ix, iy)
        if self._move_cb is not None:
            self._move_cb(ix, iy)
        return {"x": ix, "y": iy}
