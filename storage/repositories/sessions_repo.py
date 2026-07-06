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

class SessionsRepo:
    def __init__(self, conn):
        self._conn = conn

    async def insert_session(
        self,
        mode: str = "normal",
        git_hash: Optional[str] = None,
        agent_version: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> int:
        """Insert a new session row and return its id."""
        if not self._conn:
            return -1
        cur = await self._conn.execute(
            "INSERT INTO sessions (started_at, mode, git_hash, agent_version, notes)"
            " VALUES (?, ?, ?, ?, ?)",
            (time.time(), mode, git_hash, agent_version, notes),
        )
        await self._conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    async def close_session(self, session_id: int) -> None:
        if not self._conn or session_id < 0:
            return
        await self._conn.execute(
            "UPDATE sessions SET ended_at = ? WHERE id = ?",
            (time.time(), session_id),
        )
        await self._conn.commit()

    async def insert_session_summary(self, summary: dict) -> None:
        """Upsert a session summary row (keyed on session_id)."""
        if not self._conn:
            return
        try:
            await self._conn.execute(
                """INSERT OR REPLACE INTO session_summaries
                   (session_id, ts, duration_s, total_commands, success_rate,
                    cloud_escalation_rate, gate0_blocks, gate1_blocks,
                    gate2_blocks, gate3_blocks, gate4_blocks,
                    latency_p50_ms, latency_p95_ms, pain_day_pct,
                    corrections_count, avg_whisper_logprob, avg_gesture_conf,
                    source_breakdown, domain_breakdown, top_actions)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    summary.get("session_id"),
                    summary.get("ts", time.time()),
                    summary.get("duration_s"),
                    summary.get("total_commands", 0),
                    summary.get("success_rate"),
                    summary.get("cloud_escalation_rate"),
                    summary.get("gate0_blocks", 0),
                    summary.get("gate1_blocks", 0),
                    summary.get("gate2_blocks", 0),
                    summary.get("gate3_blocks", 0),
                    summary.get("gate4_blocks", 0),
                    summary.get("latency_p50_ms"),
                    summary.get("latency_p95_ms"),
                    summary.get("pain_day_pct"),
                    summary.get("corrections_count", 0),
                    summary.get("avg_whisper_logprob"),
                    summary.get("avg_gesture_conf"),
                    summary.get("source_breakdown"),
                    summary.get("domain_breakdown"),
                    summary.get("top_actions"),
                ),
            )
            await self._conn.commit()
        except Exception as exc:
            log.debug("insert_session_summary failed (non-fatal): %s", exc)

    async def start_calibration_session(self, condition: str, notes: str = "") -> int:
        if not self._conn:
            return -1
        cur = await self._conn.execute(
            "INSERT INTO voice_calibration_sessions (ts, condition, notes) VALUES (?,?,?)",
            (time.time(), condition, notes),
        )
        await self._conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    async def get_session_commands(self, session_id: int, limit: int = 20) -> list[dict]:
        """Return the last N commands from a specific session."""
        if not self._conn:
            return []
        try:
            async with self._conn.execute(
                """SELECT text, action, source, ts, session_id
                   FROM commands
                   WHERE session_id = ?
                   ORDER BY ts DESC LIMIT ?""",
                (session_id, limit),
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]
        except Exception as exc:
            log.warning("AgentDB.get_session_commands failed: %s", exc)
            return []

    async def get_most_recent_session_id(
        self, exclude_session_id: Optional[int] = None
    ) -> Optional[int]:
        """Return the id of the most recent session (optionally excluding current)."""
        if not self._conn:
            return None
        try:
            if exclude_session_id is not None:
                async with self._conn.execute(
                    """SELECT id FROM sessions
                       WHERE id != ?
                       ORDER BY started_at DESC LIMIT 1""",
                    (exclude_session_id,),
                ) as cur:
                    row = await cur.fetchone()
            else:
                async with self._conn.execute(
                    """SELECT id FROM sessions
                       ORDER BY started_at DESC LIMIT 1"""
                ) as cur:
                    row = await cur.fetchone()
            return row["id"] if row else None
        except Exception as exc:
            log.warning("AgentDB.get_most_recent_session_id failed: %s", exc)
            return None

    async def write_session_history(
        self, session_id: int, history: list[dict]
    ) -> None:
        """Persist SessionHistory to twin_session_history table at session close."""
        if not self._conn:
            return
        try:
            await self._conn.execute(
                "DELETE FROM twin_session_history WHERE session_id = ?",
                (session_id,),
            )
            for seq, item in enumerate(history):
                await self._conn.execute(
                    """INSERT INTO twin_session_history
                       (session_id, ts, cmd_text, action, source, seq)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        session_id,
                        item["ts"],
                        item["cmd_text"],
                        item["action"],
                        item["source"],
                        seq,
                    ),
                )
            await self._conn.commit()
        except Exception as exc:
            log.warning("AgentDB.write_session_history failed: %s", exc)

    async def read_session_history(
        self, session_id: int, limit: int = 20
    ) -> list[dict]:
        """Read SessionHistory for a given session."""
        if not self._conn:
            return []
        try:
            async with self._conn.execute(
                """SELECT ts, cmd_text, action, source, seq
                   FROM twin_session_history
                   WHERE session_id = ?
                   ORDER BY seq ASC LIMIT ?""",
                (session_id, limit),
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]
        except Exception as exc:
            log.warning("AgentDB.read_session_history failed: %s", exc)
            return []

