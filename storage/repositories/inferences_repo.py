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

class InferencesRepo:
    def __init__(self, conn):
        self._conn = conn

    async def insert_inference(
        self,
        command_id: Optional[int],
        model: str,
        domain: str,
        prompt: Optional[str],
        response: Optional[str],
        tokens_in: Optional[int],
        tokens_out: Optional[int],
        latency_ms: float,
        backend: str = "ollama",
        error: Optional[str] = None,
    ) -> int:
        if not self._conn:
            return -1
        prompt_hash = (
            hashlib.sha256(prompt.encode()).hexdigest()[:16] if prompt else None
        )
        try:
            cur = await self._conn.execute(
                """INSERT INTO inferences
                   (command_id, ts, model, domain, prompt_hash, prompt, response,
                    tokens_in, tokens_out, latency_ms, backend, error)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    command_id if (command_id and command_id > 0) else None,
                    time.time(), model, domain, prompt_hash, prompt, response,
                    tokens_in, tokens_out, round(latency_ms, 1), backend, error,
                ),
            )
            await self._conn.commit()
            return cur.lastrowid  # type: ignore[return-value]
        except Exception as exc:
            log.warning("AgentDB.insert_inference failed: %s", exc)
            return -1

    async def get_inference_stats_by_domain(self, limit: int = 1000) -> dict:
        """Per-domain rolling stats from the inferences table (gap H).

        Returns {domain: {count, p50_latency_ms, success_rate}} over the most
        recent `limit` inference rows. p50 + success-rate are computed in Python
        (SQLite has no median); success = the inference recorded no error.
        """
        if not self._conn:
            return {}
        try:
            async with self._conn.execute(
                "SELECT domain, latency_ms, error FROM inferences ORDER BY ts DESC LIMIT ?",
                (limit,),
            ) as cur:
                rows = await cur.fetchall()
        except Exception as exc:
            log.warning("AgentDB.get_inference_stats_by_domain failed: %s", exc)
            return {}

        buckets: dict[str, list] = {}
        for r in rows:
            buckets.setdefault(r["domain"], []).append((r["latency_ms"], r["error"]))
        out: dict[str, dict] = {}
        for domain, items in buckets.items():
            lats = sorted(l for l, _ in items if l is not None)
            p50 = lats[len(lats) // 2] if lats else None
            ok = sum(1 for _, e in items if e is None)
            out[domain] = {
                "count": len(items),
                "p50_latency_ms": round(p50, 1) if p50 is not None else None,
                "success_rate": round(ok / len(items), 3) if items else None,
            }
        return out

