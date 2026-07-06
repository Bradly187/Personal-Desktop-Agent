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

class EventsRepo:
    def __init__(self, conn):
        self._conn = conn

    async def log_ipad_events(
        self,
        session_id: int,
        entries: list,
        trace_id: Optional[str] = None,
    ) -> None:
        """Persist a batch of structured log entries forwarded from the iPad app.

        Each entry is a dict with keys: ts (float), level (str), subsystem (str), msg (str).
        trace_id correlates entries to the PC-side command that triggered them (iPad↔PC
        trace correlation gap). Silently no-ops if DB is unavailable or entries is empty.
        """
        if not self._conn or not entries:
            return
        rows = [
            (
                session_id,
                float(e.get("ts", time.time())),
                str(e.get("level", "info"))[:16],
                str(e.get("subsystem", "unknown"))[:64],
                str(e.get("msg", ""))[:2048],
                trace_id,
            )
            for e in entries
            if isinstance(e, dict)
        ]
        if not rows:
            return
        await self._conn.executemany(
            "INSERT INTO ipad_logs (session_id, ts, level, subsystem, msg, trace_id)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        await self._conn.commit()

    async def insert_event_rule(
        self,
        *,
        topic_pattern: str,
        goal_template: str,
        name: Optional[str] = None,
        predicate: Optional[str] = None,
        action_kind: str = "notify",
        cooldown_s: float = 0.0,
    ) -> int:
        """Insert an event-triggered automation rule. Returns its row id, or -1."""
        if not self._conn:
            return -1
        try:
            cur = await self._conn.execute(
                """INSERT INTO event_rules
                   (created_at, enabled, name, topic_pattern, predicate,
                    goal_template, action_kind, cooldown_s)
                   VALUES (?, 1, ?, ?, ?, ?, ?, ?)""",
                (time.time(), name, topic_pattern, predicate, goal_template,
                 action_kind, cooldown_s),
            )
            await self._conn.commit()
            return int(cur.lastrowid) if cur.lastrowid else -1
        except Exception as exc:
            log.warning("AgentDB.insert_event_rule failed: %s", exc)
            return -1

    async def list_event_rules(self, enabled_only: bool = True) -> list[dict]:
        if not self._conn:
            return []
        try:
            sql = "SELECT * FROM event_rules"
            if enabled_only:
                sql += " WHERE enabled=1"
            cur = await self._conn.execute(sql)
            return [dict(r) for r in await cur.fetchall()]
        except Exception as exc:
            log.warning("AgentDB.list_event_rules failed: %s", exc)
            return []

    async def touch_rule_fired(self, rule_id: int, now: float) -> None:
        """Record a rule firing (for cooldown + observability)."""
        if not self._conn:
            return
        try:
            await self._conn.execute(
                "UPDATE event_rules SET last_fired_at=?, fire_count=fire_count+1 "
                "WHERE id=?",
                (now, rule_id),
            )
            await self._conn.commit()
        except Exception as exc:
            log.warning("AgentDB.touch_rule_fired failed: %s", exc)

    async def cancel_event_rule(self, rule_id: int) -> bool:
        if not self._conn:
            return False
        try:
            cur = await self._conn.execute(
                "UPDATE event_rules SET enabled=0 WHERE id=? AND enabled=1",
                (rule_id,),
            )
            await self._conn.commit()
            return (cur.rowcount or 0) > 0
        except Exception as exc:
            log.warning("AgentDB.cancel_event_rule failed: %s", exc)
            return False

    async def prune_event_log(self, days: int = 7) -> int:
        """Delete event_log rows older than `days`. Returns rows deleted."""
        if not self._conn:
            return 0
        cutoff = time.time() - days * 86400
        try:
            async with self._conn.execute(
                "DELETE FROM event_log WHERE ts < ?", (cutoff,)
            ) as cur:
                deleted = cur.rowcount or 0
            await self._conn.commit()
            if deleted:
                log.info("AgentDB: pruned %d event_log rows (> %d days)", deleted, days)
            return deleted
        except Exception as exc:
            log.warning("AgentDB.prune_event_log failed: %s", exc)
            return 0

    async def insert_event(
        self,
        topic: str,
        payload: str,
        source: str,
        *,
        session_id: Optional[int] = None,
        command_id: Optional[int] = None,
        trace_id: Optional[str] = None,
    ) -> Optional[int]:
        """Append one event to event_log. Returns the new row id, or None on error."""
        if not self._conn:
            return None
        try:
            async with self._conn.execute(
                "INSERT INTO event_log (ts, topic, session_id, command_id, trace_id, source, payload)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (time.time(), topic, session_id, command_id, trace_id, source, payload),
            ) as cur:
                row_id = cur.lastrowid
            await self._conn.commit()
            return row_id
        except Exception as exc:
            log.warning("AgentDB.insert_event failed: %s", exc)
            return None

    async def poll_events(
        self,
        topic_pattern: str,
        last_event_id: int,
        limit: int = 100,
    ) -> list[dict]:
        """Return up to `limit` events matching `topic_pattern` (SQL LIKE) with id > last_event_id."""
        if not self._conn:
            return []
        try:
            async with self._conn.execute(
                "SELECT id, ts, topic, session_id, command_id, trace_id, source, payload"
                " FROM event_log WHERE topic LIKE ? AND id > ? ORDER BY id LIMIT ?",
                (topic_pattern, last_event_id, limit),
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]
        except Exception as exc:
            log.warning("AgentDB.poll_events failed: %s", exc)
            return []

    async def upsert_event_consumer(self, consumer_name: str, topic_pattern: str) -> None:
        """Register a consumer; no-op if already registered."""
        if not self._conn:
            return
        try:
            await self._conn.execute(
                "INSERT OR IGNORE INTO event_consumers"
                " (consumer_name, topic_pattern, last_event_id, updated_at)"
                " VALUES (?, ?, 0, ?)",
                (consumer_name, topic_pattern, time.time()),
            )
            await self._conn.commit()
        except Exception as exc:
            log.warning("AgentDB.upsert_event_consumer failed: %s", exc)

    async def update_consumer_cursor(self, consumer_name: str, last_event_id: int) -> None:
        """Advance a consumer's cursor after processing events."""
        if not self._conn:
            return
        try:
            await self._conn.execute(
                "UPDATE event_consumers SET last_event_id = ?, updated_at = ?"
                " WHERE consumer_name = ?",
                (last_event_id, time.time(), consumer_name),
            )
            await self._conn.commit()
        except Exception as exc:
            log.warning("AgentDB.update_consumer_cursor failed: %s", exc)

    async def insert_rate_limit_event(
        self,
        resource: str,
        *,
        command_id: Optional[int] = None,
        wait_ms: float = 0.0,
        was_dropped: bool = False,
    ) -> None:
        """Record a rate-limit breach for observability. Non-fatal on error."""
        if not self._conn:
            return
        try:
            await self._conn.execute(
                "INSERT INTO rate_limit_events (ts, resource, command_id, wait_ms, was_dropped)"
                " VALUES (?, ?, ?, ?, ?)",
                (time.time(), resource, command_id, wait_ms, int(was_dropped)),
            )
            await self._conn.commit()
        except Exception as exc:
            log.debug("AgentDB.insert_rate_limit_event failed (non-fatal): %s", exc)

    async def insert_sensor_event(
        self,
        event_type: str,
        x: Optional[float] = None,
        y: Optional[float] = None,
        confidence: Optional[float] = None,
        value: Optional[str] = None,
        params: Optional[dict] = None,
        command_id: Optional[int] = None,
    ) -> None:
        if not self._conn:
            return
        try:
            await self._conn.execute(
                """INSERT INTO sensor_events
                   (command_id, ts, event_type, x, y, confidence, value, params)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    command_id if (command_id and command_id > 0) else None,
                    time.time(), event_type, x, y, confidence, value,
                    json.dumps(params) if params else None,
                ),
            )
            await self._conn.commit()
        except Exception as exc:
            log.warning("AgentDB.insert_sensor_event failed: %s", exc)

