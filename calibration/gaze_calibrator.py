"""calibration.gaze_calibrator.py — Angular gaze-to-monitor mapping.

Maps world-space gaze rays (from ARKit eye tracking) to absolute pixel
coordinates on the primary monitor by fitting an affine model over
5 calibration point correspondences.

Math:
    Each gaze ray is expressed as (azimuth, elevation) offset in radians
    relative to a reference direction (mean of calibration rays).
    The affine mapping:

        px_x = a0 + a1*az + a2*el
        px_y = b0 + b1*az + b2*el

    is solved by numpy least squares from 5+ calibration samples. Numerically
    stable for a flat monitor subtending ~35° of visual angle.

Persistence:
    Solved calibration is stored to gaze_calibration.json (fast cold-start
    load without DB round-trip) and to gaze_monitor_calibration in AgentDB.

Usage (wired by main.py):
    calibrator = GazeCalibrator()
    calibrator.load()                               # load persisted cal if any

    # During calibration session (5 dots):
    calibrator.add_sample(ray_dir, px_x, px_y)
    success = calibrator.solve()
    if success:
        await calibrator.save_to_db(agent_db, session_id)

    # Runtime (called from FusionEngine on gaze_dwell with ray):
    coords = calibrator.project(ray_dir)            # (px_x, px_y) or None
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_JSON_PATH = Path("gaze_calibration.json")
_MIN_SAMPLES = 5
# Hard cap on the in-memory sample buffer. Prevents an unbounded grow attack via
# the LAN WebSocket: an attacker spamming gaze_calibration_sample without ever
# triggering solve() would otherwise inflate process memory indefinitely.
_MAX_SAMPLES = 50


@dataclass
class _CalibSample:
    ray_dir: tuple[float, float, float]   # unit vector (dx, dy, dz)
    px_x: int
    px_y: int
    dot_index: int = -1   # -1 = unindexed (legacy add_sample), 0+ = overlay dot


class GazeCalibrator:
    """Affine angular mapping: gaze ray direction → screen pixel.

    Call from the asyncio event loop; the solve() step is CPU-bound and
    should be wrapped in asyncio.to_thread if called from async context.
    """

    def __init__(self, screen_w: int = 1920, screen_h: int = 1080) -> None:
        self._screen_w = screen_w
        self._screen_h = screen_h
        self._samples: list[_CalibSample] = []

        # Solved state — None until calibrated
        self._ref_dir: Optional[tuple[float, float, float]] = None
        self._matrix: Optional[list[list[float]]] = None   # 2×3 affine
        self._residual_px: float = 0.0
        self._calibrated_at: float = 0.0

    # ── Sample collection ──────────────────────────────────────────────────

    def add_sample(
        self,
        ray_dir: tuple[float, float, float],
        px_x: int,
        px_y: int,
        dot_index: int = -1,
    ) -> None:
        """Add one gaze-ray → pixel correspondence.

        If `dot_index` is non-negative, replaces any prior sample for the same
        dot (a retry semantic — the most recent dwell wins, instead of letting
        multiple captures for one dot bias the least-squares fit). With the
        default `dot_index=-1` samples accumulate as before.

        Buffer is hard-capped at _MAX_SAMPLES to prevent unbounded growth.
        """
        if dot_index >= 0:
            for i, s in enumerate(self._samples):
                if s.dot_index == dot_index:
                    self._samples[i] = _CalibSample(
                        ray_dir=ray_dir, px_x=px_x, px_y=px_y, dot_index=dot_index,
                    )
                    log.debug(
                        "GazeCalibrator: sample dot=%d REPLACED  ray=(%.3f,%.3f,%.3f)  px=(%d,%d)",
                        dot_index, *ray_dir, px_x, px_y,
                    )
                    return

        if len(self._samples) >= _MAX_SAMPLES:
            log.warning(
                "GazeCalibrator.add_sample: buffer at cap (%d); dropping oldest",
                _MAX_SAMPLES,
            )
            self._samples.pop(0)

        self._samples.append(
            _CalibSample(ray_dir=ray_dir, px_x=px_x, px_y=px_y, dot_index=dot_index)
        )
        log.debug(
            "GazeCalibrator: sample %d  dot=%d  ray=(%.3f,%.3f,%.3f)  px=(%d,%d)",
            len(self._samples), dot_index, *ray_dir, px_x, px_y,
        )

    def sample_count(self) -> int:
        return len(self._samples)

    def clear_samples(self) -> None:
        self._samples.clear()

    # ── Solve ──────────────────────────────────────────────────────────────

    def solve(self) -> bool:
        """Fit affine angular mapping from accumulated samples.

        Returns True on success. Clears the sample buffer on success.
        Leaves existing calibration intact on failure so the old calibration
        keeps working while the user retries.
        """
        try:
            import numpy as np
        except ImportError:
            log.error("GazeCalibrator.solve: numpy not available")
            return False

        if len(self._samples) < _MIN_SAMPLES:
            log.warning(
                "GazeCalibrator.solve: need %d samples, have %d",
                _MIN_SAMPLES, len(self._samples),
            )
            return False

        rays = np.array([s.ray_dir for s in self._samples], dtype=np.float64)
        pxs  = np.array([[s.px_x, s.px_y] for s in self._samples], dtype=np.float64)

        # Normalise input rays
        norms = np.linalg.norm(rays, axis=1, keepdims=True)
        if np.any(norms < 1e-9):
            log.warning("GazeCalibrator.solve: degenerate zero-length ray in samples")
            return False
        rays = rays / norms

        # Reference direction: mean of all rays, normalised
        ref = rays.mean(axis=0)
        ref_norm = np.linalg.norm(ref)
        if ref_norm < 1e-9:
            log.warning("GazeCalibrator.solve: degenerate reference direction")
            return False
        ref = ref / ref_norm

        # Project each ray onto (az, el) tangent plane offsets from ref
        az_el = self._rays_to_az_el(rays, ref)   # (N, 2)

        # Check for collinear layout (degenerate calibration pattern)
        design = np.column_stack([np.ones(len(az_el)), az_el])  # (N, 3)
        if np.linalg.matrix_rank(design) < 3:
            log.warning("GazeCalibrator.solve: calibration points appear collinear")
            return False

        # Least-squares fit: design @ A_mat ≈ pxs
        A_mat, _, _, _ = np.linalg.lstsq(design, pxs, rcond=None)  # (3, 2)

        # RMS residual in pixels
        predicted = design @ A_mat
        residual_px = float(np.sqrt(np.mean((predicted - pxs) ** 2)))

        self._ref_dir = (float(ref[0]), float(ref[1]), float(ref[2]))
        # Store as 2×3: row 0 = coefficients for px_x, row 1 for px_y
        self._matrix = [
            [float(A_mat[0, 0]), float(A_mat[1, 0]), float(A_mat[2, 0])],
            [float(A_mat[0, 1]), float(A_mat[1, 1]), float(A_mat[2, 1])],
        ]
        self._residual_px = residual_px
        self._calibrated_at = time.time()

        log.info(
            "GazeCalibrator: calibrated — residual=%.1f px  n=%d",
            residual_px, len(self._samples),
        )
        self._samples.clear()
        self._save_json()
        return True

    # ── Runtime projection ─────────────────────────────────────────────────

    def project(
        self,
        ray_dir: tuple[float, float, float],
    ) -> Optional[tuple[int, int]]:
        """Convert a world-space gaze ray to screen pixel coordinates.

        Args:
            ray_dir: Unit vector (dx, dy, dz) in ARKit device-fixed space.

        Returns:
            (px_x, px_y) clamped to screen bounds, or None if not calibrated.
        """
        if self._ref_dir is None or self._matrix is None:
            return None
        try:
            import numpy as np
        except ImportError:
            return None

        ray = np.array(ray_dir, dtype=np.float64)
        norm = np.linalg.norm(ray)
        if norm < 1e-9:
            return None
        ray = ray / norm

        ref = np.array(self._ref_dir, dtype=np.float64)
        az_el = self._rays_to_az_el(ray[np.newaxis, :], ref)[0]  # (2,)

        A = np.array(self._matrix, dtype=np.float64)          # (2, 3)
        features = np.array([1.0, az_el[0], az_el[1]])         # (3,)
        px = A @ features                                       # (2,)

        px_x = int(round(float(px[0])))
        px_y = int(round(float(px[1])))
        px_x = max(0, min(self._screen_w - 1, px_x))
        px_y = max(0, min(self._screen_h - 1, px_y))
        return (px_x, px_y)

    # ── Persistence ────────────────────────────────────────────────────────

    def load(self) -> bool:
        """Load persisted calibration from gaze_calibration.json.

        Returns True if a valid calibration was loaded.
        """
        if not _JSON_PATH.exists():
            return False
        try:
            data = json.loads(_JSON_PATH.read_text(encoding="utf-8"))
            self._ref_dir       = tuple(data["ref_dir"])       # type: ignore[assignment]
            self._matrix        = data["matrix"]
            self._screen_w      = data.get("screen_w", self._screen_w)
            self._screen_h      = data.get("screen_h", self._screen_h)
            self._residual_px   = float(data.get("residual_px", 0.0))
            self._calibrated_at = float(data.get("calibrated_at", 0.0))
            log.info(
                "GazeCalibrator: loaded from %s  residual=%.1f px",
                _JSON_PATH, self._residual_px,
            )
            return True
        except Exception as exc:
            log.warning("GazeCalibrator: failed to load %s: %s", _JSON_PATH, exc)
            return False

    def _save_json(self) -> None:
        if self._ref_dir is None or self._matrix is None:
            return
        data = {
            "ref_dir":       list(self._ref_dir),
            "matrix":        self._matrix,
            "screen_w":      self._screen_w,
            "screen_h":      self._screen_h,
            "residual_px":   self._residual_px,
            "calibrated_at": self._calibrated_at,
        }
        try:
            _JSON_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
            log.debug("GazeCalibrator: saved to %s", _JSON_PATH)
        except OSError as exc:
            log.warning("GazeCalibrator: could not save %s: %s", _JSON_PATH, exc)

    async def save_to_db(self, agent_db, session_id: int) -> None:
        """Persist solved calibration to AgentDB (async, non-blocking)."""
        if agent_db is None or not agent_db.available:
            return
        if self._ref_dir is None or self._matrix is None:
            return
        await agent_db.upsert_gaze_calibration(
            session_id=session_id,
            ref_dir=list(self._ref_dir),
            matrix=self._matrix,
            screen_w=self._screen_w,
            screen_h=self._screen_h,
            residual_px=self._residual_px,
        )

    # ── Status ─────────────────────────────────────────────────────────────

    @property
    def is_calibrated(self) -> bool:
        return self._ref_dir is not None and self._matrix is not None

    def get_status(self) -> dict:
        return {
            "calibrated":      self.is_calibrated,
            "residual_px":     self._residual_px,
            "calibrated_at":   self._calibrated_at,
            "pending_samples": len(self._samples),
            "screen_w":        self._screen_w,
            "screen_h":        self._screen_h,
        }

    # ── Math ───────────────────────────────────────────────────────────────

    @staticmethod
    def _rays_to_az_el(
        rays: "np.ndarray",   # (N, 3) unit vectors
        ref:  "np.ndarray",   # (3,)  unit reference vector
    ) -> "np.ndarray":        # (N, 2) [azimuth, elevation] in radians
        """Project rays onto the tangent plane of ref → (az, el) offsets.

        Builds a right-handed frame: u = horizontal axis, v = vertical axis.
        For small angles the projections approximate the angular offsets.
        """
        import numpy as np

        world_up = np.array([0.0, 1.0, 0.0])
        if abs(np.dot(ref, world_up)) > 0.99:
            world_up = np.array([0.0, 0.0, 1.0])

        u = np.cross(ref, world_up)
        u /= np.linalg.norm(u)
        v = np.cross(u, ref)
        v /= np.linalg.norm(v)

        az = rays @ u   # (N,) horizontal offsets
        el = rays @ v   # (N,) vertical offsets
        return np.column_stack([az, el])
