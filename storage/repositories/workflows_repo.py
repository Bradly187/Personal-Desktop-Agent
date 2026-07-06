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

class WorkflowsRepo:
    def __init__(self, conn):
        self._conn = conn

    async def insert_workflow(
        self,
        name: str,
        goal: Optional[str],
        mode: str,
        subtask_count: int,
        success_count: int,
        status: str,
        verified_count: Optional[int] = None,
        latency_ms: Optional[float] = None,
        error: Optional[str] = None,
    ) -> int:
        """Record one WorkflowRunner.run() in the agent_workflows ledger.

        Best-effort journaling (specs/workflow-orchestration): a DB failure
        never breaks orchestration — returns -1, like the other inserters.
        """
        if not self._conn:
            return -1
        try:
            cur = await self._conn.execute(
                """INSERT INTO agent_workflows
                   (ts, name, goal, mode, subtask_count, success_count,
                    verified_count, status, latency_ms, error)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    time.time(), name, goal, mode, subtask_count, success_count,
                    verified_count, status,
                    round(latency_ms, 1) if latency_ms is not None else None,
                    error,
                ),
            )
            await self._conn.commit()
            return cur.lastrowid  # type: ignore[return-value]
        except Exception as exc:
            log.warning("AgentDB.insert_workflow failed: %s", exc)
            return -1

