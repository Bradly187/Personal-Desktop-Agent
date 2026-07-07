from __future__ import annotations
import logging
from storage.repositories.common import _GOAL_LEASE_TTL_S
import time
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)

class MiscRepo:
    def __init__(self, conn):
        self._conn = conn



    async def reap_expired_leases(self, lease_ttl_s: float = _GOAL_LEASE_TTL_S) -> int:
        """Runtime recovery (E15): requeue 'running' goals whose claim lease has
        outlived ``lease_ttl_s``.

        Unlike :meth:`requeue_stale_running` (a startup sweep that recovers any
        dead/own/no-owner row), this is safe to call periodically WHILE a drainer
        is executing goals in this same process: it touches ONLY rows whose
        ``claimed_at`` is older than the TTL, so an actively-executing goal — even
        one owned by this pid — with a fresh claim is never clobbered. That
        recovers a goal whose worker wedged in-process (the supervisor restarts
        the loop but the claimed row stays 'running' forever). Returns the count
        requeued/failed.
        """
        if not self._conn:
            return 0
        try:
            cutoff = time.time() - float(lease_ttl_s)
            cur = await self._conn.execute(
                "SELECT id, attempts, max_attempts FROM goal_queue "
                "WHERE status='running' AND claimed_at IS NOT NULL AND claimed_at < ?",
                (cutoff,),
            )
            rows = await cur.fetchall()
            requeued = 0
            for r in rows:
                if int(r["attempts"]) >= int(r["max_attempts"]):
                    await self._conn.execute(
                        "UPDATE goal_queue SET status='failed', "
                        "last_error='claim lease expired (worker wedged)' WHERE id=?",
                        (int(r["id"]),),
                    )
                else:
                    await self._conn.execute(
                        "UPDATE goal_queue SET status='queued', owner_pid=NULL, "
                        "claimed_at=NULL WHERE id=?",
                        (int(r["id"]),),
                    )
                    requeued += 1
            await self._conn.commit()
            if requeued:
                log.warning("AgentDB.reap_expired_leases requeued %d wedged goal(s)",
                            requeued)
            return requeued
        except Exception as exc:
            log.warning("AgentDB.reap_expired_leases failed: %s", exc)
            return 0

    async def promote_checkpoints_to_pending(self, run_id: int) -> int:
        """Mark 'checkpoint' compensations for run_id as 'pending'.
        
        Used by the voice-invokable rewind feature to prepare a run's 
        checkpoints for restoration via _run_compensations.
        """
        if not self._conn:
            return 0
        try:
            cur = await self._conn.execute(
                "UPDATE saga_compensations"
                " SET status = 'pending', finished_at = NULL"
                " WHERE run_id = ? AND status = 'checkpoint'",
                (run_id,),
            )
            await self._conn.commit()
            return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        except Exception as exc:
            log.warning("AgentDB.promote_checkpoints_to_pending failed: %s", exc)
            return 0

    async def get_cache_config(self, tool_name: str) -> tuple[float, int]:
        """Return (ttl_s, max_entries) for tool_name, or hardcoded fallback if not configured."""
        _FALLBACKS = {"vision_grounder": (2.0, 200), "ui_automation": (1.0, 200), "target_cache": (1.5, 500)}
        if not self._conn:
            return _FALLBACKS.get(tool_name, (2.0, 200))
        try:
            async with self._conn.execute(
                "SELECT ttl_s, max_entries FROM tool_cache_config WHERE tool_name = ?",
                (tool_name,),
            ) as cur:
                row = await cur.fetchone()
                return (row["ttl_s"], row["max_entries"]) if row else _FALLBACKS.get(tool_name, (2.0, 200))
        except Exception as exc:
            log.warning("AgentDB.get_cache_config failed: %s", exc)
            return _FALLBACKS.get(tool_name, (2.0, 200))

    async def get_source_stats_last_n_days(self, days: int = 7) -> list[dict]:
        """Return per-source success/total counts for the last N days."""
        if not self._conn:
            return []
        cutoff = time.time() - days * 86400
        try:
            async with self._conn.execute(
                """SELECT source,
                          SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) AS success_count,
                          COUNT(*) AS total_count
                   FROM commands
                   WHERE ts >= ?
                   GROUP BY source""",
                (cutoff,),
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]
        except Exception as exc:
            log.warning("AgentDB.get_source_stats_last_n_days failed: %s", exc)
            return []

