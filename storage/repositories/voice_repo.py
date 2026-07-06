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

class VoiceRepo:
    def __init__(self, conn):
        self._conn = conn

    async def insert_voice_calibration(
        self,
        session_id: int,
        phrase: str,
        actual_text: str,
        rms_amplitude: float,
        freq_centroid: float,
        avg_logprob: float,
        duration_s: float,
        is_flare_day: bool = False,
    ) -> None:
        if not self._conn:
            return
        try:
            await self._conn.execute(
                """INSERT INTO voice_calibration
                   (session_id, ts, phrase, actual_text, rms_amplitude,
                    freq_centroid, avg_logprob, duration_s, is_flare_day)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (session_id, time.time(), phrase, actual_text, rms_amplitude,
                 freq_centroid, avg_logprob, duration_s, int(is_flare_day)),
            )
            await self._conn.commit()
        except Exception as exc:
            log.warning("AgentDB.insert_voice_calibration failed: %s", exc)

    async def upsert_voice_profile(self, profile: dict) -> None:
        """Insert or replace the single-row voice profile."""
        if not self._conn:
            return
        try:
            existing = await (await self._conn.execute(
                "SELECT id FROM voice_profile ORDER BY id LIMIT 1"
            )).fetchone()
            if existing:
                await self._conn.execute(
                    """UPDATE voice_profile SET
                       updated_at=?, baseline_rms=?, baseline_logprob=?,
                       baseline_freq=?, flare_rms_scale=?, vad_threshold=?,
                       logprob_floor=?, sample_count=?
                       WHERE id=?""",
                    (time.time(), profile.get("baseline_rms"),
                     profile.get("baseline_logprob"), profile.get("baseline_freq"),
                     profile.get("flare_rms_scale", 0.5),
                     profile.get("vad_threshold"), profile.get("logprob_floor"),
                     profile.get("sample_count", 0), existing[0]),
                )
            else:
                await self._conn.execute(
                    """INSERT INTO voice_profile
                       (updated_at, baseline_rms, baseline_logprob, baseline_freq,
                        flare_rms_scale, vad_threshold, logprob_floor, sample_count)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (time.time(), profile.get("baseline_rms"),
                     profile.get("baseline_logprob"), profile.get("baseline_freq"),
                     profile.get("flare_rms_scale", 0.5),
                     profile.get("vad_threshold"), profile.get("logprob_floor"),
                     profile.get("sample_count", 0)),
                )
            await self._conn.commit()
        except Exception as exc:
            log.warning("AgentDB.upsert_voice_profile failed: %s", exc)

    async def get_voice_profile(self) -> dict | None:
        """Return the current voice profile row, or None if not yet calibrated."""
        if not self._conn:
            return None
        try:
            row = await (await self._conn.execute(
                """SELECT baseline_rms, baseline_logprob, baseline_freq,
                          flare_rms_scale, vad_threshold, logprob_floor, sample_count
                   FROM voice_profile ORDER BY id LIMIT 1"""
            )).fetchone()
            if not row:
                return None
            return {
                "baseline_rms": row[0], "baseline_logprob": row[1],
                "baseline_freq": row[2], "flare_rms_scale": row[3],
                "vad_threshold": row[4], "logprob_floor": row[5],
                "sample_count": row[6],
            }
        except Exception as exc:
            log.warning("AgentDB.get_voice_profile failed: %s", exc)
            return None

    async def get_voice_calibration_samples(
        self, is_flare_day: bool | None = None, limit: int = 200
    ) -> list[dict]:
        """Return recent voice calibration samples, optionally filtered by flare state."""
        if not self._conn:
            return []
        try:
            if is_flare_day is None:
                rows = await (await self._conn.execute(
                    """SELECT rms_amplitude, freq_centroid, avg_logprob, duration_s, is_flare_day
                       FROM voice_calibration ORDER BY ts DESC LIMIT ?""", (limit,)
                )).fetchall()
            else:
                rows = await (await self._conn.execute(
                    """SELECT rms_amplitude, freq_centroid, avg_logprob, duration_s, is_flare_day
                       FROM voice_calibration WHERE is_flare_day=?
                       ORDER BY ts DESC LIMIT ?""", (int(is_flare_day), limit)
                )).fetchall()
            return [
                {"rms": r[0], "freq": r[1], "logprob": r[2],
                 "duration_s": r[3], "flare": bool(r[4])}
                for r in rows if r[0] is not None
            ]
        except Exception as exc:
            log.warning("AgentDB.get_voice_calibration_samples failed: %s", exc)
            return []

    async def insert_pronunciation(
        self,
        session_id: int,
        expected: str,
        heard: str,
        logprob: float | None = None,
        duration_s: float | None = None,
    ) -> None:
        if not self._conn:
            return
        await self._conn.execute(
            """INSERT INTO voice_pronunciations
               (session_id, ts, expected, heard, logprob, duration_s)
               VALUES (?,?,?,?,?,?)""",
            (session_id, time.time(), expected, heard, logprob, duration_s),
        )
        await self._conn.commit()

    async def save_voice_profile(
        self,
        condition: str,
        corrections: dict,
        vad_threshold: float,
        logprob_floor: float,
        initial_prompt: str | None = None,
    ) -> None:
        if not self._conn:
            return
        await self._conn.execute(
            """INSERT INTO voice_profiles
               (condition, corrections_json, vad_threshold, logprob_floor,
                initial_prompt, updated_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(condition) DO UPDATE SET
                 corrections_json=excluded.corrections_json,
                 vad_threshold=excluded.vad_threshold,
                 logprob_floor=excluded.logprob_floor,
                 initial_prompt=excluded.initial_prompt,
                 updated_at=excluded.updated_at""",
            (condition, json.dumps(corrections), vad_threshold,
             logprob_floor, initial_prompt, time.time()),
        )
        await self._conn.commit()

    async def load_voice_profile(self, condition: str) -> dict | None:
        if not self._conn:
            return None
        cur = await self._conn.execute(
            "SELECT corrections_json, vad_threshold, logprob_floor, initial_prompt "
            "FROM voice_profiles WHERE condition=?",
            (condition,),
        )
        row = await cur.fetchone()
        if not row:
            return None
        return {
            "corrections": json.loads(row[0]),
            "vad_threshold": row[1],
            "logprob_floor": row[2],
            "initial_prompt": row[3],
        }

    async def get_all_pronunciations(self, condition: str) -> list[dict]:
        """Return all accepted pronunciations for a condition across all sessions."""
        if not self._conn:
            return []
        cur = await self._conn.execute(
            """SELECT vp.expected, vp.heard, vp.logprob, vp.duration_s
               FROM voice_pronunciations vp
               JOIN voice_calibration_sessions vcs ON vp.session_id = vcs.id
               WHERE vcs.condition=? AND vp.accepted=1
               ORDER BY vp.ts DESC""",
            (condition,),
        )
        rows = await cur.fetchall()
        return [{"expected": r[0], "heard": r[1], "logprob": r[2], "duration_s": r[3]}
                for r in rows]

    async def insert_ambient_transcript(
        self,
        session_id: int,
        text: str,
        logprob: float | None = None,
        duration_s: float | None = None,
    ) -> None:
        """Store a transcription that was heard but not routed as a command.

        Captures lecture audio, background conversation, etc. so it can be
        searched or reviewed later via DevAgent ("search my lecture notes").
        """
        if not self._conn:
            return
        try:
            await self._conn.execute(
                """INSERT INTO ambient_transcripts (session_id, ts, text, logprob, duration_s)
                   VALUES (?, ?, ?, ?, ?)""",
                (session_id, time.time(), text, logprob, duration_s),
            )
            await self._conn.commit()
        except Exception as exc:
            log.warning("AgentDB.insert_ambient_transcript failed: %s", exc)

