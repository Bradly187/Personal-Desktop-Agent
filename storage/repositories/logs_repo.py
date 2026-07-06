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

class LogsRepo:
    def __init__(self, conn):
        self._conn = conn

    async def insert_drift(
        self,
        session_id: Optional[int],
        trace_id: Optional[str],
        drift_score: float,
        original_intent: str,
        current_command: str,
    ) -> Optional[int]:
        """Record an intent-drift event (GAP-6). Returns row id or None on error."""
        if not self._conn:
            return None
        try:
            async with self._conn.execute(
                "INSERT INTO intent_drift_log"
                " (ts, session_id, trace_id, drift_score, original_intent, current_command)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (time.time(), session_id, trace_id, float(drift_score),
                 original_intent, current_command),
            ) as cur:
                row_id = cur.lastrowid
            await self._conn.commit()
            return row_id
        except Exception as exc:
            log.warning("AgentDB.insert_drift failed: %s", exc)
            return None

    async def insert_correction(
        self,
        session_id: Optional[int],
        trace_id: Optional[str],
        correction_text: str,
        prior_action: Optional[str],
        domain: Optional[str],
    ) -> Optional[int]:
        """Harvest a confirmed user correction (GAP-9). Returns row id or None."""
        if not self._conn or not correction_text:
            return None
        try:
            async with self._conn.execute(
                "INSERT INTO user_corrections"
                " (ts, session_id, trace_id, correction_text, prior_action, domain)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (time.time(), session_id, trace_id, correction_text,
                 prior_action, domain),
            ) as cur:
                row_id = cur.lastrowid
            await self._conn.commit()
            return row_id
        except Exception as exc:
            log.warning("AgentDB.insert_correction failed: %s", exc)
            return None

    async def get_corrections(self, limit: int = 1000) -> list[dict]:
        """Return harvested corrections newest-first (GAP-9 offline clustering)."""
        if not self._conn:
            return []
        try:
            async with self._conn.execute(
                "SELECT id, ts, session_id, trace_id, correction_text, prior_action, domain"
                " FROM user_corrections ORDER BY ts DESC LIMIT ?",
                (limit,),
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]
        except Exception as exc:
            log.warning("AgentDB.get_corrections failed: %s", exc)
            return []

    async def insert_trace_spans(
        self,
        trace_id: str,
        session_id: Optional[int],
        spans: list[dict],
    ) -> int:
        """Persist a completed trace's spans (GAP-4). Returns the count written.

        Called fire-and-forget at command end (off the hot path). Each trace is
        persisted once by its owner, so this plain append never dedups.
        """
        if not self._conn or not spans:
            return 0
        try:
            rows = []
            for i, span in enumerate(spans):
                attrs = span.get("attrs")
                rows.append((
                    trace_id,
                    session_id,
                    i,
                    span.get("stage", ""),
                    float(span.get("ts", 0.0) or 0.0),
                    span.get("dur_ms"),
                    json.dumps(attrs) if attrs else None,
                ))
            await self._conn.executemany(
                "INSERT INTO command_traces"
                " (trace_id, session_id, seq, stage, ts, dur_ms, attrs_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            await self._conn.commit()
            return len(rows)
        except Exception as exc:
            log.warning("AgentDB.insert_trace_spans failed: %s", exc)
            return 0

    async def get_trace_spans(self, trace_id: str) -> list[dict]:
        """Return persisted spans for a trace, ordered (GAP-4 eval replay)."""
        if not self._conn:
            return []
        try:
            async with self._conn.execute(
                "SELECT seq, stage, ts, dur_ms, attrs_json FROM command_traces"
                " WHERE trace_id = ? ORDER BY seq",
                (trace_id,),
            ) as cur:
                out: list[dict] = []
                for r in await cur.fetchall():
                    d = dict(r)
                    raw = d.pop("attrs_json", None)
                    if raw:
                        try:
                            d["attrs"] = json.loads(raw)
                        except Exception:
                            d["attrs"] = {}
                    out.append(d)
                return out
        except Exception as exc:
            log.warning("AgentDB.get_trace_spans failed: %s", exc)
            return []

    async def insert_tool_call(
        self,
        tool_name: str,
        *,
        command_id: Optional[int] = None,
        run_id: Optional[int] = None,
        step_id: Optional[int] = None,
        idempotency_key: Optional[str] = None,
        args_json: Optional[str] = None,
        result_json: Optional[str] = None,
        success: Optional[bool] = None,
        latency_ms: Optional[float] = None,
        timeout_ms: Optional[int] = None,
        status: str = "completed",
    ) -> Optional[int]:
        """Record a tool call. Returns row id, or None if duplicate idempotency_key."""
        if not self._conn:
            return None
        try:
            async with self._conn.execute(
                "INSERT OR IGNORE INTO tool_calls"
                " (ts, command_id, run_id, step_id, tool_name, idempotency_key,"
                "  args_json, result_json, success, latency_ms, timeout_ms, status)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    time.time(), command_id, run_id, step_id, tool_name,
                    idempotency_key, args_json, result_json,
                    int(success) if success is not None else None,
                    latency_ms, timeout_ms, status,
                ),
            ) as cur:
                row_id = cur.lastrowid if cur.rowcount else None
            await self._conn.commit()
            return row_id
        except Exception as exc:
            log.warning("AgentDB.insert_tool_call failed: %s", exc)
            return None

    async def get_tool_call_by_idempotency(self, idempotency_key: str) -> Optional[dict]:
        """Return a completed tool_call row for the given key, or None if not found."""
        if not self._conn:
            return None
        try:
            async with self._conn.execute(
                "SELECT * FROM tool_calls WHERE idempotency_key = ? AND status = 'completed' LIMIT 1",
                (idempotency_key,),
            ) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None
        except Exception as exc:
            log.warning("AgentDB.get_tool_call_by_idempotency failed: %s", exc)
            return None

    async def get_tool_timeout(self, tool_name: str) -> tuple[int, int]:
        """Return (timeout_ms, max_retries) for tool_name, or (15000, 0) if not configured."""
        if not self._conn:
            return (15_000, 0)
        try:
            async with self._conn.execute(
                "SELECT timeout_ms, max_retries FROM tool_timeout_config WHERE tool_name = ?",
                (tool_name,),
            ) as cur:
                row = await cur.fetchone()
                return (row["timeout_ms"], row["max_retries"]) if row else (15_000, 0)
        except Exception as exc:
            log.warning("AgentDB.get_tool_timeout failed: %s", exc)
            return (15_000, 0)

    async def get_rate_limit_config(self, resource: str) -> tuple[float, int]:
        """Return (max_rps, burst_capacity) for resource, or (2.0, 1) if not configured."""
        if not self._conn:
            return (2.0, 1)
        try:
            async with self._conn.execute(
                "SELECT max_rps, burst_capacity FROM rate_limit_config WHERE resource = ?",
                (resource,),
            ) as cur:
                row = await cur.fetchone()
                return (row["max_rps"], row["burst_capacity"]) if row else (2.0, 1)
        except Exception as exc:
            log.warning("AgentDB.get_rate_limit_config failed: %s", exc)
            return (2.0, 1)

