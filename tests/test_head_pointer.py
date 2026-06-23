"""Tests for sensors/head_pointer.py — pure nose-ray + depth → cursor logic.

No camera / MediaPipe here: HeadPointer takes a head-forward vector (+ optional
nose position and depth), an injected clock, and a fake move callback, so every
mapping/smoothing/parallax/degradation path is exercised deterministically.
"""

import math

import numpy as np
import pytest

from sensors.head_pointer import (
    HeadPointer, HeadPointerConfig,
    head_forward, gnomonic, head_angles, compute_homography, apply_homography,
)


# --------------------------------------------------------------------------- #
# Clock helper — OneEuro ignores a zero dt, so tests must advance time.
# --------------------------------------------------------------------------- #

class Clock:
    def __init__(self, step=1 / 30):
        self.t = 0.0
        self.step = step

    def __call__(self):
        self.t += self.step
        return self.t


def _yaw_matrix(deg):
    a = math.radians(deg)
    m = np.eye(4)
    m[0, 0] = math.cos(a); m[0, 2] = math.sin(a)
    m[2, 0] = -math.sin(a); m[2, 2] = math.cos(a)
    return m


def _pitch_matrix(deg):
    a = math.radians(deg)
    m = np.eye(4)
    m[1, 1] = math.cos(a); m[1, 2] = -math.sin(a)
    m[2, 1] = math.sin(a); m[2, 2] = math.cos(a)
    return m


def fwd(yaw, pitch):
    """Forward vector whose head_angles() == (yaw, pitch). The pointer feature is
    yaw=atan2(fx,fz), pitch=atan2(fy,hypot(fx,fz)); this inverts that."""
    return (math.sin(yaw) * math.cos(pitch),
            math.sin(pitch),
            math.cos(yaw) * math.cos(pitch))


# --------------------------------------------------------------------------- #
# head_forward
# --------------------------------------------------------------------------- #

def test_head_forward_identity_points_at_camera():
    f = head_forward(np.eye(4))
    assert f == pytest.approx((0.0, 0.0, 1.0), abs=1e-9)


def test_head_forward_is_unit_length():
    f = head_forward(_yaw_matrix(30))
    assert math.isclose(math.hypot(*f), 1.0, abs_tol=1e-9)


def test_head_forward_yaw_moves_x():
    f = head_forward(_yaw_matrix(25))
    assert f[0] > 0.0 and f[2] > 0.0


def test_head_forward_pitch_moves_y():
    f = head_forward(_pitch_matrix(25))
    assert abs(f[1]) > 0.0


@pytest.mark.parametrize("bad", [None, [[1, 2], [3, 4]], np.zeros((3, 3))])
def test_head_forward_rejects_bad_shape(bad):
    assert head_forward(bad) is None


def test_head_forward_rejects_non_finite():
    m = np.eye(4); m[0, 0] = np.nan
    assert head_forward(m) is None


def test_head_forward_rejects_zero_rotation():
    assert head_forward(np.zeros((4, 4))) is None


# --------------------------------------------------------------------------- #
# gnomonic
# --------------------------------------------------------------------------- #

def test_gnomonic_center():
    assert gnomonic((0.0, 0.0, 1.0)) == pytest.approx((0.0, 0.0))


def test_gnomonic_projects_ratio():
    assert gnomonic((0.5, -0.25, 1.0)) == pytest.approx((0.5, -0.25))


def test_gnomonic_parallel_to_plane_is_none():
    assert gnomonic((1.0, 0.0, 0.0)) is None


def test_gnomonic_none_and_short_inputs():
    assert gnomonic(None) is None
    assert gnomonic((1.0, 2.0)) is None


def test_gnomonic_non_finite_none():
    assert gnomonic((float("nan"), 0.0, 1.0)) is None


# --------------------------------------------------------------------------- #
# head_angles — the actual pointer feature (monotonic, bounded)
# --------------------------------------------------------------------------- #

def test_head_angles_center():
    assert head_angles((0.0, 0.0, 1.0)) == pytest.approx((0.0, 0.0))


def test_head_angles_monotonic_and_bounded():
    # Increasing yaw must increase monotonically and never blow up (unlike tan).
    prev = -9.0
    for deg in range(-80, 81, 10):
        f = head_forward(_yaw_matrix(deg))
        yaw, _ = head_angles(f)
        assert yaw > prev          # strictly increasing
        assert abs(yaw) < math.pi  # bounded
        prev = yaw


def test_head_angles_center_between_extremes():
    # The neutral pose must sit BETWEEN the left/right extremes (the property the
    # gnomonic feature violated on real data → broke calibration).
    left = head_angles(head_forward(_yaw_matrix(-40)))[0]
    center = head_angles((0.0, 0.0, 1.0))[0]
    right = head_angles(head_forward(_yaw_matrix(40)))[0]
    assert min(left, right) < center < max(left, right)


def test_head_angles_none_inputs():
    assert head_angles(None) is None
    assert head_angles((1.0, 2.0)) is None
    assert head_angles((float("nan"), 0.0, 1.0)) is None


# --------------------------------------------------------------------------- #
# homography helpers
# --------------------------------------------------------------------------- #

def test_homography_maps_unit_square_to_screen():
    src = [(0, 0), (1, 0), (1, 1), (0, 1)]
    dst = [(0, 0), (1920, 0), (1920, 1080), (0, 1080)]
    H = compute_homography(src, dst)
    assert H is not None
    assert apply_homography(H, 0.5, 0.5) == pytest.approx((960, 540), abs=1e-6)


def test_homography_degenerate_returns_none():
    src = [(0, 0), (0, 0), (0, 0), (0, 0)]
    dst = [(0, 0), (1, 0), (1, 1), (0, 1)]
    assert compute_homography(src, dst) is None


def test_homography_wrong_count_returns_none():
    assert compute_homography([(0, 0)], [(0, 0)]) is None


# --------------------------------------------------------------------------- #
# HeadPointer — box mapping
# --------------------------------------------------------------------------- #

def test_box_center_maps_to_screen_center():
    hp = HeadPointer(1920, 1080,
                     HeadPointerConfig(median_window=1, min_cutoff=5.0, beta=1.0),
                     now_fn=Clock())
    ev = None
    for _ in range(20):
        ev = hp.update((0.0, 0.0, 1.0))
    assert ev["x"] == pytest.approx(960, abs=2)
    assert ev["y"] == pytest.approx(540, abs=2)


def test_box_right_aim_moves_cursor_right():
    cfg = HeadPointerConfig(median_window=1, min_cutoff=5.0, beta=1.0)
    hp = HeadPointer(1920, 1080, cfg, now_fn=Clock())
    ev = None
    for _ in range(40):
        ev = hp.update((0.30, 0.0, 1.0))   # u = +0.30 inside in_x1=0.45
    assert ev["x"] > 1100   # right of center


def test_box_invert_x_flips_direction():
    cfg = HeadPointerConfig(median_window=1, min_cutoff=5.0, beta=1.0, invert_x=True)
    hp = HeadPointer(1920, 1080, cfg, now_fn=Clock())
    ev = None
    for _ in range(40):
        ev = hp.update((0.30, 0.0, 1.0))
    assert ev["x"] < 820   # inverted → left of center


def test_box_clamps_to_screen():
    cfg = HeadPointerConfig(median_window=1, min_cutoff=5.0, beta=1.0)
    hp = HeadPointer(1920, 1080, cfg, now_fn=Clock())
    ev = None
    for _ in range(40):
        ev = hp.update((5.0, 5.0, 1.0))   # way past the box
    assert 0 <= ev["x"] <= 1919
    assert 0 <= ev["y"] <= 1079
    assert ev["x"] == 1919   # clamped to right edge


# --------------------------------------------------------------------------- #
# HeadPointer — perspective (calibrated corners)
# --------------------------------------------------------------------------- #

def _corner_cfg():
    # 4 look-features (yaw,pitch) at TL, TR, BR, BL; overshoot 0 for exactness.
    return HeadPointerConfig(
        corners=[(-0.4, -0.3), (0.4, -0.3), (0.4, 0.3), (-0.4, 0.3)],
        overshoot=0.0, median_window=1, min_cutoff=5.0, beta=1.0,
    )


def test_corner_mapping_center_is_screen_center():
    hp = HeadPointer(1920, 1080, _corner_cfg(), now_fn=Clock())
    ev = None
    for _ in range(30):
        ev = hp.update(fwd(0.0, 0.0))
    assert ev["x"] == pytest.approx(960, abs=3)
    assert ev["y"] == pytest.approx(540, abs=3)


def test_corner_mapping_hits_top_left():
    hp = HeadPointer(1920, 1080, _corner_cfg(), now_fn=Clock())
    ev = None
    for _ in range(30):
        ev = hp.update(fwd(-0.4, -0.3))
    assert ev["x"] == pytest.approx(0, abs=3)
    assert ev["y"] == pytest.approx(0, abs=3)


def test_overshoot_makes_corner_forgiving():
    cfg = _corner_cfg(); cfg.overshoot = 0.08
    hp = HeadPointer(1920, 1080, cfg, now_fn=Clock())
    ev = None
    for _ in range(30):
        ev = hp.update(fwd(-0.4, -0.3))   # the calibrated TL extreme
    # With overshoot the extreme maps BEYOND the screen and clamps to the corner.
    assert ev["x"] == 0 and ev["y"] == 0


# --------------------------------------------------------------------------- #
# HeadPointer — depth parallax
# --------------------------------------------------------------------------- #

def test_parallax_noop_without_nose_ref():
    cfg = HeadPointerConfig(depth_comp=0.5, median_window=1, min_cutoff=5.0, beta=1.0)
    hp = HeadPointer(1920, 1080, cfg, now_fn=Clock())
    a = b = None
    for _ in range(30):
        a = hp.update((0.0, 0.0, 1.0), nose_nxy=(0.7, 0.5), depth_m=0.6)
    hp2 = HeadPointer(1920, 1080, cfg, now_fn=Clock())
    for _ in range(30):
        b = hp2.update((0.0, 0.0, 1.0), nose_nxy=(0.3, 0.5), depth_m=0.6)
    assert a["x"] == b["x"]   # no nose_ref → parallax disabled, same result


def test_parallax_shifts_with_lateral_nose_move():
    cfg = HeadPointerConfig(depth_comp=0.5, nose_ref=(0.5, 0.5),
                            median_window=1, min_cutoff=5.0, beta=1.0)
    left = right = None
    hpL = HeadPointer(1920, 1080, cfg, now_fn=Clock())
    hpR = HeadPointer(1920, 1080, cfg, now_fn=Clock())
    for _ in range(40):
        left = hpL.update((0.0, 0.0, 1.0), nose_nxy=(0.3, 0.5), depth_m=0.6)
        right = hpR.update((0.0, 0.0, 1.0), nose_nxy=(0.7, 0.5), depth_m=0.6)
    assert right["x"] != left["x"]   # depth parallax moved the aim


def test_parallax_gated_by_depth_range():
    cfg = HeadPointerConfig(depth_comp=0.5, nose_ref=(0.5, 0.5),
                            depth_min_m=0.3, depth_max_m=1.5,
                            median_window=1, min_cutoff=5.0, beta=1.0)
    hp = HeadPointer(1920, 1080, cfg, now_fn=Clock())
    base = HeadPointer(1920, 1080, cfg, now_fn=Clock())
    out_of_range = same = None
    for _ in range(30):
        out_of_range = hp.update((0.0, 0.0, 1.0), nose_nxy=(0.8, 0.5), depth_m=9.0)
        same = base.update((0.0, 0.0, 1.0), nose_nxy=(0.5, 0.5), depth_m=9.0)
    # depth 9.0 m is outside [0.3, 1.5] → parallax dropped → both at center
    assert out_of_range["x"] == same["x"]


def test_parallax_noop_when_depth_none():
    cfg = HeadPointerConfig(depth_comp=0.5, nose_ref=(0.5, 0.5),
                            median_window=1, min_cutoff=5.0, beta=1.0)
    hp = HeadPointer(1920, 1080, cfg, now_fn=Clock())
    base = HeadPointer(1920, 1080, cfg, now_fn=Clock())
    a = b = None
    for _ in range(30):
        a = hp.update((0.0, 0.0, 1.0), nose_nxy=(0.9, 0.5), depth_m=None)
        b = base.update((0.0, 0.0, 1.0), nose_nxy=(0.5, 0.5), depth_m=None)
    assert a["x"] == b["x"]


# --------------------------------------------------------------------------- #
# HeadPointer — degradation, move_cb, reset, retarget
# --------------------------------------------------------------------------- #

def test_no_face_returns_none_and_no_move():
    moves = []
    hp = HeadPointer(1920, 1080, HeadPointerConfig(),
                     now_fn=Clock(), move_cb=lambda x, y: moves.append((x, y)))
    ev = hp.update(None)
    assert ev == {"x": None, "y": None}
    assert moves == []


def test_extreme_yaw_clamps_not_none():
    # With angle features a 90° yaw (1,0,0) is valid (atan2 — no blow-up) and
    # clamps to the right edge instead of returning None.
    hp = HeadPointer(1920, 1080,
                     HeadPointerConfig(median_window=1, min_cutoff=5.0, beta=1.0),
                     now_fn=Clock())
    ev = None
    for _ in range(20):
        ev = hp.update((1.0, 0.0, 0.0))
    assert ev["x"] == 1919


def test_move_cb_called_with_ints():
    seen = []
    hp = HeadPointer(1920, 1080,
                     HeadPointerConfig(median_window=1),
                     now_fn=Clock(), move_cb=lambda x, y: seen.append((x, y)))
    hp.update((0.0, 0.0, 1.0))
    assert seen and all(isinstance(v, int) for v in seen[0])


def test_reset_clears_filters_keeps_no_crash():
    hp = HeadPointer(1920, 1080, HeadPointerConfig(), now_fn=Clock())
    hp.update((0.1, 0.1, 1.0))
    hp.reset()
    assert hp._last is None
    # next update still works
    assert hp.update((0.0, 0.0, 1.0))["x"] is not None


def test_set_screen_rect_retargets_origin():
    hp = HeadPointer(1920, 1080,
                     HeadPointerConfig(median_window=1, min_cutoff=5.0, beta=1.0),
                     now_fn=Clock())
    hp.set_screen_rect(origin=(1920, 0), size=(1280, 720))
    ev = None
    for _ in range(30):
        ev = hp.update((0.0, 0.0, 1.0))
    # center of the second monitor: 1920 + 1280/2 = 2560
    assert ev["x"] == pytest.approx(2560, abs=3)


def test_set_smoothing_rebuilds_median_buffer():
    hp = HeadPointer(1920, 1080, HeadPointerConfig(median_window=5), now_fn=Clock())
    hp.set_smoothing(median_window=1)
    assert hp._mwin == 1
    assert hp.cfg.median_window == 1


def test_median_rejects_single_frame_glitch():
    cfg = HeadPointerConfig(median_window=5, min_cutoff=50.0, beta=5.0)
    hp = HeadPointer(1920, 1080, cfg, now_fn=Clock())
    xs = []
    for _ in range(6):
        xs.append(hp.update((0.0, 0.0, 1.0))["x"])
    glitch = hp.update((0.45, 0.0, 1.0))["x"]   # one bad frame
    # median of 4 good + 1 glitch keeps the cursor near center, not flung right.
    assert abs(glitch - 960) < 300
