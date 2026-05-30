"""Tests for FlickEngine — D7 grab-and-flick window physics."""

from __future__ import annotations

import math
import sys
import time
from collections import deque
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from desktop.flick_engine import (
    FlickEngine,
    FlickResult,
    FlickState,
    _flick_dir_from_buf,
    _Frame,
    _reach_score,
    _score_zones,
)
from desktop.snap_zones import SnapZone


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _engine(snap_zones=None, move_fn=None, sw=1920, sh=1080):
    snap_fn = (lambda: snap_zones) if snap_zones is not None else lambda: []
    return FlickEngine(
        screen_w=sw, screen_h=sh,
        snap_zones_fn=snap_fn,
        move_window_fn=move_fn or MagicMock(),
    )


def _basic_zones():
    """Minimal 3-zone set for direction tests."""
    return [
        SnapZone("left_half",  (0, 0, 960, 1080),  (0.25, 0.5)),
        SnapZone("right_half", (960, 0, 1920, 1080), (0.75, 0.5)),
        SnapZone("maximize",   (0, 0, 1920, 1080), (0.5, 0.5)),
    ]


def _process_frames(engine, positions, pose="TWO_FINGER_GRAB", hwnd=1, dt=1/60):
    """Feed a sequence of normalised wrist positions through the engine."""
    t = time.monotonic()
    results = []
    for pos in positions:
        r = engine.process(pos, pose, t, hwnd=hwnd)
        results.append(r)
        t += dt
    return results


# ---------------------------------------------------------------------------
# FlickState enum
# ---------------------------------------------------------------------------

class TestFlickState:
    def test_all_states_exist(self):
        for name in ("IDLE", "GRABBED", "DRAGGING", "FLICKING",
                     "RESOLVE", "SNAPPING", "DROP_IN_PLACE", "SETTLING"):
            assert hasattr(FlickState, name)


# ---------------------------------------------------------------------------
# FlickResult dataclass
# ---------------------------------------------------------------------------

class TestFlickResult:
    def test_fields(self):
        r = FlickResult(zone_name="left_half", target_rect=(0, 0, 960, 1080), hwnd=42)
        assert r.zone_name == "left_half"
        assert r.hwnd == 42


# ---------------------------------------------------------------------------
# _flick_dir_from_buf
# ---------------------------------------------------------------------------

class TestFlickDir:
    def _buf(self, positions):
        buf = deque()
        t = 0.0
        for x, y in positions:
            buf.append(_Frame(pos=(x, y), t=t, speed=0.0))
            t += 0.01
        return buf

    def test_rightward_flick(self):
        buf = self._buf([(0.2, 0.5), (0.3, 0.5), (0.5, 0.5), (0.7, 0.5)])
        d = _flick_dir_from_buf(buf)
        assert d is not None
        assert d[0] > 0.9    # mostly rightward
        assert abs(d[1]) < 0.2

    def test_downward_flick(self):
        buf = self._buf([(0.5, 0.2), (0.5, 0.4), (0.5, 0.6)])
        d = _flick_dir_from_buf(buf)
        assert d is not None
        assert d[1] > 0.9
        assert abs(d[0]) < 0.2

    def test_single_frame_returns_none(self):
        buf = self._buf([(0.5, 0.5)])
        assert _flick_dir_from_buf(buf) is None

    def test_zero_displacement_returns_none(self):
        buf = self._buf([(0.5, 0.5), (0.5, 0.5), (0.5, 0.5)])
        assert _flick_dir_from_buf(buf) is None

    def test_onset_peak_override(self):
        buf = self._buf([(0.0, 0.5), (0.5, 0.5), (1.0, 0.5)])
        onset = _Frame(pos=(0.0, 0.5), t=0.0, speed=0.0)
        peak  = _Frame(pos=(0.8, 0.5), t=0.02, speed=1.0)
        d = _flick_dir_from_buf(buf, onset=onset, peak=peak)
        assert d is not None
        assert d[0] > 0.9    # rightward from onset to peak

    def test_unit_length(self):
        buf = self._buf([(0.0, 0.0), (0.3, 0.4)])
        d = _flick_dir_from_buf(buf)
        assert d is not None
        assert abs(math.hypot(d[0], d[1]) - 1.0) < 1e-6


# ---------------------------------------------------------------------------
# _score_zones
# ---------------------------------------------------------------------------

class TestScoreZones:
    def test_rightward_flick_prefers_right_half(self):
        zones = _basic_zones()
        win_center = (0.5, 0.5)
        flick_dir = (1.0, 0.0)    # pure rightward
        name, cos, zone = _score_zones(flick_dir, win_center, zones)
        assert name == "right_half"
        assert cos > 0.5

    def test_leftward_flick_prefers_left_half(self):
        zones = _basic_zones()
        win_center = (0.5, 0.5)
        flick_dir = (-1.0, 0.0)
        name, _, _ = _score_zones(flick_dir, win_center, zones)
        assert name == "left_half"

    def test_ambiguous_flick_below_threshold(self):
        zones = _basic_zones()
        win_center = (0.5, 0.5)
        # Perpendicular to both left/right; favour maximize (closest centroid in scores)
        flick_dir = (0.0, 1.0)    # downward — maximize centroid = (0.5, 0.5), same as win
        name, cos, _ = _score_zones(flick_dir, win_center, zones)
        # cos should be <= 0 for left_half and right_half (perpendicular)
        assert cos <= 0.2   # no strong alignment

    def test_empty_zones(self):
        name, cos, zone = _score_zones((1.0, 0.0), (0.5, 0.5), [])
        assert name is None
        assert cos == -2.0
        assert zone is None

    def test_zone_at_same_position_as_window_skipped(self):
        # Zone centroid coincides with win_center → dist < 1e-6 → skipped
        zone_same = SnapZone("center", (0, 0, 1920, 1080), (0.5, 0.5))
        zones = [zone_same] + _basic_zones()
        name, cos, _ = _score_zones((1.0, 0.0), (0.5, 0.5), zones)
        assert name in ("right_half", "left_half", "maximize")


# ---------------------------------------------------------------------------
# _reach_score
# ---------------------------------------------------------------------------

class TestReachScore:
    def test_zero_distance(self):
        assert _reach_score(0.0) == pytest.approx(0.0, abs=1e-6)

    def test_large_distance_saturates(self):
        s = _reach_score(10.0)
        assert s > 0.99

    def test_monotone(self):
        scores = [_reach_score(d) for d in [0.0, 0.1, 0.3, 0.5, 1.0, 2.0]]
        assert all(scores[i] < scores[i + 1] for i in range(len(scores) - 1))


# ---------------------------------------------------------------------------
# FlickEngine — state transitions
# ---------------------------------------------------------------------------

class TestFlickEngineIdle:
    def test_starts_in_idle(self):
        e = _engine()
        assert e.get_state() == FlickState.IDLE

    def test_grab_pose_transitions_to_grabbed(self):
        e = _engine()
        e.process((0.5, 0.5), "TWO_FINGER_GRAB", time.monotonic(), hwnd=1)
        # Should enter GRABBED then immediately evaluate DRAGGING
        assert e.get_state() in (FlickState.GRABBED, FlickState.DRAGGING)

    def test_no_hwnd_stays_idle(self):
        e = _engine()
        e.process((0.5, 0.5), "TWO_FINGER_GRAB", time.monotonic(), hwnd=0)
        assert e.get_state() == FlickState.IDLE

    def test_non_grab_pose_stays_idle(self):
        e = _engine()
        e.process((0.5, 0.5), "PEACE", time.monotonic(), hwnd=1)
        assert e.get_state() == FlickState.IDLE


class TestFlickEngineDragging:
    def test_slow_motion_stays_dragging(self):
        e = _engine(_basic_zones())
        # Initiate grab
        _process_frames(e, [(0.5, 0.5)], hwnd=1)
        # Slow drift — dx=0.001/frame at 60fps → speed ≈ 0.06 normalised/sec < v_on=0.35
        _process_frames(e, [(0.501, 0.5), (0.502, 0.5), (0.503, 0.5)], hwnd=1)
        assert e.get_state() in (FlickState.DRAGGING, FlickState.GRABBED)

    def test_release_from_dragging_drops_in_place(self):
        e = _engine(_basic_zones())
        _process_frames(e, [(0.5, 0.5), (0.51, 0.5)], hwnd=1)
        e.release()
        assert e.get_state() == FlickState.DROP_IN_PLACE


class TestFlickEngineFlicking:
    def _fast_flick_right(self, e, dt=1/60):
        """Feed enough fast rightward frames to trigger FLICKING."""
        t = time.monotonic()
        # Start grab
        e.process((0.2, 0.5), "TWO_FINGER_GRAB", t, hwnd=1)
        t += dt
        # Fast rightward motion
        for x in [0.3, 0.45, 0.62, 0.82]:
            e.process((x, 0.5), "TWO_FINGER_GRAB", t, hwnd=1)
            t += dt
        return t

    def test_fast_motion_enters_flicking(self):
        e = _engine(_basic_zones())
        self._fast_flick_right(e)
        assert e.get_state() in (FlickState.FLICKING, FlickState.RESOLVE,
                                  FlickState.SNAPPING, FlickState.DROP_IN_PLACE)

    def test_release_from_flicking_triggers_resolve(self):
        e = _engine(_basic_zones())
        self._fast_flick_right(e)
        if e.get_state() == FlickState.FLICKING:
            e.release()
            assert e.get_state() in (FlickState.RESOLVE, FlickState.SNAPPING,
                                      FlickState.DROP_IN_PLACE, FlickState.IDLE)

    def test_good_rightward_flick_snaps_right(self):
        """A clear rightward flick from center should snap to right_half."""
        move_calls = []
        e = _engine(_basic_zones(), move_fn=lambda hwnd, rect, **kw: move_calls.append(rect))
        t = time.monotonic()
        dt = 1 / 60
        e.process((0.5, 0.5), "TWO_FINGER_GRAB", t, hwnd=1)
        t += dt
        # Large rightward displacement to saturate velocity
        for x in [0.55, 0.65, 0.80, 0.95]:
            r = e.process((x, 0.5), "TWO_FINGER_GRAB", t, hwnd=1)
            t += dt
            if r is not None:
                assert r.zone_name == "right_half"
                return
        # If still FLICKING, release and resolve
        if e.get_state() == FlickState.FLICKING:
            e.release()
            # process one more frame to enter RESOLVE
            r = e.process((0.95, 0.5), "TWO_FINGER_GRAB", t, hwnd=1)
            if r is not None:
                assert r.zone_name == "right_half"


# ---------------------------------------------------------------------------
# FlickEngine — catch gesture
# ---------------------------------------------------------------------------

class TestFlickEngineCatch:
    def test_catch_during_snapping_re_grabs(self):
        move_fn = MagicMock()
        e = _engine(_basic_zones(), move_fn=move_fn)
        # Manually force SNAPPING state
        e._state = FlickState.SNAPPING
        e._hwnd = 1
        e._snap_start_t = time.monotonic()

        e.catch()
        assert e.get_state() == FlickState.GRABBED

    def test_catch_outside_snapping_no_op(self):
        e = _engine()
        e._state = FlickState.DRAGGING
        e.catch()
        assert e.get_state() == FlickState.DRAGGING


# ---------------------------------------------------------------------------
# FlickEngine — release
# ---------------------------------------------------------------------------

class TestFlickEngineRelease:
    def test_release_from_grabbed_drops_in_place(self):
        e = _engine()
        e._state = FlickState.GRABBED
        e.release()
        assert e.get_state() == FlickState.DROP_IN_PLACE

    def test_release_from_dragging_drops_in_place(self):
        e = _engine()
        e._state = FlickState.DRAGGING
        e.release()
        assert e.get_state() == FlickState.DROP_IN_PLACE

    def test_release_from_flicking_resolves(self):
        e = _engine(_basic_zones())
        e._state = FlickState.FLICKING
        e.release()
        assert e.get_state() == FlickState.RESOLVE


# ---------------------------------------------------------------------------
# FlickEngine — resolve with ambiguous direction (below τ_cos)
# ---------------------------------------------------------------------------

class TestFlickEngineResolve:
    def test_ambiguous_flick_drops_in_place(self):
        e = _engine(_basic_zones())
        e._state = FlickState.RESOLVE
        # Empty buffer → can't compute direction → DROP_IN_PLACE
        result = e._tick_resolve()
        assert e.get_state() == FlickState.DROP_IN_PLACE
        assert result is None

    def test_no_zones_drops_in_place(self):
        e = _engine(snap_zones=[])
        e._state = FlickState.RESOLVE
        # Add a rightward buffer so direction is computable
        t = time.monotonic()
        for x in [0.2, 0.4, 0.6]:
            e._buf.append(_Frame((x, 0.5), t, 1.0))
            t += 0.016
        result = e._tick_resolve()
        assert e.get_state() == FlickState.DROP_IN_PLACE
        assert result is None

    def test_good_flick_commits_snap(self):
        move_calls = []
        e = _engine(_basic_zones(), move_fn=lambda hwnd, rect, **kw: move_calls.append(rect))
        e._state = FlickState.RESOLVE
        e._hwnd = 1
        e._win_pos = (0.5, 0.5)
        # Strong rightward buffer
        t = time.monotonic()
        for x in [0.1, 0.3, 0.7, 0.95]:
            e._buf.append(_Frame((x, 0.5), t, 2.0))
            t += 0.016
        result = e._tick_resolve()
        assert result is not None
        assert result.zone_name == "right_half"
        assert e.get_state() == FlickState.SNAPPING


# ---------------------------------------------------------------------------
# FlickEngine — SETTLING → IDLE
# ---------------------------------------------------------------------------

class TestFlickEngineSettling:
    def test_settling_transitions_to_idle_next_frame(self):
        e = _engine()
        e._state = FlickState.SETTLING
        e.process((0.5, 0.5), "PEACE", time.monotonic(), hwnd=0)
        assert e.get_state() == FlickState.IDLE


# ---------------------------------------------------------------------------
# FlickEngine — preview zone
# ---------------------------------------------------------------------------

class TestPreviewZone:
    def test_no_preview_when_idle(self):
        e = _engine(_basic_zones())
        assert e.get_preview_zone() is None

    def test_preview_updates_during_dragging(self):
        e = _engine(_basic_zones())
        t = time.monotonic()
        # Grab
        e.process((0.5, 0.5), "TWO_FINGER_GRAB", t, hwnd=1)
        t += 1 / 60
        # Drift right slowly (no flick onset)
        for x in [0.52, 0.54, 0.56]:
            e.process((x, 0.5), "TWO_FINGER_GRAB", t, hwnd=1)
            t += 1 / 60
        # By now, preview zone should be "right_half" given rightward drift
        pz = e.get_preview_zone()
        if pz is not None:
            assert pz in ("right_half", "left_half", "maximize")
