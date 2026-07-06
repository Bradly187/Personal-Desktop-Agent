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

class SagasRepo:
    def __init__(self, conn):
        self._conn = conn

    async def insert_saga_compensation(
        self,
        run_id: int,
        step_id: int,
        compensation_action: str,
        compensation_args: Optional[str] = None,
    ) -> Optional[int]:
        """Register a compensation for a completed step. Returns new row id."""
        if not self._conn:
            return None
        try:
            async with self._conn.execute(
                "INSERT INTO saga_compensations"
                " (ts, run_id, step_id, compensation_action, compensation_args)"
                " VALUES (?, ?, ?, ?, ?)",
                (time.time(), run_id, step_id, compensation_action, compensation_args),
            ) as cur:
                row_id = cur.lastrowid
            await self._conn.commit()
            return row_id
        except Exception as exc:
            log.warning("AgentDB.insert_saga_compensation failed: %s", exc)
            return None

    async def update_saga_compensation(
        self,
        compensation_id: int,
        status: str,
        *,
        triggered_by: Optional[str] = None,
        error: Optional[str] = None,
        finished: bool = False,
    ) -> None:
        """Update the status of a saga compensation row."""
        if not self._conn:
            return
        now = time.time()
        try:
            await self._conn.execute(
                "UPDATE saga_compensations"
                " SET status = ?, triggered_by = COALESCE(?, triggered_by),"
                "     started_at = CASE WHEN started_at IS NULL THEN ? ELSE started_at END,"
                "     finished_at = CASE WHEN ? THEN ? ELSE finished_at END,"
                "     error = COALESCE(?, error)"
                " WHERE id = ?",
                (
                    status,
                    triggered_by,
                    now,
                    finished, now if finished else None,
                    error,
                    compensation_id,
                ),
            )
            await self._conn.commit()
        except Exception as exc:
            log.warning("AgentDB.update_saga_compensation failed: %s", exc)

    async def get_pending_compensations(self, run_id: int) -> list[dict]:
        """Return pending compensations for run_id in reverse step order (highest step_num first)."""
        if not self._conn:
            return []
        try:
            async with self._conn.execute(
                "SELECT sc.id, sc.step_id, sc.compensation_action, sc.compensation_args,"
                "       as2.step_num"
                " FROM saga_compensations sc"
                " JOIN agent_steps as2 ON as2.id = sc.step_id"
                " WHERE sc.run_id = ? AND sc.status = 'pending'"
                " ORDER BY as2.step_num DESC",
                (run_id,),
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]
        except Exception as exc:
            log.warning("AgentDB.get_pending_compensations failed: %s", exc)
            return []

    async def get_checkpoint_compensations(self, run_id: int) -> list[dict]:
        """Return 'checkpoint' compensations for run_id (read-only; reverse step
        order). Used by the rewind confirm to show WHAT a rollback would restore
        (specs/chat-workbench-parity R8.3) before anything is promoted."""
        if not self._conn:
            return []
        try:
            async with self._conn.execute(
                "SELECT sc.id, sc.step_id, sc.compensation_action, sc.compensation_args,"
                "       as2.step_num"
                " FROM saga_compensations sc"
                " JOIN agent_steps as2 ON as2.id = sc.step_id"
                " WHERE sc.run_id = ? AND sc.status = 'checkpoint'"
                " ORDER BY as2.step_num DESC",
                (run_id,),
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]
        except Exception as exc:
            log.warning("AgentDB.get_checkpoint_compensations failed: %s", exc)
            return []

    async def skip_pending_compensations(self, run_id: int, new_status: str = 'skipped') -> int:
        """Mark all still-pending compensations for run_id as new_status.

        Called at run finalization so a successful run (or any terminal path
        that didn't roll back) doesn't leave compensation rows 'pending'
        forever. Returns the number of rows updated.
        """
        if not self._conn:
            return 0
        try:
            cur = await self._conn.execute(
                "UPDATE saga_compensations"
                " SET status = ?,"
                "     finished_at = COALESCE(finished_at, ?)"
                " WHERE run_id = ? AND status = 'pending'",
                (new_status, time.time(), run_id),
            )
            await self._conn.commit()
            return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        except Exception as exc:
            log.warning("AgentDB.skip_pending_compensations failed: %s", exc)
            return 0

    async def insert_escalation(
        self,
        run_id: int,
        goal: str,
        reason: str,
        failed_action: Optional[str] = None,
        replans: int = 0,
        detail: Optional[str] = None,
    ) -> Optional[int]:
        """Escalate a halted dev plan for human review. Returns new row id."""
        if not self._conn:
            return None
        try:
            async with self._conn.execute(
                "INSERT INTO dev_escalations"
                " (ts, run_id, goal, reason, failed_action, replans, detail)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (time.time(), run_id, goal, reason, failed_action, replans, detail),
            ) as cur:
                row_id = cur.lastrowid
            await self._conn.commit()
            return row_id
        except Exception as exc:
            log.warning("AgentDB.insert_escalation failed: %s", exc)
            return None

    async def get_pending_escalations(self, limit: int = 10) -> list[dict]:
        """Pending escalations, newest first."""
        if not self._conn:
            return []
        try:
            async with self._conn.execute(
                "SELECT id, ts, run_id, goal, reason, failed_action, replans, detail"
                " FROM dev_escalations WHERE status = 'pending'"
                " ORDER BY ts DESC LIMIT ?",
                (limit,),
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]
        except Exception as exc:
            log.warning("AgentDB.get_pending_escalations failed: %s", exc)
            return []

    async def count_pending_escalations(self) -> int:
        if not self._conn:
            return 0
        try:
            async with self._conn.execute(
                "SELECT COUNT(*) FROM dev_escalations WHERE status = 'pending'"
            ) as cur:
                row = await cur.fetchone()
                return int(row[0]) if row else 0
        except Exception as exc:
            log.warning("AgentDB.count_pending_escalations failed: %s", exc)
            return 0

    async def resolve_escalations(
        self, status: str = "acknowledged", escalation_id: Optional[int] = None
    ) -> int:
        """Resolve one escalation (by id) or every pending one. Returns count."""
        if not self._conn:
            return 0
        try:
            if escalation_id is not None:
                cur = await self._conn.execute(
                    "UPDATE dev_escalations SET status = ?, resolved_ts = ?"
                    " WHERE id = ? AND status = 'pending'",
                    (status, time.time(), escalation_id),
                )
            else:
                cur = await self._conn.execute(
                    "UPDATE dev_escalations SET status = ?, resolved_ts = ?"
                    " WHERE status = 'pending'",
                    (status, time.time()),
                )
            await self._conn.commit()
            return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        except Exception as exc:
            log.warning("AgentDB.resolve_escalations failed: %s", exc)
            return 0

