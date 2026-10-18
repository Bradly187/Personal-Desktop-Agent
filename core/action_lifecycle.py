"""Typed action outcomes and bounded tracking for work that can outlive callers.

The registry owns every retained task until a terminal observation has remained
queryable for its TTL.  Caller deadlines use ``asyncio.shield`` so timing out a
wait never discards or cancels side-effecting work.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections import OrderedDict
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Awaitable, Callable


class ActionStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"


@dataclass(frozen=True)
class ActionIdentity:
    action_id: str
    action: str
    trace_id: str = ""
    run_id: int | None = None
    step_num: int | None = None

    def matches(self, other: "ActionIdentity") -> bool:
        return self == other


@dataclass(frozen=True)
class ActionOutcome:
    status: ActionStatus
    action: str
    trace_id: str = ""
    run_id: int | None = None
    step_num: int | None = None
    action_id: str = ""
    result: Any = None
    error: str | None = None
    reconciliation: str | None = None

    @property
    def identity(self) -> ActionIdentity:
        return ActionIdentity(
            action_id=self.action_id,
            action=self.action,
            trace_id=self.trace_id,
            run_id=self.run_id,
            step_num=self.step_num,
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to the existing result-dict boundary without hiding unknowns."""
        status = {
            ActionStatus.COMPLETED: "ok",
            ActionStatus.FAILED: "error",
            ActionStatus.OUTCOME_UNKNOWN: "outcome_unknown",
        }[self.status]
        data: dict[str, Any] = {
            "version": 2,
            "status": status,
            "lifecycle_status": self.status.value,
            "action": self.action,
            "action_id": self.action_id,
            "trace_id": self.trace_id,
            "run_id": self.run_id,
            "step_num": self.step_num,
        }
        if self.result is not None:
            data["result"] = self.result
        if self.error is not None:
            data["error"] = self.error
        if self.reconciliation is not None:
            data["reconciliation"] = self.reconciliation
        return data

    @classmethod
    def from_dict(
        cls, data: dict[str, Any], *, identity: ActionIdentity | None = None
    ) -> "ActionOutcome":
        raw = data.get("lifecycle_status")
        if raw is None:
            raw = {
                "ok": ActionStatus.COMPLETED.value,
                "error": ActionStatus.FAILED.value,
                "outcome_unknown": ActionStatus.OUTCOME_UNKNOWN.value,
            }.get(str(data.get("status", "error")), ActionStatus.FAILED.value)
        ident = identity or ActionIdentity(
            action_id=str(data.get("action_id", "")),
            action=str(data.get("action", "")),
            trace_id=str(data.get("trace_id", "")),
            run_id=data.get("run_id"),
            step_num=data.get("step_num"),
        )
        return cls(
            status=ActionStatus(raw),
            action=ident.action,
            trace_id=ident.trace_id,
            run_id=ident.run_id,
            step_num=ident.step_num,
            action_id=ident.action_id,
            result=data.get("result"),
            error=data.get("error"),
            reconciliation=data.get("reconciliation"),
        )


class RegistryFullError(RuntimeError):
    """The bounded registry cannot safely retain another action."""


class ActionIdentityMismatch(PermissionError):
    """An action id was presented with a different signed identity."""


Reconciler = Callable[[ActionOutcome], str | None | Awaitable[str | None]]
WorkFactory = Callable[[], Awaitable[Any]]


@dataclass
class _TrackedWork:
    identity: ActionIdentity
    task: asyncio.Task[Any]
    destructive: bool
    created_at: float
    expires_at: float
    reconciler: Reconciler | None = None
    outcome: ActionOutcome | None = None
    timed_out: bool = False


class TrackedWorkRegistry:
    """Bounded, TTL-retained action registry with idempotent action ids."""

    def __init__(self, *, limit: int = 32, ttl_s: float = 300.0) -> None:
        if limit < 1 or ttl_s <= 0:
            raise ValueError("registry limit and ttl must be positive")
        self.limit = limit
        self.ttl_s = ttl_s
        self._entries: OrderedDict[str, _TrackedWork] = OrderedDict()
        self._lock = asyncio.Lock()

    def __len__(self) -> int:
        return len(self._entries)

    def _purge_expired_locked(self, now: float) -> None:
        for action_id, entry in list(self._entries.items()):
            # Never discard a live handle. An over-TTL live action deliberately
            # occupies capacity so new side effects fail closed.
            if entry.task.done() and entry.outcome is not None and entry.expires_at <= now:
                self._entries.pop(action_id, None)

    async def start_or_get(
        self,
        identity: ActionIdentity,
        work_factory: WorkFactory,
        *,
        destructive: bool,
        reconciler: Reconciler | None = None,
    ) -> tuple[_TrackedWork, bool]:
        if not identity.action_id:
            raise ValueError("action_id is required")
        async with self._lock:
            now = time.monotonic()
            self._purge_expired_locked(now)
            existing = self._entries.get(identity.action_id)
            if existing is not None:
                if not existing.identity.matches(identity):
                    raise ActionIdentityMismatch("action identity does not match action_id")
                self._entries.move_to_end(identity.action_id)
                return existing, False
            if len(self._entries) >= self.limit:
                raise RegistryFullError("tracked-work registry is full")
            # Capacity is reserved before invoking the factory. create_task does
            # not run user code until control returns to the event loop.
            task = asyncio.create_task(work_factory(), name=f"action:{identity.action_id}")
            entry = _TrackedWork(
                identity=identity,
                task=task,
                destructive=destructive,
                created_at=now,
                expires_at=now + self.ttl_s,
                reconciler=reconciler,
            )
            self._entries[identity.action_id] = entry
            task.add_done_callback(
                lambda _task, aid=identity.action_id: asyncio.create_task(self._finish(aid))
            )
            return entry, True

    @staticmethod
    def _exception_outcome(identity: ActionIdentity, exc: BaseException) -> ActionOutcome:
        verified = getattr(exc, "termination_verified", None)
        status = (
            ActionStatus.OUTCOME_UNKNOWN
            if verified is False
            else ActionStatus.FAILED
        )
        reconciliation = getattr(exc, "termination_detail", None)
        error = str(exc) or (
            "worker timeout" if isinstance(exc, TimeoutError) else type(exc).__name__
        )
        return ActionOutcome(
            status=status,
            action=identity.action,
            trace_id=identity.trace_id,
            run_id=identity.run_id,
            step_num=identity.step_num,
            action_id=identity.action_id,
            error=error,
            reconciliation=reconciliation,
        )

    @staticmethod
    def _normalize(identity: ActionIdentity, value: Any) -> ActionOutcome:
        if isinstance(value, ActionOutcome):
            if value.identity.matches(identity):
                return value
            return replace(
                value,
                action=identity.action,
                trace_id=identity.trace_id,
                run_id=identity.run_id,
                step_num=identity.step_num,
                action_id=identity.action_id,
            )
        if isinstance(value, dict) and (
            "lifecycle_status" in value or value.get("status") == "outcome_unknown"
        ):
            return ActionOutcome.from_dict(value, identity=identity)
        return ActionOutcome(
            status=ActionStatus.COMPLETED,
            action=identity.action,
            trace_id=identity.trace_id,
            run_id=identity.run_id,
            step_num=identity.step_num,
            action_id=identity.action_id,
            result=value,
        )

    async def _observe_task(self, entry: _TrackedWork) -> ActionOutcome:
        try:
            value = entry.task.result()
            outcome = self._normalize(entry.identity, value)
        except asyncio.CancelledError:
            outcome = ActionOutcome(
                status=ActionStatus.OUTCOME_UNKNOWN,
                action=entry.identity.action,
                trace_id=entry.identity.trace_id,
                run_id=entry.identity.run_id,
                step_num=entry.identity.step_num,
                action_id=entry.identity.action_id,
                error="worker was cancelled without conclusive outcome",
            )
        except BaseException as exc:
            outcome = self._exception_outcome(entry.identity, exc)
        if entry.reconciler is not None and entry.timed_out:
            try:
                observed = entry.reconciler(outcome)
                if inspect.isawaitable(observed):
                    observed = await observed
                if observed:
                    outcome = replace(outcome, reconciliation=str(observed))
            except Exception as exc:
                # Reconciliation is supporting evidence; its own failure cannot
                # turn an unknown action into a proven failure.
                if outcome.status is ActionStatus.OUTCOME_UNKNOWN:
                    outcome = replace(outcome, reconciliation=f"evidence unavailable: {exc}")
        return outcome

    async def _finish(self, action_id: str) -> None:
        async with self._lock:
            entry = self._entries.get(action_id)
        if entry is None or entry.outcome is not None or not entry.task.done():
            return
        outcome = await self._observe_task(entry)
        async with self._lock:
            current = self._entries.get(action_id)
            if current is entry and current.outcome is None:
                current.outcome = outcome
                current.expires_at = time.monotonic() + self.ttl_s

    @staticmethod
    def _unknown(identity: ActionIdentity, timeout_s: float | None) -> ActionOutcome:
        suffix = "" if timeout_s is None else f" after {timeout_s:g}s"
        return ActionOutcome(
            status=ActionStatus.OUTCOME_UNKNOWN,
            action=identity.action,
            trace_id=identity.trace_id,
            run_id=identity.run_id,
            step_num=identity.step_num,
            action_id=identity.action_id,
            error=f"caller wait budget elapsed{suffix}; worker outcome is unknown",
        )

    async def await_entry(
        self, entry: _TrackedWork, *, timeout_s: float | None
    ) -> ActionOutcome:
        if entry.outcome is not None:
            return entry.outcome
        try:
            if timeout_s is None:
                await asyncio.shield(entry.task)
            else:
                await asyncio.wait_for(asyncio.shield(entry.task), timeout=timeout_s)
        except asyncio.TimeoutError:
            if not entry.task.done():
                entry.timed_out = True
                return self._unknown(entry.identity, timeout_s)
        except asyncio.CancelledError:
            raise
        except BaseException:
            # A worker exception is converted into a typed outcome below.
            pass
        await self._finish(entry.identity.action_id)
        return entry.outcome or self._unknown(entry.identity, timeout_s)

    async def execute(
        self,
        identity: ActionIdentity,
        work_factory: WorkFactory,
        *,
        timeout_s: float,
        destructive: bool,
        reconciliation_grace_s: float = 0.0,
        reconciler: Reconciler | None = None,
    ) -> ActionOutcome:
        entry, _started = await self.start_or_get(
            identity, work_factory, destructive=destructive, reconciler=reconciler
        )
        outcome = await self.await_entry(entry, timeout_s=timeout_s)
        if (
            outcome.status is ActionStatus.OUTCOME_UNKNOWN
            and reconciliation_grace_s > 0
            and not entry.task.done()
        ):
            outcome = await self.await_entry(entry, timeout_s=reconciliation_grace_s)
        return outcome

    async def query(self, identity: ActionIdentity) -> ActionOutcome | None:
        async with self._lock:
            self._purge_expired_locked(time.monotonic())
            entry = self._entries.get(identity.action_id)
            if entry is None:
                return None
            if not entry.identity.matches(identity):
                raise ActionIdentityMismatch("action identity does not match action_id")
        if entry.task.done() and entry.outcome is None:
            await self._finish(identity.action_id)
        return entry.outcome or self._unknown(identity, None)

    async def snapshot(self) -> list[ActionOutcome]:
        async with self._lock:
            entries = list(self._entries.values())
        results: list[ActionOutcome] = []
        for entry in entries:
            if entry.task.done() and entry.outcome is None:
                await self._finish(entry.identity.action_id)
            results.append(entry.outcome or self._unknown(entry.identity, None))
        return results
