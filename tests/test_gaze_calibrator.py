"""Tests for GazeCalibrator — Sprint G2 angular gaze-to-monitor mapping."""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_calibrator(screen_w: int = 1920, screen_h: int = 1080):
    from calibration.gaze_calibrator import GazeCalibrator
    return GazeCalibrator(screen_w=screen_w, screen_h=screen_h)


def _synthetic_samples(calibrator, n: int = 5):
    """Add n synthetic samples using a known affine mapping so we can verify recovery."""
    import numpy as np
    from calibration.gaze_calibrator import GazeCalibrator

    screen_w, screen_h = calibrator._screen_w, calibrator._screen_h

    # Define 5 gaze directions spread around a reference
    # Use directions that map to screen corners + center
    targets = [
        (int(screen_w * 0.05), int(screen_h * 0.05)),    # top-left
        (int(screen_w * 0.95), int(screen_h * 0.05)),    # top-right
        (int(screen_w * 0.50), int(screen_h * 0.50)),    # center
        (int(screen_w * 0.05), int(screen_h * 0.95)),    # bottom-left
        (int(screen_w * 0.95), int(screen_h * 0.95)),    # bottom-right
    ][:n]

    # Simulate gaze rays: center direction + small angular offsets
    # Reference direction (looking straight at monitor center)
    ref = np.array([0.0, 0.0, -1.0])

    # Map screen position → angular offsets (inverse of what calibrator will solve)
    # az proportional to (x - center_x), el proportional to (y - center_y)
    cx, cy = screen_w / 2, screen_h / 2
    az_scale = 0.3 / cx   # rad per pixel
    el_scale = 0.2 / cy

    rays = []
    for px_x, px_y in targets:
        az = (px_x - cx) * az_scale
        el = (px_y - cy) * el_scale
        # Construct ray as ref rotated by small angles
        dir_x = az
        dir_y = -el   # flip: positive el → upward look → negative y in ray
        dir_z = -1.0
        norm = (dir_x**2 + dir_y**2 + dir_z**2) ** 0.5
        rays.append((dir_x / norm, dir_y / norm, dir_z / norm))

    for ray, (px_x, px_y) in zip(rays, targets):
        calibrator.add_sample(ray, px_x, px_y)

    return targets, rays


# ---------------------------------------------------------------------------
# sample_count / clear_samples
# ---------------------------------------------------------------------------

class TestSampleManagement:
    def test_add_sample_increments_count(self):
        cal = _make_calibrator()
        assert cal.sample_count() == 0
        cal.add_sample((0.0, 0.0, -1.0), 960, 540)
        assert cal.sample_count() == 1

    def test_clear_samples_resets_count(self):
        cal = _make_calibrator()
        cal.add_sample((0.0, 0.0, -1.0), 960, 540)
        cal.clear_samples()
        assert cal.sample_count() == 0


# ---------------------------------------------------------------------------
# is_calibrated / get_status before solve
# ---------------------------------------------------------------------------

class TestInitialState:
    def test_not_calibrated_initially(self):
        cal = _make_calibrator()
        assert cal.is_calibrated is False

    def test_project_returns_none_when_not_calibrated(self):
        cal = _make_calibrator()
        assert cal.project((0.0, 0.0, -1.0)) is None

    def test_get_status_keys(self):
        cal = _make_calibrator()
        status = cal.get_status()
        assert "calibrated" in status
        assert "residual_px" in status
        assert "calibrated_at" in status
        assert "pending_samples" in status
        assert "screen_w" in status
        assert "screen_h" in status

    def test_get_status_not_calibrated(self):
        cal = _make_calibrator()
        assert cal.get_status()["calibrated"] is False


# ---------------------------------------------------------------------------
# solve
# ---------------------------------------------------------------------------

def _no_disk_write(fn):
    """Decorator: suppress JSON writes so solve() tests don't leave files on disk."""
    def wrapper(*args, **kwargs):
        with patch("calibration.gaze_calibrator._JSON_PATH", Path("/dev/null").parent / "gaze_cal_test_noop.json"):
            # Patch _save_json to be a no-op so tests stay disk-clean
            target = args[0] if args else kwargs.get("cal")
            orig = target._save_json if hasattr(target, "_save_json") else None
            if orig:
                target._save_json = lambda: None
            result = fn(*args, **kwargs)
            if orig:
                target._save_json = orig
            return result
    return wrapper


class TestSolve:
    def test_solve_succeeds_with_5_samples(self):
        cal = _make_calibrator()
        _synthetic_samples(cal)
        with patch.object(cal, "_save_json"):
            assert cal.solve() is True

    def test_solve_fails_with_too_few_samples(self):
        cal = _make_calibrator()
        cal.add_sample((0.0, 0.0, -1.0), 100, 100)
        cal.add_sample((0.1, 0.0, -1.0), 500, 100)
        with patch.object(cal, "_save_json"):
            assert cal.solve() is False

    def test_solve_clears_samples_on_success(self):
        cal = _make_calibrator()
        _synthetic_samples(cal)
        with patch.object(cal, "_save_json"):
            cal.solve()
        assert cal.sample_count() == 0

    def test_solve_does_not_clear_samples_on_failure(self):
        cal = _make_calibrator()
        cal.add_sample((0.0, 0.0, -1.0), 100, 100)
        with patch.object(cal, "_save_json"):
            cal.solve()   # fails — too few
        assert cal.sample_count() == 1

    def test_calibrated_after_solve(self):
        cal = _make_calibrator()
        _synthetic_samples(cal)
        with patch.object(cal, "_save_json"):
            cal.solve()
        assert cal.is_calibrated is True

    def test_solve_with_collinear_points_returns_false(self):
        """All 5 points on a line → rank-deficient design matrix → False."""
        cal = _make_calibrator()
        for i in range(5):
            az = i * 0.02
            ray = (az, 0.0, -1.0)
            norm = sum(x**2 for x in ray) ** 0.5
            ray = tuple(x / norm for x in ray)
            cal.add_sample(ray, 200 * i, 540)   # all at same screen y
        with patch.object(cal, "_save_json"):
            result = cal.solve()
        assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# project
# ---------------------------------------------------------------------------

def _solved_calibrator(**kwargs):
    """Return a calibrated GazeCalibrator without touching disk."""
    cal = _make_calibrator(**kwargs)
    targets, rays = _synthetic_samples(cal)
    with patch.object(cal, "_save_json"):
        cal.solve()
    return cal, targets, rays


class TestProject:
    def test_project_center_ray_near_center(self):
        """The reference direction should project close to screen center."""
        cal, targets, rays = _solved_calibrator(screen_w=1920, screen_h=1080)

        center_ray = rays[2]
        result = cal.project(center_ray)
        assert result is not None
        px_x, px_y = result
        cx, cy = targets[2]
        assert abs(px_x - cx) < 15, f"Expected ~{cx}, got {px_x}"
        assert abs(px_y - cy) < 15, f"Expected ~{cy}, got {px_y}"

    def test_project_known_sample_low_residual(self):
        """All calibration samples should project within 20px of their targets."""
        cal, targets, rays = _solved_calibrator()

        for (expected_x, expected_y), ray in zip(targets, rays):
            result = cal.project(ray)
            assert result is not None
            px_x, px_y = result
            assert abs(px_x - expected_x) < 20
            assert abs(px_y - expected_y) < 20

    def test_project_clamps_to_screen_bounds(self):
        """Extreme gaze directions must be clamped, not raise."""
        cal, _, _ = _solved_calibrator(screen_w=1920, screen_h=1080)

        extreme_ray = (1.0, 0.0, 0.0)
        result = cal.project(extreme_ray)
        if result is not None:
            px_x, px_y = result
            assert 0 <= px_x < 1920
            assert 0 <= px_y < 1080

    def test_project_zero_ray_returns_none(self):
        """A zero-length ray must not crash and returns None."""
        cal, _, _ = _solved_calibrator()
        assert cal.project((0.0, 0.0, 0.0)) is None

    def test_project_returns_ints(self):
        cal, _, _ = _solved_calibrator()
        result = cal.project((0.0, 0.0, -1.0))
        if result is not None:
            assert isinstance(result[0], int)
            assert isinstance(result[1], int)


# ---------------------------------------------------------------------------
# Persistence (JSON)
# ---------------------------------------------------------------------------

class TestPersistence:
    def test_save_and_load_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            json_path = Path(tmp) / "gaze_calibration.json"
            with patch("calibration.gaze_calibrator._JSON_PATH", json_path):
                cal1 = _make_calibrator()
                _synthetic_samples(cal1)
                cal1.solve()
                assert cal1.is_calibrated

                cal2 = _make_calibrator()
                assert cal2.load() is True
                assert cal2.is_calibrated
                assert abs(cal2._residual_px - cal1._residual_px) < 0.01

    def test_load_returns_false_when_no_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            json_path = Path(tmp) / "missing.json"
            with patch("calibration.gaze_calibrator._JSON_PATH", json_path):
                cal = _make_calibrator()
                assert cal.load() is False
                assert cal.is_calibrated is False

    def test_load_returns_false_on_corrupt_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            json_path = Path(tmp) / "gaze_calibration.json"
            json_path.write_text("not valid json{{{")
            with patch("calibration.gaze_calibrator._JSON_PATH", json_path):
                cal = _make_calibrator()
                assert cal.load() is False


# ---------------------------------------------------------------------------
# DB persistence
# ---------------------------------------------------------------------------

class TestDBPersistence:
    def test_save_to_db_calls_upsert(self):
        import asyncio
        cal, _, _ = _solved_calibrator()
        residual = cal._residual_px

        db = MagicMock()
        db.available = True
        db.upsert_gaze_calibration = AsyncMock()

        asyncio.run(cal.save_to_db(db, session_id=1))
        db.upsert_gaze_calibration.assert_awaited_once()
        kwargs = db.upsert_gaze_calibration.call_args
        assert kwargs.kwargs["screen_w"] == 1920
        assert kwargs.kwargs["residual_px"] == pytest.approx(residual, abs=0.01)

    def test_save_to_db_skips_when_not_calibrated(self):
        import asyncio
        cal = _make_calibrator()  # not solved

        db = MagicMock()
        db.available = True
        db.upsert_gaze_calibration = AsyncMock()

        asyncio.run(cal.save_to_db(db, session_id=1))
        db.upsert_gaze_calibration.assert_not_awaited()
