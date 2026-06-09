"""Tests for HandPointer — absolute mapping + dwell-to-click logic (no hardware)."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from sensors.hand_pointer import (
    HandPointer, HandPointerConfig, fingertip_centroid,
    ThumbClick, ThumbClickConfig, thumb_finger_ratio,
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
                rearm_radius_px=70.0, min_cutoff=10.0, beta=1.0)
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
