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

class SkillsRepo:
    def __init__(self, conn):
        self._conn = conn

    async def log_skill_invocation(
        self,
        skill_id: str,
        tool_name: str,
        *,
        send: bool = False,
        status: str = "?",
        blocked: bool = False,
        result_summary: str = "",
    ) -> None:
        """Append an audit row for an MCP-client skill call (N+1). No-ops if the
        DB is unavailable."""
        if not self._conn:
            return
        await self._conn.execute(
            "INSERT INTO skill_invocations "
            "(ts, skill_id, tool_name, send, status, blocked, result_summary) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                time.time(), str(skill_id)[:128], str(tool_name)[:128],
                1 if send else 0, str(status)[:32], 1 if blocked else 0,
                str(result_summary or "")[:512],
            ),
        )
        await self._conn.commit()

    async def insert_evolution_candidate(
        self,
        kind: str,
        text: str,
        action_or_wrong: str,
        *,
        domain: str = "command",
        reason: Optional[str] = None,
        source_refs: Optional[str] = None,
    ) -> Optional[int]:
        """Stage one synthesized candidate (status='proposed'). Idempotent on
        UNIQUE(kind, text, action_or_wrong) — a re-run never duplicates."""
        if not self._conn:
            return None
        try:
            cur = await self._conn.execute(
                """INSERT INTO self_evolution_candidates
                   (ts, kind, domain, text, action_or_wrong, reason, source_refs, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'proposed')
                   ON CONFLICT(kind, text, action_or_wrong) DO NOTHING""",
                (time.time(), kind, domain, text, action_or_wrong, reason, source_refs),
            )
            await self._conn.commit()
            if cur.lastrowid:
                return cur.lastrowid
            async with self._conn.execute(
                "SELECT id FROM self_evolution_candidates "
                "WHERE kind = ? AND text = ? AND action_or_wrong = ?",
                (kind, text, action_or_wrong),
            ) as c2:
                row = await c2.fetchone()
                return row[0] if row else None
        except Exception as exc:
            log.warning("AgentDB.insert_evolution_candidate failed: %s", exc)
            return None

    async def get_evolution_candidates(
        self, status: str = "proposed", limit: int = 100,
        kind: Optional[str] = None,
    ) -> list[dict]:
        """Staged candidates in `status`. Pass `kind` (e.g. 'macro',
        'skill_proposal') to filter to one candidate type; None = all kinds
        (back-compat with the example/counterexample callers)."""
        if not self._conn:
            return []
        try:
            sql = (
                "SELECT id, ts, kind, domain, text, action_or_wrong, reason, "
                "source_refs, eval_delta, status, decided_ts "
                "FROM self_evolution_candidates WHERE status = ?"
            )
            params: list = [status]
            if kind is not None:
                sql += " AND kind = ?"
                params.append(kind)
            sql += " ORDER BY ts DESC LIMIT ?"
            params.append(limit)
            async with self._conn.execute(sql, tuple(params)) as cur:
                return [dict(r) for r in await cur.fetchall()]
        except Exception as exc:
            log.warning("AgentDB.get_evolution_candidates failed: %s", exc)
            return []

    async def set_evolution_candidate_status(
        self, candidate_id: int, status: str, eval_delta: Optional[float] = None
    ) -> None:
        if not self._conn:
            return
        try:
            await self._conn.execute(
                "UPDATE self_evolution_candidates SET status = ?, decided_ts = ?, "
                "eval_delta = COALESCE(?, eval_delta) WHERE id = ?",
                (status, time.time(), eval_delta, candidate_id),
            )
            await self._conn.commit()
        except Exception as exc:
            log.warning("AgentDB.set_evolution_candidate_status failed: %s", exc)

    async def get_evolution_candidate(self, candidate_id: int) -> Optional[dict]:
        """One candidate row by id (None if absent)."""
        if not self._conn:
            return None
        try:
            async with self._conn.execute(
                """SELECT id, ts, kind, domain, text, action_or_wrong, reason,
                          source_refs, eval_delta, status, decided_ts
                   FROM self_evolution_candidates WHERE id = ?""",
                (candidate_id,),
            ) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None
        except Exception as exc:
            log.warning("AgentDB.get_evolution_candidate failed: %s", exc)
            return None

    async def promote_macro_candidate(
        self, candidate_id: int, name: str, source_refs: str
    ) -> None:
        """Promote a macro candidate with the user-chosen name persisted.

        Self-skilling rung 2: the human approval ("save that as a command called
        X") supplies the name + keywords, written into `text` and `source_refs`
        so MacroStore.load_promoted reconstructs the user's macro on restart.
        """
        if not self._conn:
            return
        try:
            await self._conn.execute(
                "UPDATE self_evolution_candidates SET status='promoted', "
                "decided_ts = ?, text = ?, source_refs = ? WHERE id = ?",
                (time.time(), name, source_refs, candidate_id),
            )
            await self._conn.commit()
        except Exception as exc:
            log.warning("AgentDB.promote_macro_candidate failed: %s", exc)

    async def get_hotwords(self) -> list[str]:
        if not self._conn:
            return []
        try:
            async with self._conn.execute("SELECT word FROM hotwords") as cur:
                return [r["word"] for r in await cur.fetchall()]
        except Exception as exc:
            log.warning("AgentDB.get_hotwords failed: %s", exc)
            return []

    async def promote_hotwords(self, threshold: int = 3) -> None:
        if not self._conn:
            return
        try:
            async with self._conn.execute(
                "SELECT word FROM word_counts WHERE count >= ?", (threshold,)
            ) as cur:
                candidates = [r["word"] for r in await cur.fetchall()]
            for word in candidates:
                await self._conn.execute(
                    "INSERT OR IGNORE INTO hotwords (word, added) VALUES (?, ?)",
                    (word, time.time()),
                )
            if candidates:
                await self._conn.commit()
        except Exception as exc:
            log.warning("AgentDB.promote_hotwords failed: %s", exc)

