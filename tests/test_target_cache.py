"""Tests for the reworked ClickableTargetCache + FusionEngine cursor gravity.

The cache rework (2026-06-05) fixed the E_POINTER thrash by gating the COM walk
on the foreground-window handle plus a slow heartbeat, with a failure backoff.
These tests cover the pure logic (nearest, walk-gating, backoff) with the
provider mocked, plus the gravity math in FusionEngine._apply_gravity. No real
COM / UIAutomation is touched.

Run with:
    pytest tests/test_target_cache.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "mcp_server"))

from desktop.target_cache import ClickableTargetCache, Target


# ---------------------------------------------------------------------------
# Target geometry
# ---------------------------------------------------------------------------

class TestTarget:
    def test_center(self):
        assert Target("ok", "Button", (0, 0, 100, 50)).center() == (50, 25)

    def test_area(self):
        assert Target("ok", "Button", (0, 0, 100, 50)).area() == 5000

    def test_distance_inside_is_zero(self):
        assert Target("ok", "Button", (0, 0, 100, 100)).distance_to(50, 50) == 0.0

    def test_distance_outside(self):
        # 10 px to the right of a box ending at x=100
        assert Target("ok", "Button", (0, 0, 100, 100)).distance_to(110, 50) == 10.0


# ---------------------------------------------------------------------------
# nearest()
# ---------------------------------------------------------------------------

class TestNearest:
    def _cache_with(self, targets: list[Target]) -> ClickableTargetCache:
        c = ClickableTargetCache()
        c._targets = targets
        return c

    def test_in_radius_returned(self):
        c = self._cache_with([Target("btn", "Button", (100, 100, 200, 150))])
        got = c.nearest(150, 125, radius=90)
        assert got is not None and got.name == "btn"

    def test_out_of_radius_none(self):
        c = self._cache_with([Target("btn", "Button", (1000, 1000, 1100, 1050))])
        assert c.nearest(0, 0, radius=90) is None

    def test_empty_snapshot_none(self):
        assert self._cache_with([]).nearest(0, 0, radius=90) is None

    def test_tie_breaks_to_smaller_area(self):
        # Both rectangles contain the point (distance 0); smaller one wins.
        big = Target("panel", "Button", (0, 0, 400, 400))
        small = Target("ok", "Button", (40, 40, 80, 80))
        c = self._cache_with([big, small])
        got = c.nearest(60, 60, radius=90)
        assert got is not None and got.name == "ok"

    def test_nearest_of_two_in_radius(self):
        near = Target("near", "Button", (100, 100, 120, 120))
        far = Target("far", "Button", (160, 160, 180, 180))
        c = self._cache_with([near, far])
        got = c.nearest(105, 105, radius=200)
        assert got is not None and got.name == "near"


# ---------------------------------------------------------------------------
# walk-gating (_walk_due)
# ---------------------------------------------------------------------------

class TestWalkDue:
    def test_first_iteration_walks(self):
        c = ClickableTargetCache(heartbeat_s=1.5)
        # last_hwnd seeded to -1 → any real hwnd differs → due
        assert c._walk_due(hwnd=12345, last_hwnd=-1, now=0.0, last_walk=0.0)

    def test_unchanged_hwnd_pre_heartbeat_skips(self):
        c = ClickableTargetCache(heartbeat_s=1.5)
        assert not c._walk_due(hwnd=42, last_hwnd=42, now=1.0, last_walk=0.5)

    def test_foreground_change_walks(self):
        c = ClickableTargetCache(heartbeat_s=1.5)
        assert c._walk_due(hwnd=7, last_hwnd=42, now=0.6, last_walk=0.5)

    def test_heartbeat_elapsed_walks(self):
        c = ClickableTargetCache(heartbeat_s=1.5)
        assert c._walk_due(hwnd=42, last_hwnd=42, now=2.1, last_walk=0.5)


# ---------------------------------------------------------------------------
# lifecycle + backoff via a driven _loop with the provider mocked
# ---------------------------------------------------------------------------

class TestLoopBehaviour:
    """Drive _loop on a real thread but with a fake provider + stubbed sleep.

    We stop the loop after a bounded number of iterations by flipping
    _running False from inside the patched time.sleep, so the test never hangs.
    """

    def _run_loop(self, cache, provider, hwnd_seq, max_iters=6):
        """Run cache._loop with comtypes + provider + foreground stubbed.

        hwnd_seq supplies the foreground handle per iteration; sleep counts
        iterations and stops the loop at max_iters.
        """
        state = {"i": 0, "sleeps": []}

        def fake_hwnd():
            i = min(state["i"], len(hwnd_seq) - 1)
            return hwnd_seq[i]

        def fake_sleep(dur):
            state["sleeps"].append(dur)
            state["i"] += 1
            if state["i"] >= max_iters:
                cache._running = False

        fake_comtypes = MagicMock()
        with patch.dict(sys.modules, {"comtypes": fake_comtypes}), \
             patch("desktop.ui_automation.UIAutomationProvider", return_value=provider), \
             patch.object(cache, "_foreground_hwnd", side_effect=fake_hwnd), \
             patch("time.sleep", side_effect=fake_sleep):
            cache._running = True
            cache._loop()
        return state

    def test_unavailable_provider_noops(self):
        cache = ClickableTargetCache()
        provider = MagicMock()
        provider.is_available.return_value = False
        with patch.dict(sys.modules, {"comtypes": MagicMock()}), \
             patch("desktop.ui_automation.UIAutomationProvider", return_value=provider):
            cache._running = True
            cache._loop()
        assert cache._available is False
        assert cache.is_running() is False
        assert cache.snapshot() == []

    def test_walk_only_on_change_or_heartbeat(self):
        cache = ClickableTargetCache(poll_hz=100.0, heartbeat_s=1e9)
        provider = MagicMock()
        provider.is_available.return_value = True
        provider.collect_snap_targets_for_window.return_value = [
            MagicMock(name="e", role="Button", bounds=(0, 0, 10, 10))
        ]
        # Same hwnd every iteration; heartbeat effectively infinite → only the
        # first iteration (hwnd differs from the -1 seed) walks.
        self._run_loop(cache, provider, hwnd_seq=[55], max_iters=5)
        assert provider.collect_snap_targets_for_window.call_count == 1

    def test_walks_each_foreground_change(self):
        cache = ClickableTargetCache(poll_hz=100.0, heartbeat_s=1e9)
        provider = MagicMock()
        provider.is_available.return_value = True
        provider.collect_snap_targets_for_window.return_value = []
        self._run_loop(cache, provider, hwnd_seq=[1, 2, 3, 4, 5], max_iters=5)
        assert provider.collect_snap_targets_for_window.call_count == 5

    def test_backoff_grows_on_failure_and_resets(self):
        cache = ClickableTargetCache(poll_hz=10.0, heartbeat_s=1e9, max_backoff_s=2.0)
        provider = MagicMock()
        provider.is_available.return_value = True
        # Fail, fail, then succeed; new hwnd each iter so a walk is attempted each time.
        provider.collect_snap_targets_for_window.side_effect = [
            RuntimeError("E_POINTER"),
            RuntimeError("E_POINTER"),
            [],
        ]
        state = self._run_loop(cache, provider, hwnd_seq=[1, 2, 3], max_iters=3)
        poll = cache._poll_interval
        # iter 1 failed → backoff = poll; iter 2 failed → backoff = 2*poll;
        # iter 3 succeeded → backoff cleared (sleep == poll).
        assert state["sleeps"][0] == pytest.approx(poll + poll)
        assert state["sleeps"][1] == pytest.approx(poll + 2 * poll)
        assert state["sleeps"][2] == pytest.approx(poll)

    def test_backoff_capped(self):
        cache = ClickableTargetCache(poll_hz=10.0, heartbeat_s=1e9, max_backoff_s=0.25)
        provider = MagicMock()
        provider.is_available.return_value = True
        provider.collect_snap_targets_for_window.side_effect = RuntimeError("boom")
        state = self._run_loop(cache, provider, hwnd_seq=[1, 2, 3, 4, 5, 6], max_iters=6)
        # Every iteration fails; the added backoff component never exceeds the cap.
        poll = cache._poll_interval
        for s in state["sleeps"]:
            assert s <= poll + 0.25 + 1e-9


# ---------------------------------------------------------------------------
# FusionEngine._apply_gravity
# ---------------------------------------------------------------------------

class _FakeCache:
    def __init__(self, target, running=True):
        self._target = target
        self._running = running

    def is_running(self):
        return self._running

    def nearest(self, x, y, radius):
        return self._target


@pytest.fixture
def gravity_engine():
    with patch("pyautogui.moveRel"), patch("pyautogui.moveTo"), \
         patch("pyautogui.position", return_value=MagicMock(x=960, y=540)):
        import pyautogui as pya
        pya.FAILSAFE = False
        pya.PAUSE = 0
        from core.fusion_engine import FusionEngine
        fe = FusionEngine(screen_width=1920, screen_height=1080)
        yield fe


class TestApplyGravity:
    def test_noop_when_no_cache(self, gravity_engine):
        fe = gravity_engine
        fe._target_cache = None
        assert fe._apply_gravity(500, 500) == (500, 500)

    def test_noop_when_cache_not_running(self, gravity_engine):
        fe = gravity_engine
        fe._target_cache = _FakeCache(
            Target("b", "Button", (480, 480, 520, 520)), running=False
        )
        assert fe._apply_gravity(500, 500) == (500, 500)

    def test_noop_when_no_target(self, gravity_engine):
        fe = gravity_engine
        fe._target_cache = _FakeCache(None)
        assert fe._apply_gravity(500, 500) == (500, 500)

    def test_settles_to_center_when_essentially_on_it(self, gravity_engine):
        fe = gravity_engine
        # Target center is (500, 500); cursor within 1px → snap to center.
        fe._target_cache = _FakeCache(Target("b", "Button", (480, 480, 520, 520)))
        assert fe._apply_gravity(500, 500) == (500, 500)

    def test_pulls_toward_center(self, gravity_engine):
        fe = gravity_engine
        fe._gravity_radius = 90
        fe._gravity_max_pull = 18
        # Target center (500, 500); cursor 30px to the left at (470, 500).
        tg = Target("b", "Button", (490, 490, 510, 510))  # center (500, 500)
        fe._target_cache = _FakeCache(tg)
        nx, ny = fe._apply_gravity(470, 500)
        # Pull is along +x only; must move right but never overshoot the center.
        assert 470 < nx <= 500
        assert ny == 500

    def test_pull_never_overshoots_center(self, gravity_engine):
        fe = gravity_engine
        fe._gravity_radius = 90
        fe._gravity_max_pull = 18
        tg = Target("b", "Button", (495, 495, 505, 505))  # center (500, 500)
        fe._target_cache = _FakeCache(tg)
        # Only 5px away — pull is min(max_pull, dist)*strength, capped at dist.
        nx, ny = fe._apply_gravity(495, 500)
        assert 495 <= nx <= 500
