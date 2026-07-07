from __future__ import annotations
import logging
import time
from typing import Optional, TYPE_CHECKING


if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)

class GesturesRepo:
    def __init__(self, conn):
        self._conn = conn

    async def record_gesture_sample(
        self,
        gesture: str,
        confidence: float,
        lidar_depth_m: Optional[float] = None,
        command_id: Optional[int] = None,
    ) -> None:
        if not self._conn:
            return
        try:
            await self._conn.execute(
                """INSERT INTO gesture_samples
                   (command_id, ts, gesture, confidence, lidar_depth_m)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    command_id if (command_id and command_id > 0) else None,
                    time.time(), gesture, confidence, lidar_depth_m,
                ),
            )
            await self._conn.commit()
        except Exception as exc:
            log.warning("AgentDB.record_gesture_sample failed: %s", exc)

    async def get_recent_gesture_samples(
        self, gesture: str, limit: int = 500
    ) -> list[float]:
        if not self._conn:
            return []
        try:
            async with self._conn.execute(
                """SELECT confidence FROM gesture_samples
                   WHERE gesture = ?
                   ORDER BY ts DESC LIMIT ?""",
                (gesture, limit),
            ) as cur:
                return [r["confidence"] for r in await cur.fetchall()]
        except Exception as exc:
            log.warning("AgentDB.get_recent_gesture_samples failed: %s", exc)
            return []

    async def update_gesture_calibration(
        self,
        gesture: str,
        confidence_floor: float,
        sample_count: int,
        p10: float,
    ) -> None:
        if not self._conn:
            return
        try:
            await self._conn.execute(
                """INSERT INTO gesture_calibration
                   (ts, gesture, confidence_floor, sample_count, p10)
                   VALUES (?, ?, ?, ?, ?)""",
                (time.time(), gesture, confidence_floor, sample_count, p10),
            )
            await self._conn.commit()
        except Exception as exc:
            log.warning("AgentDB.update_gesture_calibration failed: %s", exc)

    async def get_gesture_floor(self, gesture: str) -> float:
        if not self._conn:
            return 0.60
        try:
            async with self._conn.execute(
                """SELECT confidence_floor FROM gesture_calibration
                   WHERE gesture = ?
                   ORDER BY ts DESC LIMIT 1""",
                (gesture,),
            ) as cur:
                row = await cur.fetchone()
                return row["confidence_floor"] if row else 0.60
        except Exception as exc:
            log.warning("AgentDB.get_gesture_floor failed: %s", exc)
            return 0.60

    async def record_gesture_velocity(
        self, gesture: str, velocity: float, pain_day: bool = False
    ) -> None:
        if not self._conn:
            return
        try:
            await self._conn.execute(
                "INSERT INTO gesture_velocity_samples (ts, gesture, velocity, pain_day)"
                " VALUES (?, ?, ?, ?)",
                (time.time(), gesture, velocity, int(pain_day)),
            )
            await self._conn.commit()
        except Exception as exc:
            log.warning("AgentDB.record_gesture_velocity failed: %s", exc)

    async def get_recent_gesture_velocities(
        self, gesture: str, limit: int = 500
    ) -> list[float]:
        if not self._conn:
            return []
        try:
            async with self._conn.execute(
                "SELECT velocity FROM gesture_velocity_samples"
                " WHERE gesture = ? ORDER BY ts DESC LIMIT ?",
                (gesture, limit),
            ) as cur:
                return [r["velocity"] for r in await cur.fetchall()]
        except Exception as exc:
            log.warning("AgentDB.get_recent_gesture_velocities failed: %s", exc)
            return []

    async def update_gesture_velocity_calibration(
        self, gesture: str, velocity_floor: float, sample_count: int, p10: float
    ) -> None:
        if not self._conn:
            return
        try:
            await self._conn.execute(
                "INSERT INTO gesture_velocity_calibration"
                " (ts, gesture, velocity_floor, sample_count, p10)"
                " VALUES (?, ?, ?, ?, ?)",
                (time.time(), gesture, velocity_floor, sample_count, p10),
            )
            await self._conn.commit()
        except Exception as exc:
            log.warning("AgentDB.update_gesture_velocity_calibration failed: %s", exc)

    async def get_gesture_velocity_floor(
        self, gesture: str, default: Optional[float] = None
    ) -> Optional[float]:
        """Latest calibrated velocity floor for `gesture`, or `default` when no
        calibration row exists. The default is None — NOT a numeric guess:
        swipe floors are ~1.2 normalized coords/s but push/pull floors are
        ~0.30 m/s (different units), so a shared numeric default poisons the
        other gesture class (audit 2026-06-09: the old 1.2 default made
        push/pull/snap gestures 4x harder on every uncalibrated startup,
        permanently, because below-threshold motions are never sampled)."""
        if not self._conn:
            return default
        try:
            async with self._conn.execute(
                "SELECT velocity_floor FROM gesture_velocity_calibration"
                " WHERE gesture = ? ORDER BY ts DESC LIMIT 1",
                (gesture,),
            ) as cur:
                row = await cur.fetchone()
                return row["velocity_floor"] if row else default
        except Exception as exc:
            log.warning("AgentDB.get_gesture_velocity_floor failed: %s", exc)
            return default

