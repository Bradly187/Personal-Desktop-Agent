from __future__ import annotations
import json
import logging
from storage.repositories.common import _GOAL_LEASE_TTL_S, _pid_alive
import math
import os
import time
import hashlib
from typing import Optional, TYPE_CHECKING

from storage.embeddings import _get_encoder, _encode_sync, _cosine, _tokens, _jaccard, _recency_weight, _fse_score

if TYPE_CHECKING:
    from core.command_executor import Command

log = logging.getLogger(__name__)

class TelemetryRepo:
    def __init__(self, conn):
        self._conn = conn

    async def insert_sensor_telemetry(
        self,
        session_id: int,
        ts: float,
        *,
        tilt_rx: Optional[float] = None,
        tilt_ry: Optional[float] = None,
        gaze_dx: Optional[float] = None,
        gaze_dy: Optional[float] = None,
        gaze_conf: Optional[float] = None,
        head_pitch: Optional[float] = None,
        head_yaw: Optional[float] = None,
        cursor_x: Optional[int] = None,
        cursor_y: Optional[int] = None,
        pain_day_active: bool = False,
        active_source: Optional[str] = None,
        gesture_conf: Optional[float] = None,
        rms_ambient: Optional[float] = None,
        trace_id: Optional[str] = None,
    ) -> None:
        """Write one 1-Hz sensor telemetry row. Non-fatal on any error."""
        if not self._conn:
            return
        try:
            await self._conn.execute(
                """INSERT INTO sensor_telemetry
                   (session_id, ts, tilt_rx, tilt_ry, gaze_dx, gaze_dy, gaze_conf,
                    head_pitch, head_yaw, cursor_x, cursor_y,
                    pain_day_active, active_source, gesture_conf, rms_ambient,
                    trace_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    session_id, ts,
                    tilt_rx, tilt_ry,
                    gaze_dx, gaze_dy, gaze_conf,
                    head_pitch, head_yaw,
                    cursor_x, cursor_y,
                    int(pain_day_active),
                    active_source,
                    gesture_conf, rms_ambient,
                    trace_id,
                ),
            )
            await self._conn.commit()
        except Exception as exc:
            log.debug("insert_sensor_telemetry failed (non-fatal): %s", exc)

    async def prune_sensor_telemetry(self, days: int = 7) -> int:
        """Delete sensor_telemetry rows older than `days`. Returns rows deleted.

        At 1 Hz write rate, 7 days = ~604,800 rows (~30–50 MB). Call at startup
        to keep the DB from growing unboundedly across long-uptime deployments.
        """
        cutoff = time.time() - days * 86400
        return await self._prune_with_retry(
            "DELETE FROM sensor_telemetry WHERE ts < ?", (cutoff,),
            label=f"sensor_telemetry rows (> {days} days)", checkpoint=True,
        )

    async def get_sensor_rom(self, sensor: str) -> dict[str, dict]:
        """Return the most recent range-of-motion row per direction for a sensor.

        Returns dict keyed by direction, each value is
        {max_value, comfortable_value, unit}.
        """
        if not self._conn:
            return {}
        try:
            async with self._conn.execute(
                """SELECT direction, max_value, comfortable_value, unit
                   FROM sensor_rom
                   WHERE sensor = ?
                   GROUP BY direction
                   HAVING ts = MAX(ts)""",
                (sensor,),
            ) as cur:
                rows = await cur.fetchall()
            return {
                r["direction"]: {
                    "max_value": r["max_value"],
                    "comfortable_value": r["comfortable_value"],
                    "unit": r["unit"],
                }
                for r in rows
                if r["direction"] is not None
            }
        except Exception as exc:
            log.warning("AgentDB.get_sensor_rom failed: %s", exc)
            return {}

