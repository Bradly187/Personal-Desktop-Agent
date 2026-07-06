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

class GoalsRepo:
    def __init__(self, conn):
        self._conn = conn

    async def enqueue_goal(
        self,
        goal: str,
        domain: str = "plan",
        idempotency_key: Optional[str] = None,
        max_attempts: int = 3,
    ) -> int:
        """Persist a goal to the durable backlog. Returns its row id, or -1.

        idempotency_key is UNIQUE. While the keyed row is still active
        (queued/scheduled/running) a re-enqueue is a no-op returning the existing
        id — genuine dedup (e.g. crash recovery). But once the row reaches a
        terminal state (done/failed/cancelled), a re-enqueue REVIVES it back to
        'queued' (#1): a recurring/event goal that legitimately wants to run again
        must not be silently swallowed by a stale terminal row.
        """
        if not self._conn:
            return -1
        try:
            await self._conn.execute(
                """INSERT OR IGNORE INTO goal_queue
                   (ts, goal, domain, status, idempotency_key, max_attempts)
                   VALUES (?, ?, ?, 'queued', ?, ?)""",
                (time.time(), goal, domain, idempotency_key, max_attempts),
            )
            await self._conn.commit()
            if idempotency_key is not None:
                cur = await self._conn.execute(
                    "SELECT id, status FROM goal_queue WHERE idempotency_key=?",
                    (idempotency_key,),
                )
                row = await cur.fetchone()
                if row is None:
                    return -1
                gid = int(row["id"])
                if str(row["status"]) in ("done", "failed", "cancelled"):
                    # INSERT was ignored (key still present) → revive the row.
                    await self._conn.execute(
                        "UPDATE goal_queue SET status='queued', goal=?, domain=?, "
                        "attempts=0, last_error=NULL, owner_pid=NULL, "
                        "claimed_at=NULL, ts=? WHERE id=?",
                        (goal, domain, time.time(), gid),
                    )
                    await self._conn.commit()
                return gid
            cur = await self._conn.execute("SELECT last_insert_rowid() AS id")
            row = await cur.fetchone()
            return int(row["id"]) if row else -1
        except Exception as exc:
            log.warning("AgentDB.enqueue_goal failed: %s", exc)
            return -1

    async def claim_next_goal(self) -> Optional[dict]:
        """Atomically claim the oldest queued goal → 'running'. Returns its dict
        (with attempts incremented), or None if the queue is empty.

        Single-consumer (the drainer), so SELECT-then-guarded-UPDATE is race-safe
        without RETURNING; the UPDATE's `status='queued'` guard is belt-and-braces.
        """
        if not self._conn:
            return None
        try:
            cur = await self._conn.execute(
                "SELECT * FROM goal_queue WHERE status='queued' ORDER BY ts LIMIT 1"
            )
            row = await cur.fetchone()
            if row is None:
                return None
            gid = int(row["id"])
            now = time.time()
            self_pid = os.getpid()
            upd = await self._conn.execute(
                "UPDATE goal_queue SET status='running', attempts=attempts+1, "
                "owner_pid=?, claimed_at=? WHERE id=? AND status='queued'",
                (self_pid, now, gid),
            )
            await self._conn.commit()
            if (upd.rowcount or 0) == 0:
                return None   # lost a race (shouldn't happen single-consumer)
            d = dict(row)
            d["status"] = "running"
            d["attempts"] = int(d.get("attempts", 0)) + 1
            d["owner_pid"] = self_pid
            d["claimed_at"] = now
            return d
        except Exception as exc:
            log.warning("AgentDB.claim_next_goal failed: %s", exc)
            return None

    async def complete_goal(
        self,
        goal_id: int,
        status: str,
        error: Optional[str] = None,
        run_id: Optional[int] = None,
    ) -> None:
        """Mark a claimed goal terminal: 'done' / 'failed' / 'cancelled'."""
        if not self._conn:
            return
        try:
            await self._conn.execute(
                "UPDATE goal_queue SET status=?, last_error=?, run_id=COALESCE(?, run_id) "
                "WHERE id=?",
                (status, error, run_id, goal_id),
            )
            await self._conn.commit()
        except Exception as exc:
            log.warning("AgentDB.complete_goal failed: %s", exc)

    async def enqueue_scheduled_goal(
        self,
        goal: str,
        *,
        execute_at: float,
        recurrence: Optional[str] = None,
        domain: str = "plan",
        source_trigger: str = "schedule",
        idempotency_key: Optional[str] = None,
    ) -> int:
        """Persist a future-dated goal with status='scheduled'. The
        ProactiveScheduler promotes it to 'queued' when execute_at <= now.
        Returns its row id, or -1."""
        if not self._conn:
            return -1
        try:
            await self._conn.execute(
                """INSERT OR IGNORE INTO goal_queue
                   (ts, goal, domain, status, idempotency_key, execute_at,
                    recurrence, source_trigger)
                   VALUES (?, ?, ?, 'scheduled', ?, ?, ?, ?)""",
                (time.time(), goal, domain, idempotency_key, execute_at,
                 recurrence, source_trigger),
            )
            await self._conn.commit()
            if idempotency_key is not None:
                cur = await self._conn.execute(
                    "SELECT id FROM goal_queue WHERE idempotency_key=?",
                    (idempotency_key,),
                )
                row = await cur.fetchone()
                return int(row["id"]) if row else -1
            cur = await self._conn.execute("SELECT last_insert_rowid() AS id")
            row = await cur.fetchone()
            return int(row["id"]) if row else -1
        except Exception as exc:
            log.warning("AgentDB.enqueue_scheduled_goal failed: %s", exc)
            return -1

    async def promote_due_goals(self, now: float) -> list[dict]:
        """Promote every scheduled goal whose execute_at has passed to 'queued'
        and return the promoted rows (so the scheduler can re-lay recurrences and
        kick the drainer). Single-consumer (the ProactiveScheduler)."""
        if not self._conn:
            return []
        try:
            cur = await self._conn.execute(
                "SELECT * FROM goal_queue WHERE status='scheduled' "
                "AND execute_at IS NOT NULL AND execute_at <= ? ORDER BY execute_at",
                (now,),
            )
            rows = [dict(r) for r in await cur.fetchall()]
            if not rows:
                return []
            ids = [r["id"] for r in rows]
            qmarks = ",".join("?" for _ in ids)
            await self._conn.execute(
                f"UPDATE goal_queue SET status='queued' WHERE id IN ({qmarks})",
                ids,
            )
            await self._conn.commit()
            return rows
        except Exception as exc:
            log.warning("AgentDB.promote_due_goals failed: %s", exc)
            return []

    async def list_schedules(self) -> list[dict]:
        """All pending scheduled goals (status='scheduled')."""
        if not self._conn:
            return []
        try:
            cur = await self._conn.execute(
                "SELECT id, goal, execute_at, recurrence, source_trigger "
                "FROM goal_queue WHERE status='scheduled' ORDER BY execute_at"
            )
            return [dict(r) for r in await cur.fetchall()]
        except Exception as exc:
            log.warning("AgentDB.list_schedules failed: %s", exc)
            return []

    async def cancel_schedule(self, goal_id: int) -> bool:
        """Cancel a scheduled goal. Returns True if one was cancelled."""
        if not self._conn:
            return False
        try:
            cur = await self._conn.execute(
                "UPDATE goal_queue SET status='cancelled' "
                "WHERE id=? AND status='scheduled'",
                (goal_id,),
            )
            await self._conn.commit()
            return (cur.rowcount or 0) > 0
        except Exception as exc:
            log.warning("AgentDB.cancel_schedule failed: %s", exc)
            return False

    async def get_queued_goals(self, limit: int = 50) -> list[dict]:
        """Return queued goals (oldest first) for status queries / draining."""
        if not self._conn:
            return []
        try:
            cur = await self._conn.execute(
                "SELECT id, goal, domain, attempts, max_attempts, ts "
                "FROM goal_queue WHERE status='queued' ORDER BY ts LIMIT ?",
                (limit,),
            )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]
        except Exception as exc:
            log.warning("AgentDB.get_queued_goals failed: %s", exc)
            return []

