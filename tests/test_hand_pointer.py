"""Tests for HandPointer — absolute mapping + dwell-to-click logic (no hardware)."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from sensors.hand_pointer import (
    HandPointer, HandPointerConfig, fingertip_centroid,
    ThumbClick, ThumbClickConfig, thumb_finger_ratio,
    compute_homography, apply_homography,
)


def _hand(thumb_xy):
    """21-landmark hand: index_MCP(5)..pinky_MCP(17) span = 0.2 (hand scale),
    index tip(8) & middle tip(12) at a fixed spot, thumb tip(4) placed by caller."""
    lm = [(0.0, 0.0)] * 21
    lm[5] = (0.40, 0.50)   # index MCP
    lm[17] = (0.60, 0.50)  # pinky MCP  -> scale = 0.2
    lm[8] = (0.50, 0.40)   # index tip
    lm[12] = (0.52, 0.40)  # middle tip
    lm[4] = thumb_xy       # thumb tip
    return lm


class FakeClock:
    def __init__(self):
        self.t = 0.0
    def __call__(self):
        return self.t
    def advance(self, dt):
        self.t += dt


def _cfg(**kw):
    # No inversion + full-frame input box so screen coords are easy to reason about.
    base = dict(in_x0=0.0, in_y0=0.0, in_x1=1.0, in_y1=1.0, invert_x=False,
                invert_y=False, dwell_time_s=0.5, dwell_radius_px=40.0,
                rearm_radius_px=70.0, min_cutoff=10.0, beta=1.0,
                median_window=1)  # isolate dwell/box logic from the median pre-filter
    base.update(kw)
    return HandPointerConfig(**base)


# --------------------------------------------------------------------------- #
# fingertip_centroid
# --------------------------------------------------------------------------- #

def test_fingertip_centroid_mean_of_tips():
    lm = [(0.0, 0.0)] * 21
    lm[8] = (0.4, 0.4); lm[12] = (0.6, 0.4); lm[16] = (0.4, 0.6); lm[20] = (0.6, 0.6)
    cx, cy = fingertip_centroid(lm)
    assert cx == pytest.approx(0.5)
    assert cy == pytest.approx(0.5)


def test_fingertip_centroid_none_when_too_few():
    assert fingertip_centroid([(0, 0)] * 5) is None
    assert fingertip_centroid(None) is None


# --------------------------------------------------------------------------- #
# absolute mapping
# --------------------------------------------------------------------------- #

def test_maps_center_to_screen_center():
    hp = HandPointer(1000, 800, _cfg(), now_fn=FakeClock())
    ev = hp.update(0.5, 0.5)
    assert ev["x"] == pytest.approx(499, abs=2)
    assert ev["y"] == pytest.approx(399, abs=2)


def _first(cfg, nx, ny):
    # Fresh pointer so the read is OneEuro's first (unsmoothed) sample.
    return HandPointer(1000, 800, cfg, now_fn=FakeClock()).update(nx, ny)


def test_input_box_subregion_maps_to_full_screen():
    # A 0.2..0.8 box: nx=0.2 -> left edge, nx=0.8 -> right edge.
    cfg = _cfg(in_x0=0.2, in_x1=0.8, in_y0=0.2, in_y1=0.8)
    assert _first(cfg, 0.2, 0.2)["x"] == pytest.approx(0, abs=2)
    assert _first(cfg, 0.8, 0.2)["x"] == pytest.approx(999, abs=2)


def test_invert_x_mirrors():
    cfg = _cfg(invert_x=True)
    assert _first(cfg, 0.0, 0.5)["x"] == pytest.approx(999, abs=2)
    assert _first(cfg, 1.0, 0.5)["x"] == pytest.approx(0, abs=2)


def test_clamps_outside_box():
    cfg = _cfg(in_x0=0.2, in_x1=0.8)
    assert _first(cfg, -0.5, 0.5)["x"] == pytest.approx(0, abs=2)
    assert _first(cfg, 2.0, 0.5)["x"] == pytest.approx(999, abs=2)


def test_move_cb_receives_cursor():
    seen = []
    hp = HandPointer(1000, 800, _cfg(), now_fn=FakeClock(),
                     move_cb=lambda x, y: seen.append((x, y)))
    hp.update(0.5, 0.5)
    assert len(seen) == 1


# --------------------------------------------------------------------------- #
# dwell click
# --------------------------------------------------------------------------- #

def test_dwell_fires_after_hold():
    clk = FakeClock()
    hp = HandPointer(1000, 800, _cfg(dwell_time_s=0.5), now_fn=clk)
    assert hp.update(0.5, 0.5)["click"] is False        # anchor set
    clk.advance(0.3)
    ev = hp.update(0.5, 0.5)
    assert ev["click"] is False and 0.0 < ev["dwell_progress"] < 1.0
    clk.advance(0.25)                                   # total 0.55 > 0.5
    assert hp.update(0.5, 0.5)["click"] is True


def test_no_click_if_moving():
    clk = FakeClock()
    hp = HandPointer(1000, 800, _cfg(dwell_time_s=0.5), now_fn=clk)
    hp.update(0.1, 0.1)
    for _ in range(10):
        clk.advance(0.1)
        ev = hp.update(0.1 + _ * 0.05, 0.1)   # keeps moving > radius
        assert ev["click"] is False


def test_does_not_repeat_click_without_rearm():
    clk = FakeClock()
    hp = HandPointer(1000, 800, _cfg(dwell_time_s=0.5), now_fn=clk)
    hp.update(0.5, 0.5)
    clk.advance(0.6)
    assert hp.update(0.5, 0.5)["click"] is True     # first click
    clk_clicks = 0
    for _ in range(10):
        clk.advance(0.6)
        if hp.update(0.5, 0.5)["click"]:
            clk_clicks += 1
    assert clk_clicks == 0   # stays disarmed while hand is still


def test_rearms_after_moving_away_then_clicks_again():
    clk = FakeClock()
    hp = HandPointer(1000, 800, _cfg(dwell_time_s=0.5, rearm_radius_px=70),
                     now_fn=clk)
    hp.update(0.5, 0.5); clk.advance(0.6)
    assert hp.update(0.5, 0.5)["click"] is True      # click 1 at center
    # move far away to re-arm (0.9 -> ~899px, >70px from 499)
    clk.advance(0.1); hp.update(0.9, 0.5)
    # dwell at the new spot
    clk.advance(0.6)
    assert hp.update(0.9, 0.5)["click"] is True      # click 2 after re-arm


# --------------------------------------------------------------------------- #
# ThumbClick (thumb-tip-to-fingers pinch)
# --------------------------------------------------------------------------- #

def test_thumb_ratio_scale_invariant():
    # thumb far from fingers -> large ratio; thumb on the fingers -> small ratio
    far = thumb_finger_ratio(_hand((0.50, 0.80)))     # 0.4 below the tips
    near = thumb_finger_ratio(_hand((0.50, 0.41)))    # right at index/middle tips
    assert far > 1.0
    assert near < 0.2


def test_thumb_click_fires_on_pinch():
    tc = ThumbClick(ThumbClickConfig(close_ratio=0.55, open_ratio=0.85))
    assert tc.update(_hand((0.50, 0.80)))["click"] is False   # open
    assert tc.update(_hand((0.50, 0.41)))["click"] is True    # pinch -> click


def test_thumb_click_no_repeat_until_release():
    tc = ThumbClick(ThumbClickConfig(close_ratio=0.55, open_ratio=0.85))
    tc.update(_hand((0.50, 0.80)))
    assert tc.update(_hand((0.50, 0.41)))["click"] is True
    # held pinched -> no repeat
    for _ in range(5):
        assert tc.update(_hand((0.50, 0.41)))["click"] is False


def test_thumb_click_rearms_after_release():
    tc = ThumbClick(ThumbClickConfig(close_ratio=0.55, open_ratio=0.85))
    tc.update(_hand((0.50, 0.80)))
    assert tc.update(_hand((0.50, 0.41)))["click"] is True     # click 1
    tc.update(_hand((0.50, 0.80)))                             # release (ratio>0.85)
    assert tc.update(_hand((0.50, 0.41)))["click"] is True     # click 2


def test_thumb_click_none_when_no_hand():
    tc = ThumbClick()
    assert tc.update(None)["click"] is False
    assert tc.update([(0, 0)] * 5)["click"] is False


def test_reset_clears_dwell():
    clk = FakeClock()
    hp = HandPointer(1000, 800, _cfg(dwell_time_s=0.5), now_fn=clk)
    hp.update(0.5, 0.5); clk.advance(0.3)
    hp.reset()
    clk.advance(0.3)
    # after reset the anchor is gone; this sample just re-anchors, no click yet
    assert hp.update(0.5, 0.5)["click"] is False


# --------------------------------------------------------------------------- #
# Perspective (homography) mapping  — audit 2026-06-09
# --------------------------------------------------------------------------- #

def test_homography_maps_corners_exactly():
    # A trapezoid (wider at the bottom) → unit screen. Corners must land exactly.
    src = [(0.30, 0.20), (0.70, 0.20), (0.85, 0.80), (0.15, 0.80)]  # TL,TR,BR,BL
    dst = [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 100.0)]
    H = compute_homography(src, dst)
    assert H is not None
    for (sx, sy), (dx, dy) in zip(src, dst):
        px, py = apply_homography(H, sx, sy)
        assert px == pytest.approx(dx, abs=1e-6)
        assert py == pytest.approx(dy, abs=1e-6)


def test_homography_degenerate_returns_none():
    # All four points collinear → no valid perspective transform.
    src = [(0.1, 0.1), (0.2, 0.2), (0.3, 0.3), (0.4, 0.4)]
    dst = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
    assert compute_homography(src, dst) is None


def test_pointer_uses_homography_when_corners_set():
    # Trapezoid corners (TL,TR,BR,BL) on a 1000x1000 screen. The four corners
    # must map to the screen corners regardless of the trapezoid skew, and the
    # CENTER of the trapezoid edges maps sensibly inside the screen.
    sw = sh = 1000
    corners = [(0.30, 0.20), (0.70, 0.20), (0.85, 0.80), (0.15, 0.80)]
    cfg = HandPointerConfig(corners=corners, dwell_enabled=False,
                            min_cutoff=50.0, beta=1.0)
    p = HandPointer(sw, sh, cfg, now_fn=FakeClock())
    assert p._H is not None
    # Top-left hand-corner → screen top-left (~0,0).
    ev = p.update(0.30, 0.20)
    assert ev["x"] == pytest.approx(0, abs=3)
    assert ev["y"] == pytest.approx(0, abs=3)
    # Bottom-right hand-corner → screen bottom-right (~999,999).
    p2 = HandPointer(sw, sh, cfg, now_fn=FakeClock())
    ev2 = p2.update(0.85, 0.80)
    assert ev2["x"] == pytest.approx(sw - 1, abs=3)
    assert ev2["y"] == pytest.approx(sh - 1, abs=3)


def test_pointer_homography_clamps_outside_trapezoid():
    # A point well outside the trapezoid still yields an on-screen, clamped px.
    sw = sh = 1000
    corners = [(0.30, 0.20), (0.70, 0.20), (0.85, 0.80), (0.15, 0.80)]
    cfg = HandPointerConfig(corners=corners, dwell_enabled=False,
                            min_cutoff=50.0, beta=1.0)
    p = HandPointer(sw, sh, cfg, now_fn=FakeClock())
    ev = p.update(0.95, 0.05)   # outside the quad
    assert 0 <= ev["x"] <= sw - 1
    assert 0 <= ev["y"] <= sh - 1


def test_pointer_falls_back_to_box_without_corners():
    # No corners → axis-aligned box mapping still works (regression).
    cfg = _cfg()
    p = HandPointer(1000, 1000, cfg, now_fn=FakeClock())
    assert p._H is None
    ev = p.update(0.5, 0.5)
    assert ev["x"] == pytest.approx(499, abs=2)
    assert ev["y"] == pytest.approx(499, abs=2)


# --------------------------------------------------------------------------- #
# Cursor gravity (stickiness near targets) — audit 2026-06-09
# --------------------------------------------------------------------------- #

def _gcfg(**kw):
    base = dict(in_x0=0.0, in_y0=0.0, in_x1=1.0, in_y1=1.0, invert_x=False,
                invert_y=False, dwell_enabled=False, min_cutoff=1e6, beta=1.0,
                median_window=1, gravity_radius_px=90.0, gravity_max_pull_px=22.0)
    base.update(kw)
    return HandPointerConfig(**base)


def test_gravity_pulls_toward_nearby_target():
    # Target center at screen (500,500); hand maps to (540,500) — 40px away,
    # inside the 90px radius → cursor pulled toward the center (x decreases).
    target = (500, 500)
    prov = lambda x, y, r: target if abs(x - 500) <= r and abs(y - 500) <= r else None
    p = HandPointer(1000, 1000, _gcfg(), now_fn=FakeClock(), gravity_provider=prov)
    ev = p.update(0.54, 0.50)   # → raw (540 - ish, 500)
    assert ev["x"] < 540        # pulled toward 500
    assert ev["x"] >= 500       # never overshoots the center


def test_gravity_snaps_when_essentially_on_target():
    target = (500, 500)
    prov = lambda x, y, r: target
    p = HandPointer(1000, 1000, _gcfg(), now_fn=FakeClock(), gravity_provider=prov)
    ev = p.update(0.5005, 0.5005)   # ~ (500,500), within 1px
    assert ev["x"] == 500 and ev["y"] == 500


def test_gravity_noop_when_no_target_in_range():
    prov = lambda x, y, r: None      # nothing nearby
    p = HandPointer(1000, 1000, _gcfg(), now_fn=FakeClock(), gravity_provider=prov)
    ev = p.update(0.20, 0.20)
    assert ev["x"] == pytest.approx(200, abs=1)   # unchanged by gravity


def test_gravity_pull_capped_and_no_overshoot():
    # Target far within radius (80px); pull must not exceed max_pull (22) and
    # must not cross the center.
    target = (500, 500)
    prov = lambda x, y, r: target
    p = HandPointer(1000, 1000, _gcfg(gravity_max_pull_px=22.0),
                    now_fn=FakeClock(), gravity_provider=prov)
    ev = p.update(0.58, 0.50)    # raw x≈580, 80px from center (within 90 radius)
    # pulled toward 500 but by a capped amount → stays well right of center
    assert 500 < ev["x"] < 580


def test_gravity_off_without_provider():
    p = HandPointer(1000, 1000, _gcfg(), now_fn=FakeClock())  # no provider
    ev = p.update(0.54, 0.50)
    assert ev["x"] == pytest.approx(540, abs=1)   # exact mapping, no pull


def test_overshoot_makes_corner_reachable_short_of_extreme():
    # Square calibration corners; with overshoot, a hand position INSIDE the
    # calibrated quad (short of the extreme) still reaches the screen corner.
    corners = [(0.2, 0.2), (0.8, 0.2), (0.8, 0.8), (0.2, 0.8)]  # TL,TR,BR,BL
    base = dict(corners=corners, dwell_enabled=False, min_cutoff=1e6, beta=1.0,
                median_window=1)
    # No overshoot: hand just inside the TL corner does NOT reach (0,0).
    p0 = HandPointer(1000, 1000, HandPointerConfig(overshoot=0.0, **base),
                     now_fn=FakeClock())
    ev0 = p0.update(0.23, 0.23)   # a bit inside TL
    assert ev0["x"] > 0 and ev0["y"] > 0
    # With overshoot, the same short-of-extreme reach clamps onto (0,0).
    p1 = HandPointer(1000, 1000, HandPointerConfig(overshoot=0.10, **base),
                     now_fn=FakeClock())
    ev1 = p1.update(0.23, 0.23)
    assert ev1["x"] == 0 and ev1["y"] == 0
