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

class ProfileRepo:
    def __init__(self, conn):
        self._conn = conn

    async def get_flare_profile(self) -> dict | None:
        if not self._conn:
            return None
        try:
            row = await (await self._conn.execute(
                """SELECT voice_degrades, gesture_degrades, gaze_degrades,
                          tilt_degrades, flare_vad_scale, manual_pain_day, notes,
                          sound_degrades
                   FROM flare_profile ORDER BY id DESC LIMIT 1"""
            )).fetchone()
            if not row:
                return None
            return {
                "voice_degrades": bool(row[0]), "gesture_degrades": bool(row[1]),
                "gaze_degrades": bool(row[2]), "tilt_degrades": bool(row[3]),
                "flare_vad_scale": row[4], "manual_pain_day": bool(row[5]),
                "notes": row[6], "sound_degrades": bool(row[7]),
            }
        except Exception as exc:
            log.warning("AgentDB.get_flare_profile failed: %s", exc)
            return None

    async def upsert_flare_profile(self, flags: dict) -> None:
        """Persist the user's flare degrade profile from the iPad FlareProfileSheet.

        Updates the most recent flare_profile row (preserving manual_pain_day),
        or inserts a new one. `flags` may contain any of: voice_degrades,
        gesture_degrades, gaze_degrades, tilt_degrades, sound_degrades,
        flare_vad_scale.
        """
        if not self._conn:
            return
        cols = ("voice_degrades", "gesture_degrades", "gaze_degrades",
                "tilt_degrades", "sound_degrades", "flare_vad_scale")
        try:
            existing = await (await self._conn.execute(
                "SELECT id FROM flare_profile ORDER BY id DESC LIMIT 1"
            )).fetchone()
            present = [(c, flags[c]) for c in cols if c in flags]
            if not present:
                return
            if existing:
                set_clause = ", ".join(f"{c}=?" for c, _ in present)
                params = [
                    int(v) if c != "flare_vad_scale" else float(v)
                    for c, v in present
                ]
                await self._conn.execute(
                    f"UPDATE flare_profile SET {set_clause}, updated_at=? WHERE id=?",
                    (*params, time.time(), existing[0]),
                )
            else:
                col_names = ", ".join(c for c, _ in present)
                placeholders = ", ".join("?" for _ in present)
                params = [
                    int(v) if c != "flare_vad_scale" else float(v)
                    for c, v in present
                ]
                await self._conn.execute(
                    f"INSERT INTO flare_profile (updated_at, {col_names}) "
                    f"VALUES (?, {placeholders})",
                    (time.time(), *params),
                )
            await self._conn.commit()
            log.info("AgentDB: flare_profile updated — %s", dict(present))
        except Exception as exc:
            log.warning("AgentDB.upsert_flare_profile failed: %s", exc)

    async def set_manual_pain_day(self, active: bool) -> None:
        """User override: force pain_day_active regardless of auto-detection."""
        if not self._conn:
            return
        try:
            existing = await (await self._conn.execute(
                "SELECT id FROM flare_profile ORDER BY id DESC LIMIT 1"
            )).fetchone()
            if existing:
                await self._conn.execute(
                    "UPDATE flare_profile SET manual_pain_day=?, updated_at=? WHERE id=?",
                    (int(active), time.time(), existing[0]),
                )
            else:
                await self._conn.execute(
                    """INSERT INTO flare_profile
                       (updated_at, manual_pain_day, flare_vad_scale)
                       VALUES (?,?,0.5)""",
                    (time.time(), int(active)),
                )
            await self._conn.commit()
            log.info("AgentDB: manual_pain_day set to %s", active)
        except Exception as exc:
            log.warning("AgentDB.set_manual_pain_day failed: %s", exc)

    async def log_pain_day(
        self,
        session_id: int,
        score: float,
        active: bool,
        fail_ratio: float,
        clarify_ratio: float,
        gesture_conf_delta: float,
        cmd_rate_delta: float,
    ) -> None:
        """Append a pain_day_log row."""
        if not self._conn:
            return
        try:
            await self._conn.execute(
                """INSERT INTO twin_pain_day_log
                   (session_id, ts, pain_day_score, pain_day_active,
                    fail_ratio, clarify_ratio, gesture_conf_delta, cmd_rate_delta)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id, time.time(), score, 1 if active else 0,
                    fail_ratio, clarify_ratio, gesture_conf_delta, cmd_rate_delta,
                ),
            )
            await self._conn.commit()
        except Exception as exc:
            log.warning("AgentDB.log_pain_day failed: %s", exc)

    async def get_preference_model_snapshot(self) -> Optional[str]:
        """Return the most recent preference_model JSON from settings_versions."""
        if not self._conn:
            return None
        try:
            async with self._conn.execute(
                """SELECT new_value FROM settings_versions
                   WHERE component = 'preference_model' AND key = 'snapshot'
                   ORDER BY ts DESC LIMIT 1""",
            ) as cur:
                row = await cur.fetchone()
                return row["new_value"] if row else None
        except Exception as exc:
            log.warning("AgentDB.get_preference_model_snapshot failed: %s", exc)
            return None

    async def log_settings_change(
        self,
        component: str,
        key: str,
        old_value,
        new_value,
        changed_by: str = "user",
    ) -> None:
        if not self._conn:
            return
        try:
            await self._conn.execute(
                """INSERT INTO settings_versions
                   (ts, component, key, old_value, new_value, changed_by)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    time.time(), component, key,
                    # Avoid double-serialization: if the value is already a JSON
                    # string (e.g. from PreferenceModel.to_json()), store as-is.
                    json.dumps(old_value) if (old_value is not None and not isinstance(old_value, str)) else old_value,
                    new_value if isinstance(new_value, str) else json.dumps(new_value),
                    changed_by,
                ),
            )
            await self._conn.commit()
        except Exception as exc:
            log.warning("AgentDB.log_settings_change failed: %s", exc)

