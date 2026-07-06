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

class RoutingRepo:
    _DOMAIN_OVERLAY_MAX = 5.0
    
    def __init__(self, conn):
        self._conn = conn

    async def get_recent_routing_stats(self, limit: int = 1000) -> list[dict]:
        """Return recent command rows for gate threshold adaptation."""
        if not self._conn:
            return []
        try:
            async with self._conn.execute(
                """SELECT route, action FROM commands
                   ORDER BY ts DESC LIMIT ?""",
                (limit,),
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]
        except Exception as exc:
            log.warning("AgentDB.get_recent_routing_stats failed: %s", exc)
            return []

    async def log_adaptation(
        self,
        component: str,
        metric_before: float,
        metric_after: float,
        cloud_rate: float = 0.0,
        failure_rate: float = 0.0,
        domain: Optional[str] = None,
    ) -> int:
        """Insert one adaptation_log row. Returns the new row id.

        `domain` tags per-domain SLO adaptations (gap H); None for global ones.
        """
        if not self._conn:
            return -1
        try:
            cur = await self._conn.execute(
                """INSERT INTO adaptation_log
                   (ts, component, metric_before, metric_after, cloud_rate, failure_rate, domain)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (time.time(), component, metric_before, metric_after,
                 cloud_rate, failure_rate, domain),
            )
            await self._conn.commit()
            return cur.lastrowid  # type: ignore[return-value]
        except Exception as exc:
            log.warning("AgentDB.log_adaptation failed: %s", exc)
            return -1

    async def get_recent_adaptation_log(
        self, component: str, limit: int = 5
    ) -> list[dict]:
        """Return the most recent NON-rolled-back adaptation_log rows for a
        component. Rolled-back rows are excluded so the D5 rollback check never
        re-evaluates (and re-triggers on) an adaptation it already undid."""
        if not self._conn:
            return []
        try:
            async with self._conn.execute(
                """SELECT id, ts, metric_before, metric_after,
                          cloud_rate, failure_rate, rolled_back
                   FROM adaptation_log
                   WHERE component = ? AND rolled_back = 0
                   ORDER BY ts DESC LIMIT ?""",
                (component, limit),
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]
        except Exception as exc:
            log.warning("AgentDB.get_recent_adaptation_log failed: %s", exc)
            return []

    async def mark_adaptation_rolled_back(self, row_id: int) -> None:
        """Set rolled_back=1 for the given adaptation_log row."""
        if not self._conn or row_id < 0:
            return
        try:
            await self._conn.execute(
                "UPDATE adaptation_log SET rolled_back=1 WHERE id=?", (row_id,)
            )
            await self._conn.commit()
        except Exception as exc:
            log.warning("AgentDB.mark_adaptation_rolled_back failed: %s", exc)

    async def get_domain_misroutes(self, limit: int = 1000) -> list[dict]:
        """Per-domain routed vs corrected counts → a misroute rate signal (E1).

        Joins each inference's chosen `domain` to its command and counts how many
        of those commands the user later corrected (`corrected_to IS NOT NULL`).
        A high rate means that domain's routing/handling is error-prone. Returns
        [{domain, routed, corrected, rate}], busiest domains first. Read-only —
        the misroute analyzer logs from this; it does not itself change routing.
        """
        if not self._conn:
            return []
        try:
            async with self._conn.execute(
                """SELECT i.domain AS domain,
                          COUNT(*) AS routed,
                          SUM(CASE WHEN c.corrected_to IS NOT NULL THEN 1 ELSE 0 END)
                              AS corrected
                   FROM inferences i
                   JOIN commands c ON c.id = i.command_id
                   WHERE i.command_id IS NOT NULL
                   GROUP BY i.domain
                   ORDER BY routed DESC
                   LIMIT ?""",
                (limit,),
            ) as cur:
                out = []
                for r in await cur.fetchall():
                    d = dict(r)
                    routed = d.get("routed") or 0
                    corrected = d.get("corrected") or 0
                    d["rate"] = (corrected / routed) if routed else 0.0
                    out.append(d)
                return out
        except Exception as exc:
            log.warning("AgentDB.get_domain_misroutes failed: %s", exc)
            return []

    async def upsert_domain_keyword_weight(self, domain: str, keyword: str,
                                           weight: float) -> None:
        if not self._conn or not domain or not keyword:
            return
        w = max(0.0, min(self._DOMAIN_OVERLAY_MAX, float(weight)))
        try:
            await self._conn.execute(
                """INSERT INTO domain_keyword_weights (domain, keyword, weight, ts)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(domain, keyword) DO UPDATE
                     SET weight = excluded.weight, ts = excluded.ts""",
                (domain, keyword.lower(), w, time.time()),
            )
            await self._conn.commit()
        except Exception as exc:
            log.warning("AgentDB.upsert_domain_keyword_weight failed: %s", exc)

    async def get_domain_keyword_weights(self) -> dict:
        """Return {domain: {keyword: weight}} — the full learned overlay."""
        if not self._conn:
            return {}
        try:
            async with self._conn.execute(
                "SELECT domain, keyword, weight FROM domain_keyword_weights"
            ) as cur:
                out: dict = {}
                for r in await cur.fetchall():
                    out.setdefault(r["domain"], {})[r["keyword"]] = r["weight"]
                return out
        except Exception as exc:
            log.warning("AgentDB.get_domain_keyword_weights failed: %s", exc)
            return {}

    async def clear_domain_keyword_overlay(self, domain: str) -> None:
        """Drop a domain's learned overlay (rollback path)."""
        if not self._conn or not domain:
            return
        try:
            await self._conn.execute(
                "DELETE FROM domain_keyword_weights WHERE domain = ?", (domain,))
            await self._conn.commit()
        except Exception as exc:
            log.warning("AgentDB.clear_domain_keyword_overlay failed: %s", exc)

