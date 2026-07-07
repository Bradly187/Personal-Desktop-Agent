from __future__ import annotations
import logging
from storage.repositories.common import _GOAL_LEASE_TTL_S, _pid_alive, PruneRetryMixin
import os
import time
from typing import Optional, TYPE_CHECKING


if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)

class RunsRepo(PruneRetryMixin):
    def __init__(self, conn):
        self._conn = conn

    async def insert_agent_run(
        self,
        command_id: Optional[int],
        goal: str,
        domain: str,
        model_used: Optional[str],
        step_count: int,
        success: bool,
        total_latency_ms: float,
        error: Optional[str] = None,
    ) -> int:
        if not self._conn:
            return -1
        try:
            cur = await self._conn.execute(
                """INSERT INTO agent_runs
                   (command_id, ts, goal, domain, model_used,
                    step_count, success, total_latency_ms, error)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    command_id if (command_id and command_id > 0) else None,
                    time.time(), goal, domain, model_used,
                    step_count, int(success), round(total_latency_ms, 1), error,
                ),
            )
            await self._conn.commit()
            return cur.lastrowid  # type: ignore[return-value]
        except Exception as exc:
            log.warning("AgentDB.insert_agent_run failed: %s", exc)
            return -1

    async def insert_agent_step(
        self,
        run_id: int,
        step_num: int,
        action: str,
        args: Optional[str],
        body: Optional[str],
        result: Optional[str],
        success: Optional[bool],
        latency_ms: float,
        compensation_action: Optional[str] = None,
        compensation_args: Optional[str] = None,
    ) -> Optional[int]:
        """Insert a step record. Returns the new row id (for saga compensation wiring), or None."""
        if not self._conn or run_id < 0:
            return None
        try:
            async with self._conn.execute(
                """INSERT INTO agent_steps
                   (run_id, step_num, action, args, body, result, success, latency_ms,
                    compensation_action, compensation_args)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id, step_num, action, args or None, body or None,
                    result or None,
                    None if success is None else int(success),
                    round(latency_ms, 1),
                    compensation_action,
                    compensation_args,
                ),
            ) as cur:
                row_id = cur.lastrowid
            await self._conn.commit()
            return row_id
        except Exception as exc:
            log.warning("AgentDB.insert_agent_step failed: %s", exc)
            return None

    async def update_agent_step(
        self,
        step_id: int,
        result: Optional[str],
        success: Optional[bool],
        latency_ms: float,
    ) -> None:
        """Update an existing step record with execution results."""
        if not self._conn or step_id < 0:
            return
        try:
            await self._conn.execute(
                """UPDATE agent_steps
                   SET result = ?, success = ?, latency_ms = ?
                   WHERE id = ?""",
                (
                    result or None,
                    None if success is None else int(success),
                    round(latency_ms, 1),
                    step_id,
                ),
            )
            await self._conn.commit()
        except Exception as exc:
            log.warning("AgentDB.update_agent_step failed: %s", exc)

    async def start_agent_run(
        self,
        goal: str,
        domain: str = "plan",
        model_used: Optional[str] = None,
        command_id: Optional[int] = None,
    ) -> int:
        """Insert a run row with status='running' and return its id.

        Steps are appended via insert_agent_step as they complete; the run is
        finalised with update_agent_run. A row left 'running' (process crash) is
        reconciled to 'interrupted' on next startup by mark_interrupted_runs().
        """
        if not self._conn:
            return -1
        try:
            cur = await self._conn.execute(
                """INSERT INTO agent_runs
                   (command_id, ts, goal, domain, model_used, step_count,
                    success, total_latency_ms, error, status)
                   VALUES (?,?,?,?,?,0,NULL,NULL,NULL,'running')""",
                (
                    command_id if (command_id and command_id > 0) else None,
                    time.time(), goal, domain, model_used,
                ),
            )
            await self._conn.commit()
            return cur.lastrowid  # type: ignore[return-value]
        except Exception as exc:
            log.warning("AgentDB.start_agent_run failed: %s", exc)
            return -1

    async def update_agent_run(
        self,
        run_id: int,
        status: str,
        step_count: int,
        success: bool,
        total_latency_ms: float,
        error: Optional[str] = None,
    ) -> None:
        """Finalise a run with a terminal status ('completed'/'failed'/'cancelled')."""
        if not self._conn or run_id < 0:
            return
        try:
            await self._conn.execute(
                """UPDATE agent_runs
                   SET status=?, step_count=?, success=?, total_latency_ms=?, error=?
                   WHERE id=?""",
                (status, step_count, int(success), round(total_latency_ms, 1), error, run_id),
            )
            await self._conn.commit()
        except Exception as exc:
            log.warning("AgentDB.update_agent_run failed: %s", exc)

    async def mark_interrupted_runs(self) -> int:
        """Reconcile orphaned 'running' rows to 'interrupted'. Returns the count.

        Called once at startup: any run still 'running' means the process died
        mid-plan. Returns how many rows were reconciled so the caller can offer
        a resume.
        """
        if not self._conn:
            return 0
        try:
            cur = await self._conn.execute(
                "UPDATE agent_runs SET status='interrupted' WHERE status='running'"
            )
            await self._conn.commit()
            return cur.rowcount if cur.rowcount is not None else 0
        except Exception as exc:
            log.warning("AgentDB.mark_interrupted_runs failed: %s", exc)
            return 0

    async def get_interrupted_runs(self, limit: int = 10) -> list[dict]:
        """Return recent interrupted runs (most recent first) for resume offers."""
        if not self._conn:
            return []
        try:
            cur = await self._conn.execute(
                """SELECT id, goal, domain, model_used, step_count, ts
                   FROM agent_runs WHERE status='interrupted'
                   ORDER BY ts DESC LIMIT ?""",
                (limit,),
            )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]
        except Exception as exc:
            log.warning("AgentDB.get_interrupted_runs failed: %s", exc)
            return []

    async def get_steps_for_run(self, run_id: int) -> list[dict]:
        """Ordered steps for one run (specs/resume-working-memory, Gap C).

        Read-only SELECT over the existing ``agent_steps`` ledger — no schema
        change. Returns [] on any failure so resume degrades cleanly (R3.2)."""
        if not self._conn:
            return []
        try:
            async with self._conn.execute(
                """SELECT step_num, action, args, body, result, success
                   FROM agent_steps WHERE run_id = ? ORDER BY step_num ASC""",
                (run_id,),
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]
        except Exception as exc:
            log.warning("AgentDB.get_steps_for_run failed: %s", exc)
            return []

    async def get_recent_runs(
        self, limit: int = 20, exclude_id: Optional[int] = None
    ) -> list[dict]:
        """Recent terminal/interrupted runs, most recent first (R4.2, Gap C).

        Read-only SELECT over ``agent_runs`` — no schema change, no
        ``user_version`` bump. Powers cross-session working-memory: a fresh plan
        scans these for runs related to its goal. ``exclude_id`` is defensive only
        (the current run row isn't created until after context assembly). Returns
        [] on any failure so seeding degrades cleanly (R4.4 / R3.2)."""
        if not self._conn:
            return []
        try:
            cur = await self._conn.execute(
                """SELECT id, goal, ts, success, status
                   FROM agent_runs
                   WHERE status IN ('completed','failed','interrupted')
                     AND (? IS NULL OR id != ?)
                   ORDER BY ts DESC LIMIT ?""",
                (exclude_id, exclude_id, limit),
            )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]
        except Exception as exc:
            log.warning("AgentDB.get_recent_runs failed: %s", exc)
            return []

    async def get_successful_runs_with_steps(
        self, *, since: float = 0.0, min_steps: int = 2, limit: int = 500
    ) -> list[dict]:
        """Successful agent runs with their ordered step trajectory.

        The raw material for MacroDetector (self-skilling rung 2): each returned
        dict is {id, goal, domain, ts, steps:[{step_num, action, args, body,
        success}, ...]} with steps ordered by step_num. Only runs with
        success=1 and at least `min_steps` steps are returned — a 1-step "macro"
        is just a single verb and carries no composition worth saving. Read-only;
        intended to be called off the hot path by the offline detector.
        """
        if not self._conn:
            return []
        try:
            async with self._conn.execute(
                """SELECT id, goal, domain, ts FROM agent_runs
                   WHERE success = 1 AND ts >= ? AND step_count >= ?
                   ORDER BY ts DESC LIMIT ?""",
                (since, min_steps, limit),
            ) as cur:
                runs = [dict(r) for r in await cur.fetchall()]
            if not runs:
                return []
            ids = [r["id"] for r in runs]
            placeholders = ",".join("?" * len(ids))
            steps_by_run: dict[int, list[dict]] = {rid: [] for rid in ids}
            async with self._conn.execute(
                f"""SELECT run_id, step_num, action, args, body, success
                    FROM agent_steps WHERE run_id IN ({placeholders})
                    ORDER BY run_id, step_num ASC""",
                tuple(ids),
            ) as cur:
                for r in await cur.fetchall():
                    row = dict(r)
                    steps_by_run.setdefault(row["run_id"], []).append({
                        "step_num": row["step_num"], "action": row["action"],
                        "args": row["args"], "body": row["body"],
                        "success": row["success"],
                    })
            out = []
            for run in runs:
                steps = steps_by_run.get(run["id"], [])
                if len(steps) >= min_steps:
                    run["steps"] = steps
                    out.append(run)
            return out
        except Exception as exc:
            log.warning("AgentDB.get_successful_runs_with_steps failed: %s", exc)
            return []

    async def requeue_stale_running(self) -> int:
        """Startup recovery: a goal left 'running' means the process died mid-goal.

        Requeue it (attempts already counted the failed try) when under
        max_attempts; otherwise mark it 'failed' so a poison goal can't loop
        forever. Returns the number requeued.

        Lease guard (#9): a 'running' row whose owner_pid is a DIFFERENT, still
        alive process belongs to a concurrent instance — never clobber its claim.
        Our own pid, a dead owner, or no owner means the previous run died and the
        goal is genuinely recoverable.
        """
        if not self._conn:
            return 0
        try:
            cur = await self._conn.execute(
                "SELECT id, attempts, max_attempts, owner_pid, claimed_at "
                "FROM goal_queue WHERE status='running'"
            )
            rows = await cur.fetchall()
            self_pid = os.getpid()
            now = time.time()
            requeued = 0
            for r in rows:
                pid = r["owner_pid"]
                # E15: a different live owner whose lease is still within the TTL
                # is a healthy concurrent instance — leave it. But a lease that
                # outlived the TTL means that owner wedged, so recover it too.
                claimed_at = r["claimed_at"]
                lease_expired = (
                    claimed_at is not None
                    and (now - float(claimed_at)) > _GOAL_LEASE_TTL_S
                )
                if (pid is not None and int(pid) != self_pid
                        and _pid_alive(pid) and not lease_expired):
                    continue  # live concurrent instance, lease still valid
                if int(r["attempts"]) >= int(r["max_attempts"]):
                    await self._conn.execute(
                        "UPDATE goal_queue SET status='failed', "
                        "last_error='exceeded max_attempts after crash' WHERE id=?",
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
            return requeued
        except Exception as exc:
            log.warning("AgentDB.requeue_stale_running failed: %s", exc)
            return 0

    async def prune_episodic_memory(self, cap: int = 2000) -> int:
        """Keep the newest `cap` notes; delete the rest. Returns rows deleted."""
        if not self._conn:
            return 0
        try:
            async with self._conn.execute(
                "SELECT COUNT(*) FROM episodic_memory"
            ) as cur:
                total = (await cur.fetchone())[0]
            if total <= cap:
                return 0
            await self._conn.execute(
                """DELETE FROM episodic_memory WHERE id IN (
                       SELECT id FROM episodic_memory ORDER BY ts DESC
                       LIMIT -1 OFFSET ?
                   )""",
                (cap,),
            )
            await self._conn.commit()
            return total - cap
        except Exception as exc:
            log.warning("AgentDB.prune_episodic_memory failed: %s", exc)
            return 0

    async def prune_gesture_velocity_samples(self, days: int = 90) -> int:
        """Delete gesture_velocity_samples rows older than `days`. Returns rows deleted.

        At ~7,200/day, 90 days = ~648,000 rows. Retaining 90 days preserves enough
        signal for ContinuousTrainer's p10 velocity-floor calibration.
        """
        cutoff = time.time() - days * 86400
        return await self._prune_with_retry(
            "DELETE FROM gesture_velocity_samples WHERE ts < ?", (cutoff,),
            label=f"gesture_velocity_samples rows (> {days} days)",
        )

    async def prune_ipad_logs(self, days: int = 60) -> int:
        """Delete ipad_logs rows older than `days`. Returns rows deleted."""
        if not self._conn:
            return 0
        cutoff = time.time() - days * 86400
        try:
            async with self._conn.execute(
                "DELETE FROM ipad_logs WHERE ts < ?", (cutoff,)
            ) as cur:
                deleted = cur.rowcount or 0
            await self._conn.commit()
            if deleted:
                log.info("AgentDB: pruned %d ipad_logs rows (> %d days)", deleted, days)
            return deleted
        except Exception as exc:
            log.warning("AgentDB.prune_ipad_logs failed: %s", exc)
            return 0

    async def prune_tool_calls(self, days: int = 30) -> int:
        """Delete tool_calls rows older than `days`. Returns rows deleted."""
        if not self._conn:
            return 0
        cutoff = time.time() - days * 86400
        try:
            async with self._conn.execute(
                "DELETE FROM tool_calls WHERE ts < ?", (cutoff,)
            ) as cur:
                deleted = cur.rowcount or 0
            await self._conn.commit()
            if deleted:
                log.info("AgentDB: pruned %d tool_calls rows (> %d days)", deleted, days)
            return deleted
        except Exception as exc:
            log.warning("AgentDB.prune_tool_calls failed: %s", exc)
            return 0

    async def prune_rate_limit_events(self, days: int = 7) -> int:
        """Delete rate_limit_events rows older than `days`. Returns rows deleted."""
        if not self._conn:
            return 0
        cutoff = time.time() - days * 86400
        try:
            async with self._conn.execute(
                "DELETE FROM rate_limit_events WHERE ts < ?", (cutoff,)
            ) as cur:
                deleted = cur.rowcount or 0
            await self._conn.commit()
            if deleted:
                log.info("AgentDB: pruned %d rate_limit_events rows (> %d days)", deleted, days)
            return deleted
        except Exception as exc:
            log.warning("AgentDB.prune_rate_limit_events failed: %s", exc)
            return 0

