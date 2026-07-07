import asyncio
import json
import logging
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Optional, TYPE_CHECKING
from core.events import TOPIC_STEP_FAILED, TOPIC_REPLAN_EXHAUSTED, TOPIC_DAG_RUN_FINALIZED
from inference.plan_parser import AgentResult, AgentStep

if TYPE_CHECKING:
    from storage.db import AgentDB
    from inference.dev_agent import DevAgent

log = logging.getLogger(__name__)

class SagaManager:
    def __init__(self, agent: "DevAgent", agent_db: Optional['AgentDB'] = None):
        # Shared run state (_escalated_this_run, _rollback_summary,
        # _active_trace_id, _saga_announce, _escalation_sidecar_path) lives on
        # the agent — reads delegate via __getattr__, writes go through
        # self._agent.X so DevAgent stays the single source of truth.
        self._agent = agent
        self._agent_db = agent_db

    async def _start_run(self, goal: str, model_used: Optional[str]) -> int:
        db = self._db()
        if not db or not getattr(db, "available", False):
            return -1
        try:
            return await db.runs.start_agent_run(goal=goal, domain="plan", model_used=model_used)
        except Exception as exc:
            log.debug("DevAgent._start_run failed: %s", exc)
            return -1

    # Max file size we snapshot for a WRITE_FILE rollback. Above this, we record
    # that the file existed (so rollback won't delete it) but keep no backup.
    _SAGA_SNAPSHOT_MAX_BYTES = 256 * 1024

    @staticmethod
    def _saga_dir() -> Path:
        return Path.home() / ".claude" / "saga"

    @classmethod
    def _snapshot_for_write(cls, path_str: Optional[str]) -> dict:
        """Capture a WRITE_FILE pre-write snapshot for saga rollback.

        Returns {path, existed, backup}: `existed` is whether the target file
        was present before the write; `backup` is a copy of its prior bytes (or
        None when it didn't exist, or was too large to snapshot). On rollback:
        existed+backup → restore; existed+no-backup → leave the overwritten file
        (deleting would lose data we couldn't back up); not-existed → delete.
        """
        info: dict = {"path": "", "existed": False, "backup": None}
        if not path_str:
            return info
        try:
            p = Path(path_str.strip().strip("'\""))
            info["path"] = str(p)
            if p.exists() and p.is_file():
                info["existed"] = True
                # Git-blob backend (opt-in, DA_SAGA_GIT_BACKEND): capture the
                # current bytes as a git loose object. No size cap (closes the
                # file-copy backend's >256 KB rollback gap), git-native and
                # inspectable (`git cat-file blob <sha>`), and it touches ONLY the
                # object store — never the working tree, index, or stash stack.
                # Degrades to the file-copy backend below when git/repo is absent.
                if cls._saga_git_backend_enabled():
                    blob = cls._git_blob_snapshot(p)
                    if blob:
                        info["git_blob"] = blob["sha"]
                        info["git_repo"] = blob["repo"]
                        return info
                if p.stat().st_size <= cls._SAGA_SNAPSHOT_MAX_BYTES:
                    saga = cls._saga_dir()
                    saga.mkdir(parents=True, exist_ok=True)
                    backup = saga / f"{p.name}.{uuid.uuid4().hex}.bak"
                    shutil.copy2(p, backup)
                    info["backup"] = str(backup)
        except Exception as exc:
            log.debug("DevAgent._snapshot_for_write(%r) failed: %s", path_str, exc)
        return info

    @staticmethod
    def _saga_git_backend_enabled() -> bool:
        """Whether the git-blob saga snapshot backend is on (DA_SAGA_GIT_BACKEND).

        Default OFF → byte-identical file-copy snapshots. Read per-call (not the
        60 Hz path; only fires on a dev-agent file write) so tests/ops can toggle
        it via the env without reconstructing the agent."""
        return os.environ.get(
            "DA_SAGA_GIT_BACKEND", "0").strip().lower() in ("1", "true", "on", "yes")

    @staticmethod
    def _git_blob_snapshot(p: Path) -> Optional[dict]:
        """Write p's current bytes into the git object store; return {sha, repo}.

        Returns None when p is not inside a git work tree or git is unavailable —
        the caller then falls back to the file-copy backend. `hash-object -w` only
        creates a loose object; it does NOT stage the file or alter the working
        tree/index/stash, so a snapshot is side-effect-free for the user's repo.
        """
        try:
            top = subprocess.run(
                ["git", "-C", str(p.parent), "rev-parse", "--show-toplevel"],
                capture_output=True, text=True, timeout=5,
            )
            repo_root = top.stdout.strip()
            if top.returncode != 0 or not repo_root:
                return None
            out = subprocess.run(
                ["git", "-C", repo_root, "hash-object", "-w", "--", str(p)],
                capture_output=True, text=True, timeout=10,
            )
            sha = out.stdout.strip()
            if out.returncode != 0 or not sha:
                return None
            return {"sha": sha, "repo": repo_root}
        except Exception as exc:
            log.debug("DevAgent._git_blob_snapshot(%s) failed: %s", p, exc)
            return None

    @staticmethod
    def _git_cat_blob(repo: str, sha: str) -> Optional[bytes]:
        """Return the bytes of git blob `sha` from `repo`, or None if unavailable."""
        try:
            out = subprocess.run(
                ["git", "-C", repo, "cat-file", "blob", sha],
                capture_output=True, timeout=10,
            )
            if out.returncode != 0:
                return None
            return out.stdout
        except Exception as exc:
            log.debug("DevAgent._git_cat_blob(%s) failed: %s", sha, exc)
            return None

    @staticmethod
    def _compensation_for(step: "AgentStep") -> tuple[Optional[str], Optional[str]]:
        """Return (compensation_action, compensation_args) for a completed step, or (None, None)."""
        action = step.action.upper()
        if action in ("WRITE_FILE", "EDIT_FILE"):
            # Prefer the execute-time snapshot (RESTORE_FILE: restore an
            # overwritten/edited file or delete a freshly-created one). Fall back
            # to the legacy blind DELETE_FILE only if no snapshot was captured
            # (EDIT_FILE always edits an existing file, so its snapshot is always
            # present → RESTORE_FILE, never the DELETE_FILE fallback).
            if step.comp_args:
                return "RESTORE_FILE", step.comp_args
            return "DELETE_FILE", step.args.strip() if step.args else None
        if action == "RUN_TERMINAL":
            # Terminal side-effects can't be automatically reversed, but we
            # record the command so a human reviewer can manually undo.
            return "REVERT_TERMINAL", step.args or step.body or None
        return None, None

    async def _pre_register_step(self, step: "AgentStep") -> None:
        """S2.3: Insert the step early so snapshot compensations have a step_id."""
        db = self._db()
        if not db or not getattr(db, "available", False) or step.run_id is None or step.step_num is None:
            return
        if step.db_id is not None:
            return
        comp_action, comp_args = self._compensation_for(step)
        try:
            step.db_id = await db.runs.insert_agent_step(
                run_id=step.run_id, step_num=step.step_num, action=step.action,
                args=step.args or None, body=step.body or None,
                result=None, success=None, latency_ms=0.0,
                compensation_action=comp_action,
                compensation_args=comp_args,
            )
        except Exception as exc:
            log.debug("DevAgent._pre_register_step failed: %s", exc)

    async def _persist_step(self, run_id: int, step_num: int, step: "AgentStep") -> None:
        # Publish step.failed (best-effort, independent of DB persistence) so
        # observer agents (R-1) and event rules react even if the DB is down.
        # Single chokepoint for both the sequential and DAG execution paths.
        if step.success is False and run_id >= 0 and self._event_bus is not None:
            try:
                await self._event_bus.publish(
                    TOPIC_STEP_FAILED,
                    {"run_id": run_id, "step_num": step_num,
                     "action": step.action, "error": (step.result or "")[:200]},
                    source="dev_agent",
                    trace_id=self._active_trace_id or None,
                )
            except Exception as _pub_exc:
                log.debug("DevAgent: step.failed publish failed: %s", _pub_exc)
        # Live DAG: mark this node done (success or fail) for the chat UI. Single
        # chokepoint for both the sequential and DAG-wave execution paths.
        if step.success is not None:
            await self._emit_step_completed(step, step_num)
        db = self._db()
        if run_id < 0 or not db or not getattr(db, "available", False):
            return
        comp_action, comp_args = self._compensation_for(step)
        try:
            if step.db_id is not None:
                await db.runs.update_agent_step(
                    step.db_id, result=step.result, success=step.success, latency_ms=step.latency_ms
                )
                step_id = step.db_id
            else:
                step_id = await db.runs.insert_agent_step(
                    run_id=run_id, step_num=step_num, action=step.action,
                    args=step.args or None, body=step.body or None,
                    result=step.result, success=step.success, latency_ms=step.latency_ms,
                    compensation_action=comp_action,
                    compensation_args=comp_args,
                )
                step.db_id = step_id

            # Register a saga compensation row for every successful step that
            # has a defined reverse action, so they can be unwound on failure.
            # E6: a WRITE_FILE/EDIT_FILE that FAILED may still have PARTIALLY
            # modified the file (truncated/half-written then errored). If a
            # pre-write snapshot was captured, register its RESTORE too so the
            # partial write is rolled back — restoring is a safe no-op if the file
            # was untouched.
            register = bool(step.success)
            if (not step.success and step.action.upper() in ("WRITE_FILE", "EDIT_FILE")
                    and step.comp_args):
                register = True
            if register and comp_action and step_id is not None:
                if step.comp_id is None:
                    step.comp_id = await db.sagas.insert_saga_compensation(
                        run_id=run_id, step_id=step_id,
                        compensation_action=comp_action,
                        compensation_args=comp_args,
                    )
        except Exception as exc:
            log.debug("DevAgent._persist_step failed: %s", exc)

    async def _halt_and_compensate(
        self, run_id: int, goal: str, replans: int, failed_action: str
    ) -> None:
        """Publish the replan-exhausted event (best-effort) and roll back
        completed side effects. Used on every replan-exhausted terminal path
        (sequential and DAG)."""
        if self._event_bus is not None:
            try:
                await self._event_bus.publish(
                    TOPIC_REPLAN_EXHAUSTED,
                    {"run_id": run_id, "goal": goal[:120], "replans": replans,
                     "failed_action": failed_action},
                    source="dev_agent",
                    trace_id=self._active_trace_id or None,
                )
            except Exception as _pub_exc:
                log.debug("DevAgent: event publish failed: %s", _pub_exc)
        incomplete = await self._run_compensations(run_id, triggered_by="max_replans")
        await self._record_escalation(run_id, goal, "max_replans", failed_action, replans,
                                      incomplete=incomplete)

    def _escalation_sidecar(self) -> Path:
        """Durable fallback store for escalations the DB couldn't accept (E4)."""
        return self._escalation_sidecar_path

    async def _record_escalation(
        self, run_id: int, goal: str, reason: str,
        failed_action: Optional[str], replans: int, *, incomplete: int = 0,
    ) -> None:
        """Persist a halted plan to the human-review escalation queue.

        Called only on budget-exhaustion halts (max_replans / max_steps) — a
        user cancel is deliberate and never escalates. The rollback already ran,
        so this must not raise.

        E4: the escalation must NOT be silently lost when the DB is down or the
        INSERT fails (insert_escalation swallows its own error and returns None).
        On any non-persist, the row is appended to a durable JSONL sidecar and
        reconciled into dev_escalations on the next healthy boot. ``incomplete``
        is the number of saga compensations that did not roll back cleanly (E5);
        it rides along in detail so the reviewer sees a partial rollback.
        _escalated_this_run is set ONLY when the row was actually persisted
        somewhere — so the completion TTS never claims "saved" when it wasn't.
        """
        detail = json.dumps({"current_step": self._current_step,
                             "total_steps": self._total_steps,
                             "incomplete_compensations": incomplete})
        persisted = False
        db = self._db()
        if db and getattr(db, "available", False):
            try:
                row_id = await db.sagas.insert_escalation(
                    run_id, goal, reason,
                    failed_action=failed_action, replans=replans, detail=detail,
                )
                persisted = row_id is not None   # insert_escalation returns None on failure
            except Exception as exc:
                log.warning("DevAgent._record_escalation DB insert failed: %s", exc)
        if not persisted:
            persisted = await asyncio.to_thread(
                self._append_escalation_sidecar, self._escalation_sidecar(),
                {"run_id": run_id, "goal": goal, "reason": reason,
                 "failed_action": failed_action, "replans": replans, "detail": detail,
                 "ts": time.time()},
            )
            if persisted:
                log.warning("DevAgent: DB unavailable — escalation saved to sidecar "
                            "for reconcile on next boot (%s): %.60s", reason, goal)
        if persisted:
            self._agent._escalated_this_run = True
            log.info("DevAgent: escalated halted plan to review queue (%s): %.60s",
                     reason, goal)
        else:
            log.error("DevAgent: FAILED to persist escalation anywhere (%s): %.60s",
                      reason, goal)

    @staticmethod
    def _append_escalation_sidecar(path: Path, row: dict) -> bool:
        """Append one escalation row to the JSONL sidecar. Returns success."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
            return True
        except Exception as exc:
            log.error("DevAgent: escalation sidecar append failed: %s", exc)
            return False

    async def reconcile_pending_escalations(self) -> int:
        """Drain the escalation sidecar (E4) into dev_escalations at startup.

        Each line that inserts cleanly is dropped; anything that still fails is
        kept for the next attempt. Returns the number reconciled. Safe no-op when
        the sidecar is absent or the DB is unavailable.
        """
        db = self._db()
        if not db or not getattr(db, "available", False):
            return 0
        path = self._escalation_sidecar()
        rows = await asyncio.to_thread(self._read_escalation_sidecar, path)
        if not rows:
            return 0
        reconciled = 0
        leftover: list[dict] = []
        for row in rows:
            try:
                rid = await db.sagas.insert_escalation(
                    int(row.get("run_id", -1)), row.get("goal", ""), row.get("reason", ""),
                    failed_action=row.get("failed_action"),
                    replans=int(row.get("replans", 0)), detail=row.get("detail"),
                )
                if rid is not None:
                    reconciled += 1
                else:
                    leftover.append(row)
            except Exception:
                leftover.append(row)
        await asyncio.to_thread(self._rewrite_escalation_sidecar, path, leftover)
        if reconciled:
            log.info("DevAgent: reconciled %d sidecar escalation(s) into the review queue",
                     reconciled)
        return reconciled

    @staticmethod
    def _read_escalation_sidecar(path: Path) -> list[dict]:
        if not path.exists():
            return []
        rows: list[dict] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
        except Exception as exc:
            log.debug("DevAgent: escalation sidecar read failed: %s", exc)
        return rows

    @staticmethod
    def _rewrite_escalation_sidecar(path: Path, rows: list[dict]) -> None:
        try:
            if not rows:
                if path.exists():
                    path.unlink()
                return
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
            os.replace(tmp, path)
        except Exception as exc:
            log.debug("DevAgent: escalation sidecar rewrite failed: %s", exc)

    async def _run_compensations(
        self, run_id: int, triggered_by: str = "step_failure"
    ) -> int:
        """Execute pending saga compensations for run_id in reverse step order.

        Called on EVERY non-success terminal path — replan exhaustion
        (triggered_by="max_replans"), MAX_STEPS halt ("max_steps"), user/cancel
        ("user_cancel"). Each compensation is marked running → done / skipped /
        failed so the audit trail is truthful; failures are logged but never
        raise — we always attempt every pending compensation.

        Returns the number of compensations that did NOT complete cleanly
        (skipped + failed + manual-review) so the caller can surface a partial
        rollback to the user instead of silently reporting a clean unwind.
        """
        db = self._db()
        if run_id < 0 or not db or not getattr(db, "available", False):
            return 0
        compensations = await db.sagas.get_pending_compensations(run_id)
        if not compensations:
            return 0
        log.info("DevAgent: running %d saga compensation(s) for run %d (%s)",
                 len(compensations), run_id, triggered_by)
        incomplete = 0
        reverted = 0   # file changes actually undone (RESTORE_FILE / DELETE_FILE)
        manual = 0     # REVERT_TERMINAL notes that need a human
        for comp in compensations:
            cid = comp["id"]
            caction = comp.get("compensation_action", "")
            cargs = comp.get("compensation_args")
            await db.sagas.update_saga_compensation(cid, "running", triggered_by=triggered_by)
            try:
                if caction == "RESTORE_FILE" and cargs:
                    restored = await asyncio.to_thread(self._restore_file, cargs)
                    if restored is False:
                        # An overwritten file with no backup was left in place —
                        # record the truth (E5), not a misleading "done".
                        incomplete += 1
                        await db.sagas.update_saga_compensation(
                            cid, "skipped",
                            error="no backup — overwritten file left in place",
                            finished=True)
                        await self._record_escalation(
                            run_id, "Saga rollback", "compensation_failed",
                            caction, 0, incomplete=1,
                        )
                        continue
                    reverted += 1
                elif caction == "DELETE_FILE" and cargs:
                    # Legacy/back-compat (no pre-write snapshot was captured).
                    path = Path(cargs.strip())
                    if path.exists():
                        path.unlink()
                        reverted += 1
                        log.info("DevAgent: saga compensation DELETE_FILE %s", path)
                elif caction == "REVERT_TERMINAL":
                    manual += 1
                    log.warning(
                        "DevAgent: saga compensation REVERT_TERMINAL requires manual review: %r", cargs
                    )
                await db.sagas.update_saga_compensation(cid, "done", finished=True)
            except Exception as exc:
                incomplete += 1
                log.error("DevAgent: saga compensation %s failed: %s", caction, exc)
                await db.sagas.update_saga_compensation(cid, "failed", error=str(exc), finished=True)
                await self._record_escalation(
                    run_id, "Saga rollback", "compensation_failed",
                    caction, 0, incomplete=1,
                )
        if incomplete:
            log.warning("DevAgent: %d compensation(s) did not roll back cleanly for run %d",
                        incomplete, run_id)
        # Record the rollback so completion speech can announce it (R2.2). Set only
        # when compensations actually ran (empty list returns early above), so a
        # successful plan with no rollback leaves the summary None → silent.
        self._agent._rollback_summary = {
            "reverted": reverted, "manual": manual,
            "incomplete": incomplete, "triggered_by": triggered_by,
        }
        return incomplete

    @staticmethod
    def _restore_file(comp_args: str) -> bool:
        """Roll back a WRITE_FILE step from its pre-write snapshot.

        existed + backup → restore the original bytes; existed + no backup →
        leave the overwritten file in place (deleting would lose data we
        couldn't snapshot); not-existed → delete the file the plan created.

        Returns True when the rollback completed cleanly, False when it could NOT
        be completed (an overwritten file with no backup is left in place) — the
        caller records that as `skipped`, not a misleading `done` (E5).
        """
        info = json.loads(comp_args)
        path = Path(info["path"])
        if info.get("existed"):
            # Git-blob backend (DA_SAGA_GIT_BACKEND snapshots) — restore the
            # original bytes from the captured loose object. The snapshot is
            # self-describing (git_blob/git_repo ride in comp_args), so restore
            # works regardless of the current flag state.
            blob, repo = info.get("git_blob"), info.get("git_repo")
            if blob and repo:
                data = SagaManager._git_cat_blob(repo, blob)
                if data is not None:
                    path.write_bytes(data)
                    log.info("DevAgent: saga RESTORE_FILE restored %s from git blob %s",
                             path, blob[:8])
                    return True
                log.warning(
                    "DevAgent: saga RESTORE_FILE %s git blob %s unavailable — "
                    "leaving the overwritten file in place", path, blob[:8],
                )
                return False
            backup = info.get("backup")
            if backup and Path(backup).exists():
                shutil.copy2(backup, path)
                log.info("DevAgent: saga RESTORE_FILE restored %s from backup", path)
                return True
            log.warning(
                "DevAgent: saga RESTORE_FILE %s existed but no backup — "
                "leaving the overwritten file in place", path,
            )
            return False
        if path.exists():
            path.unlink()
            log.info("DevAgent: saga RESTORE_FILE deleted %s (created by plan)", path)
        return True

    async def _finalize_run(self, run_id: int, result: AgentResult, status: str) -> None:
        db = self._db()
        if run_id < 0 or not db or not getattr(db, "available", False):
            return
        try:
            await db.runs.update_agent_run(
                run_id=run_id, status=status, step_count=len(result.steps),
                success=result.success, total_latency_ms=result.total_latency_ms,
                error=result.error,
            )
            # Any compensation still 'pending' here was never triggered (the run
            # succeeded, or a path that didn't roll back). For successful runs,
            # we promote them to 'checkpoint' so VoiceRewindHandler can restore them.
            # For failed/cancelled runs (already rolled back), any leftovers are skipped.
            new_status = 'checkpoint' if result.success else 'skipped'
            promoted = await db.sagas.skip_pending_compensations(run_id, new_status=new_status)
            # Chat undo affordance (specs/chat-workbench-parity R8.2): a run that
            # persisted checkpoints can be rolled back ("undo this run"), so tell
            # the originating chat turn. No-op for non-chat runs.
            if result.success and promoted > 0:
                await self._publish_live(TOPIC_DAG_RUN_FINALIZED, {
                    "run_id": run_id, "status": status, "rewindable": True,
                })
        except Exception as exc:
            log.debug("DevAgent._finalize_run failed: %s", exc)

    async def revert_last_run(self, *, trace_id: str = "") -> bool:
        """VoiceRewindHandler: Revert the most recently finalized run.

        Promotes checkpoints back to pending, then runs compensations.

        ``trace_id`` (specs/chat-workbench-parity R8.3): set by the chat server's
        "Undo this run" control so the confirm gate surfaces as an in-chat
        approval card on the requesting socket. The gate itself is unchanged —
        _confirm_destructive_op keeps its fail-safe DENY; voice callers pass
        nothing and are byte-identical.
        """
        if trace_id:
            self._agent._active_trace_id = trace_id
        db = self._db()
        if not db or not getattr(db, "available", False):
            return False
            
        # Get the most recent run (completed or failed, but not active)
        cur = await db._conn.execute(
            "SELECT id, goal FROM agent_runs ORDER BY id DESC LIMIT 1"
        )
        row = await cur.fetchone()
        if not row:
            log.info("VoiceRewindHandler: no run found to revert")
            return False
            
        run_id = row["id"]
        goal = row["goal"]

        # Chat card context (specs/chat-workbench-parity R8.3): list the files a
        # rollback would restore, read-only — nothing is promoted until approval.
        files: list[str] = []
        try:
            for comp in await db.sagas.get_checkpoint_compensations(run_id):
                try:
                    p = json.loads(comp.get("compensation_args") or "{}").get("path")
                except Exception:
                    p = None
                if p and p not in files:
                    files.append(p)
        except Exception as exc:  # noqa: BLE001
            log.debug("VoiceRewindHandler: checkpoint listing failed: %s", exc)
        card = {"command": "restore: " + ", ".join(files[:20])} if files else None

        if not await self._confirm_destructive_op(f"Undo the run: {goal[:60]}?",
                                                  card=card):
            log.info("VoiceRewindHandler: user declined revert of run %s", run_id)
            return False
            
        log.info("VoiceRewindHandler: reverting run %d (%s)", run_id, goal)
        await db.misc.promote_checkpoints_to_pending(run_id)
        
        # We need to run compensations in a background task so we don't block voice
        from core.async_utils import fire_and_log
        fire_and_log(
            self._run_compensations(run_id, triggered_by="voice_rewind"), 
            log, 
            label=f"voice_rewind_run_{run_id}"
        )
        return True

    def __getattr__(self, name):
        agent = self.__dict__.get('_agent')
        if not agent:
            raise AttributeError(name)
        return getattr(agent, name)