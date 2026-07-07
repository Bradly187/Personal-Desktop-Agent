from __future__ import annotations
import json
import logging
from storage.repositories.common import PruneRetryMixin
import time
from typing import Optional, TYPE_CHECKING


if TYPE_CHECKING:
    from core.command_executor import Command

log = logging.getLogger(__name__)

class CommandsRepo(PruneRetryMixin):
    def __init__(self, conn):
        self._conn = conn

    async def insert_command(
        self,
        session_id: int,
        cmd: "Command",
        action: Optional[str],
        route: Optional[str],
        gate_that_decided: Optional[str],
        latency_ms: Optional[float],
        success: Optional[bool] = None,
        error_msg: Optional[str] = None,
        trace_id: Optional[str] = None,
        resolved_by: Optional[str] = None,
    ) -> int:
        """Insert a command routing record and return its id."""
        if not self._conn:
            return -1
        gaze_x = gaze_y = None
        if cmd.gaze_coords:
            gaze_x, gaze_y = cmd.gaze_coords
        success_int = None if success is None else int(success)
        try:
            cur = await self._conn.execute(
                """INSERT INTO commands
                   (session_id, ts, source, text, action, params,
                    route, gate_that_decided, latency_ms,
                    whisper_logprob, gesture_confidence,
                    gaze_x, gaze_y, success, error_msg, trace_id, resolved_by)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    session_id, time.time(), cmd.source, cmd.text,
                    action, json.dumps(
                        {k: v for k, v in cmd.params.items()
                         if not isinstance(v, (bytes, bytearray))}
                    ) if cmd.params else None,
                    route, gate_that_decided,
                    round(latency_ms, 1) if latency_ms is not None else None,
                    cmd.whisper_logprob, cmd.gesture_confidence,
                    gaze_x, gaze_y, success_int, error_msg, trace_id, resolved_by,
                ),
            )
            await self._conn.commit()
            return cur.lastrowid  # type: ignore[return-value]
        except Exception as exc:
            log.warning("AgentDB.insert_command failed: %s", exc)
            return -1

    async def mark_command_corrected(self, command_id: int, corrected_to: str) -> None:
        if not self._conn or command_id < 0:
            return
        await self._conn.execute(
            "UPDATE commands SET corrected_to = ? WHERE id = ?",
            (corrected_to, command_id),
        )
        await self._conn.commit()

    async def link_inferences_to_command(
        self, inference_ids: list[int], command_id: int
    ) -> None:
        """Backfill inferences.command_id once the command row exists.

        insert_command necessarily runs AFTER inference returns, so inference
        rows are first written with command_id=NULL and linked here (audit
        2026-06-09: without this backfill every command-pipeline inference row
        was unlinkable and the fine-tuning extraction JOIN matched nothing).
        Only NULL rows are updated — never relinks an already-linked row.
        """
        if not self._conn or not command_id or command_id <= 0:
            return
        ids = [int(i) for i in inference_ids if i and i > 0]
        if not ids:
            return
        try:
            await self._conn.execute(
                "UPDATE inferences SET command_id = ?"
                f" WHERE id IN ({','.join('?' * len(ids))})"
                " AND command_id IS NULL",
                (command_id, *ids),
            )
            await self._conn.commit()
        except Exception as exc:
            log.warning("AgentDB.link_inferences_to_command failed: %s", exc)

    async def prune_command_traces(self, days: int = 30) -> int:
        """Delete command_traces rows older than `days`. Returns rows deleted.

        Tracing is on by default (2026-06-19), so this table now grows with every
        command (~a handful of span rows each). 30 days keeps enough history for
        latency-attribution debugging and `monitoring/replay.py` replays while
        bounding growth — same cadence as tool_calls.
        """
        cutoff = time.time() - days * 86400
        return await self._prune_with_retry(
            "DELETE FROM command_traces WHERE ts < ?", (cutoff,),
            label=f"command_traces rows (> {days} days)",
        )

    async def get_recent_successful_commands(self, limit: int = 500) -> list[dict]:
        """Return the N most recent successful commands across all sessions."""
        if not self._conn:
            return []
        try:
            async with self._conn.execute(
                """SELECT text, action, source, ts, session_id
                   FROM commands
                   WHERE success = 1
                   ORDER BY ts DESC LIMIT ?""",
                (limit,),
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]
        except Exception as exc:
            log.warning("AgentDB.get_recent_successful_commands failed: %s", exc)
            return []

    async def get_command_stats_last_n_days(self, days: int = 30) -> list[dict]:
        """Return commands from the last N days for time-of-day distribution."""
        if not self._conn:
            return []
        cutoff = time.time() - days * 86400
        try:
            async with self._conn.execute(
                """SELECT ts, action, source, success
                   FROM commands
                   WHERE ts >= ?
                   ORDER BY ts DESC""",
                (cutoff,),
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]
        except Exception as exc:
            log.warning("AgentDB.get_command_stats_last_n_days failed: %s", exc)
            return []

