"""Priority-aware task scheduler for the accessibility pipeline.

Priority tiers (lower int = higher priority):
    0  ACCESSIBILITY  — touch, voice click (bypass sources)
    1  VOICE          — WhisperStream transcription commands
    2  GESTURE        — MediaPipe gesture commands
    3  DEV_AGENT      — DevAgent plan/execute chains
    4  BACKGROUND     — CodebaseIndexer re-indexing, ContinuousTrainer adaptation

What actually provides isolation (read before changing dispatch):
    The single worker dequeues each item and immediately dispatches it to its
    own asyncio.create_task — it never blocks between items. So the PRIORITY
    ORDER only changes anything when the queue is genuinely backed up (several
    items waiting at once); under normal load items are dispatched as fast as
    they arrive and priority is effectively moot. The real, always-on protection
    is the DEV semaphore:

    - ACCESSIBILITY / VOICE / GESTURE still dispatch concurrently (no execution
      semaphore). Source-aware admission bounds total retained work before task
      creation; VOICE/GESTURE cannot consume the explicit accessibility reserve.
    - At most _MAX_CONCURRENT_DEV (=1) DEV_AGENT/BACKGROUND task executes at once,
      via `_dev_sem`. THIS is what stops a long DevAgent/RAG job from starving
      accessibility during a flare. Priority is a secondary tie-breaker on top.
    - The FusionEngine 60 Hz tick loop is never touched.

Resource invariants (the one finite resource is `_dev_sem`, a single permit):
    1. Request → use → release. `_run_dev_task` holds the permit via
       `async with self._dev_sem`, so it is released on success, timeout,
       exception, AND cancellation. The `_dev_inflight` gauge mirrors the permit
       and is decremented in a `finally`, for the same guarantee.
    2. SINGLE-PERMIT RE-ENTRANCY (deadlock trap): a coroutine running on a
       DEV_AGENT/BACKGROUND tier (already holding the only permit) MUST NOT
       submit()-and-await another DEV_AGENT/BACKGROUND task on this scheduler —
       the parent holds the permit, the child waits for it forever ⇒ deadlock.
       Fan out instead via a fast/ungated tier or `asyncio.gather` on plain
       coroutines (see the multi-agent note below).
    3. `submit_plan` holds the permit for up to _PLAN_TASK_TIMEOUT_S (300 s) — the
       primary deadlock vector under invariant 2. It currently has NO call sites;
       keep it that way unless the caller provably cannot re-enter.
    4. stop() cancels in-flight dispatched tasks so no permit/task is held with
       no one left to need it.

Multi-agent fan-out (IMPLEMENTED — see fan_out()): to run sub-steps concurrently
    from within a dev task, call `fan_out(coros)`. It gathers the children under
    a SEPARATE sub-task semaphore (`_subagent_sem`, N>1 permits) that is distinct
    from `_dev_sem`, so children never contend for the permit the parent is
    holding (invariant 2). Only fan out genuinely independent (ideally read-only)
    sub-steps — ordering/dependencies are the caller's responsibility.

Design notes:
    - Uses asyncio.PriorityQueue (stdlib, no new deps).
    - Queue items retain priority/FIFO fields plus coroutine, outward Future,
      timeout/trace data, and private lifecycle metadata.
    - DEV_AGENT/BACKGROUND tasks run under asyncio.wait_for(timeout) so a hung
      specialist never holds the permit indefinitely.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from enum import IntEnum
from types import MappingProxyType
from typing import Any, Coroutine, Hashable, Mapping

log = logging.getLogger(__name__)

_MAX_CONCURRENT_DEV = 1    # only one heavy dev/background task at a time
_DEV_TASK_TIMEOUT_S = 30   # ceiling for single specialist-model inferences
_PLAN_TASK_TIMEOUT_S = 300 # ceiling for full DevAgent plan_and_run() (5 min)

# Sub-agent fan-out (gap #1): how many independent sub-steps of ONE dev plan may
# run concurrently. This permit pool is DISTINCT from `_dev_sem` — that is what
# makes fan-out deadlock-free (scheduler invariant 2): the parent dev task holds
# the single `_dev_sem` permit while its children take `_subagent_sem` permits,
# so children never contend for the permit the parent is holding. Kept modest so
# parallel read-heavy sub-steps don't saturate the box during accessibility use.
_MAX_CONCURRENT_SUBAGENTS = 3


class Priority(IntEnum):
    ACCESSIBILITY = 0
    VOICE = 1
    GESTURE = 2
    DEV_AGENT = 3
    BACKGROUND = 4


@dataclass(frozen=True)
class SchedulerConfig:
    """Constructor-injected process admission limits.

    Defaults are documented in the Phase 3 design evidence table.  Tests inject
    deliberately tiny limits so admission behavior never depends on timing.
    """

    hard_limit: int = 256
    accessibility_reserve: int = 16
    non_accessibility_limit: int = 21
    dev_agent_limit: int = 16
    background_limit: int = 10
    max_parked_dev: int = 16
    voice_stale_after_s: float = 2.5
    gesture_stale_after_s: float = 0.8

    def __post_init__(self) -> None:
        if self.hard_limit <= 0:
            raise ValueError("hard_limit must be positive")
        if not 0 < self.accessibility_reserve < self.hard_limit:
            raise ValueError("accessibility_reserve must be between 1 and hard_limit - 1")
        non_reserved = self.hard_limit - self.accessibility_reserve
        if not 0 < self.non_accessibility_limit <= non_reserved:
            raise ValueError("non_accessibility_limit must fit below the reserve")
        if not 0 <= self.background_limit <= self.dev_agent_limit:
            raise ValueError("background_limit must not exceed dev_agent_limit")
        if not 0 < self.dev_agent_limit <= self.non_accessibility_limit:
            raise ValueError("dev_agent_limit must not exceed non_accessibility_limit")
        if not 0 < self.max_parked_dev <= self.dev_agent_limit:
            raise ValueError("max_parked_dev must not exceed dev_agent_limit")
        if self.voice_stale_after_s < 0 or self.gesture_stale_after_s < 0:
            raise ValueError("stale windows must be non-negative")


@dataclass(frozen=True)
class AdmissionDecision:
    accepted: bool
    reason: str | None
    coalesced: bool = False


_ADMISSION_REASONS = (
    "denied_background_limit",
    "denied_dev_agent_limit",
    "denied_accessibility_reserve",
    "denied_hard_limit",
    "denied_parked_limit",
    "coalesced_exact_key",
    "stale_before_dispatch",
)


def _zero_reason_counts() -> dict[str, int]:
    return {reason: 0 for reason in _ADMISSION_REASONS}


def decide_admission(
    config: SchedulerConfig,
    counts: Mapping[Priority, int],
    priority: Priority,
) -> AdmissionDecision:
    """Pure O(1) admission policy shared with Fusion's degraded fallback."""
    total = sum(counts.values())
    if total >= config.hard_limit:
        return AdmissionDecision(False, "denied_hard_limit")
    heavy = counts[Priority.DEV_AGENT] + counts[Priority.BACKGROUND]
    if priority is Priority.BACKGROUND and heavy >= config.background_limit:
        return AdmissionDecision(False, "denied_background_limit")
    if priority is Priority.DEV_AGENT and heavy >= config.dev_agent_limit:
        return AdmissionDecision(False, "denied_dev_agent_limit")
    if priority is not Priority.ACCESSIBILITY:
        non_accessibility = total - counts[Priority.ACCESSIBILITY]
        limit = min(
            config.non_accessibility_limit,
            config.hard_limit - config.accessibility_reserve,
        )
        if non_accessibility >= limit:
            return AdmissionDecision(False, "denied_accessibility_reserve")
    return AdmissionDecision(True, None)


@dataclass(frozen=True)
class SchedulerLoadSnapshot:
    """Immutable, allocation-free-at-read view of scheduler-owned work."""

    queued_total: int
    running_total: int
    queued_by_priority: Mapping[Priority, int]
    running_by_priority: Mapping[Priority, int]
    parked_dev: int
    shed_total: int
    denied_total: int
    coalesced_total: int
    stale_total: int
    reason_counts: Mapping[str, int]


@dataclass
class _TaskMetadata:
    """Internal lifecycle record retained from enqueue through terminal state."""

    priority: Priority
    label: str
    source: str
    enqueue_ts: float
    trace_id: str
    stale_deadline: float | None = None
    coalesce_registry_key: tuple[Priority, str, Hashable] | None = None
    primary_future: asyncio.Future | None = None
    state: str = "new"
    terminal: bool = False
    coro_started: bool = False


def _zero_priority_counts() -> dict[Priority, int]:
    return {priority: 0 for priority in Priority}


class AccessibilityScheduler:
    """asyncio-native priority dispatcher over HybridCoordinator.route() coroutines.

    Instantiated once in main.py and injected into FusionEngine via
    set_scheduler().  FusionEngine calls submit() from _emit(); the scheduler's
    worker coroutine dequeues and runs them in priority order.

    Wire-up:
        scheduler = AccessibilityScheduler()
        await scheduler.start()
        fusion.set_scheduler(scheduler)
        dev_agent.set_scheduler(scheduler)
        # ... pipeline runs ...
        await scheduler.stop()
    """

    def __init__(self, config: SchedulerConfig | None = None) -> None:
        self.config = config or SchedulerConfig()
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._seq: int = 0
        self._worker_task: asyncio.Task | None = None
        self._running = False
        # Submission is accepted before the first start and after any explicit
        # start(), but never once deliberate shutdown begins.
        self._accepting_submissions = True
        self._dev_sem = asyncio.Semaphore(_MAX_CONCURRENT_DEV)
        self._subagent_sem = asyncio.Semaphore(_MAX_CONCURRENT_SUBAGENTS)  # gap #1 fan-out
        self._metrics = None                              # set via set_metrics()
        self._dev_inflight: int = 0                       # DEV/BACKGROUND tasks holding the permit
        self._subagent_inflight: int = 0                  # sub-agent fan-out tasks in flight
        self._shed_count: int = 0
        self._denied_count: int = 0
        self._coalesced_count: int = 0
        self._stale_count: int = 0
        self._reason_counts = _zero_reason_counts()
        self._active_coalesced: dict[
            tuple[Priority, str, Hashable], asyncio.Future
        ] = {}
        self._dev_parked: int = 0                          # dev tasks parked at the flare gate
        self._inflight_tasks: set[asyncio.Task] = set()   # dispatched tasks, for stop() cleanup
        self._queued_by_priority = _zero_priority_counts()
        self._running_by_priority = _zero_priority_counts()
        self._load_snapshot = self._build_load_snapshot()
        # Flare admission gate (gap #3): set == dev work admitted; cleared == paused.
        self._dev_resume_event = asyncio.Event()
        self._dev_resume_event.set()
        self._dev_paused: bool = False

    def set_metrics(self, metrics) -> None:
        """Wire the Metrics singleton for scheduler load visibility."""
        self._metrics = metrics
        register_provider = getattr(metrics, "register_gauge_provider", None)
        if callable(register_provider):
            try:
                register_provider("accessibility_scheduler", self._gauge_values)
            except Exception:
                pass
        self._publish_gauges()

    def _build_load_snapshot(self) -> SchedulerLoadSnapshot:
        """Build the next immutable snapshot after an accounting mutation."""
        queued = dict(self._queued_by_priority)
        running = dict(self._running_by_priority)
        return SchedulerLoadSnapshot(
            queued_total=sum(queued.values()),
            running_total=sum(running.values()),
            queued_by_priority=MappingProxyType(queued),
            running_by_priority=MappingProxyType(running),
            parked_dev=self._dev_parked,
            shed_total=self._shed_count,
            denied_total=self._denied_count,
            coalesced_total=self._coalesced_count,
            stale_total=self._stale_count,
            reason_counts=MappingProxyType(dict(self._reason_counts)),
        )

    def _refresh_load_snapshot(self) -> None:
        self._load_snapshot = self._build_load_snapshot()
        self._publish_gauges()

    def load_snapshot(self) -> SchedulerLoadSnapshot:
        """Return the current prebuilt snapshot without awaiting or doing I/O."""
        return self._load_snapshot

    def _mark_queued(self, metadata: _TaskMetadata) -> None:
        metadata.state = "queued"
        self._queued_by_priority[metadata.priority] += 1
        self._refresh_load_snapshot()

    def _mark_running(self, metadata: _TaskMetadata) -> None:
        if metadata.terminal or metadata.state != "queued":
            return
        self._queued_by_priority[metadata.priority] -= 1
        self._running_by_priority[metadata.priority] += 1
        metadata.state = "running"
        self._refresh_load_snapshot()

    def _mark_terminal(self, metadata: _TaskMetadata) -> None:
        """Finalize one item exactly once from either queued or running state."""
        if metadata.terminal:
            return
        if metadata.state == "queued":
            self._queued_by_priority[metadata.priority] -= 1
        elif metadata.state == "running":
            self._running_by_priority[metadata.priority] -= 1
        key = metadata.coalesce_registry_key
        if key is not None and self._active_coalesced.get(key) is metadata.primary_future:
            self._active_coalesced.pop(key, None)
        metadata.state = "terminal"
        metadata.terminal = True
        self._refresh_load_snapshot()

    def _record_reason(self, reason: str, *, denied: bool = False,
                       stale: bool = False) -> None:
        self._reason_counts[reason] += 1
        if denied:
            self._denied_count += 1
            self._shed_count += 1
        if stale:
            self._stale_count += 1
            self._shed_count += 1
        self._refresh_load_snapshot()

    @staticmethod
    def _observe_future_exception(future: asyncio.Future) -> None:
        """Retrieve a terminal exception once; awaiting callers remain unaffected.

        Execution, timeout, shedding, and dispatch paths are the sole reporters;
        this callback only marks ignored outward-Future exceptions as observed.
        """
        if future.cancelled():
            return
        try:
            future.exception()
        except (asyncio.CancelledError, Exception):
            pass

    def _new_future(self) -> asyncio.Future:
        future = asyncio.get_running_loop().create_future()
        future.add_done_callback(self._observe_future_exception)
        return future

    def _on_dispatch_done(
        self,
        task: asyncio.Task,
        metadata: _TaskMetadata,
        future: asyncio.Future,
        coro: Coroutine[Any, Any, Any],
    ) -> None:
        """Release retained dispatch state, including cancellation-before-start."""
        self._inflight_tasks.discard(task)
        if task.cancelled():
            if not metadata.coro_started:
                try:
                    coro.close()
                except Exception:
                    pass
            if not future.done():
                future.cancel()
        else:
            try:
                task.exception()
            except (asyncio.CancelledError, Exception):
                pass
        self._mark_terminal(metadata)

    def _gauge_values(self) -> dict[str, float]:
        """Return current scheduler gauges from the immutable load snapshot."""
        snapshot = self.load_snapshot()
        values = {
            "scheduler_queue_depth": snapshot.queued_total,
            "scheduler_dev_inflight": self._dev_inflight,
            "scheduler_dev_parked": snapshot.parked_dev,
            "scheduler_subagent_inflight": self._subagent_inflight,
            "scheduler_shed_total": snapshot.shed_total,
            "scheduler_denied_total": snapshot.denied_total,
            "scheduler_coalesced_total": snapshot.coalesced_total,
            "scheduler_stale_total": snapshot.stale_total,
            "scheduler_queued_total": snapshot.queued_total,
            "scheduler_running_total": snapshot.running_total,
        }
        for reason, count in snapshot.reason_counts.items():
            values[f"scheduler_reason_{reason}"] = count
        for priority in Priority:
            suffix = priority.name.lower()
            values[f"scheduler_queued_{suffix}"] = snapshot.queued_by_priority[priority]
            values[f"scheduler_running_{suffix}"] = snapshot.running_by_priority[priority]
        return values

    def _publish_gauges(self) -> None:
        """Publish one coherent scheduler snapshot without waiting on metrics."""
        if self._metrics is None:
            return
        try:
            self._metrics.set_many(self._gauge_values(), blocking=False)
        except Exception:
            pass

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._running:
            log.warning("AccessibilityScheduler.start() called while already running — ignored")
            return
        self._accepting_submissions = True
        self._running = True
        self._worker_task = asyncio.create_task(
            self._worker(), name="scheduler_worker"
        )
        log.info("AccessibilityScheduler started (max_concurrent_dev=%d)", _MAX_CONCURRENT_DEV)

    async def stop(self) -> None:
        # Close admission before the first await so concurrent producers either
        # transferred ownership before shutdown or are rejected terminally.
        self._accepting_submissions = False
        self._running = False
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        self._worker_task = None

        # Drain work that was never dispatched. Closing the coroutine and
        # cancelling its outward Future makes shutdown terminal for every item.
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            (_priority_val, _seq, coro, future, _label,
             _timeout_s, _trace_id, _enqueue_ts, metadata) = item
            try:
                coro.close()
            except Exception:
                pass
            if not future.done():
                future.cancel()
            self._mark_terminal(metadata)
            self._queue.task_done()

        # Cancel any dispatched tasks still in flight so no task (and no held
        # _dev_sem permit) lingers past shutdown — resource invariant 4.
        inflight = list(self._inflight_tasks)
        for task in inflight:
            task.cancel()
        for task in inflight:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        # Let registered done callbacks release cancellation-before-start records.
        await asyncio.sleep(0)
        self._inflight_tasks.clear()
        self._publish_gauges()
        log.info("AccessibilityScheduler stopped")

    # ── Public submit API ──────────────────────────────────────────────────────

    def _reject_if_not_accepting(
        self,
        coro: Coroutine[Any, Any, Any],
        future: asyncio.Future,
    ) -> bool:
        """Terminally reject ownership transfers after deliberate shutdown."""
        if self._accepting_submissions:
            return False
        try:
            coro.close()
        except Exception:
            pass
        if not future.done():
            future.cancel()
        return True

    def admission_decision(self, priority: Priority) -> AdmissionDecision:
        """Return the current source-aware decision without mutating state."""
        snapshot = self.load_snapshot()
        counts = {
            item_priority: (
                snapshot.queued_by_priority[item_priority]
                + snapshot.running_by_priority[item_priority]
            )
            for item_priority in Priority
        }
        return decide_admission(self.config, counts, priority)

    @staticmethod
    def _registry_key(
        priority: Priority,
        source: str,
        coalesce_key: Hashable | None,
    ) -> tuple[Priority, str, Hashable] | None:
        if priority not in (Priority.VOICE, Priority.GESTURE):
            return None
        if not source or coalesce_key is None:
            return None
        try:
            hash(coalesce_key)
        except TypeError:
            return None
        return (priority, source, coalesce_key)

    def _proxy_primary(
        self, primary: asyncio.Future, proxy: asyncio.Future
    ) -> asyncio.Future:
        """Wire an independently cancellable follower to an exact primary."""

        def _copy_result(done: asyncio.Future) -> None:
            if proxy.done():
                return
            if done.cancelled():
                proxy.cancel()
                return
            try:
                exc = done.exception()
            except asyncio.CancelledError:
                proxy.cancel()
                return
            if exc is not None:
                proxy.set_exception(exc)
            else:
                proxy.set_result(done.result())

        primary.add_done_callback(_copy_result)
        return proxy

    def _submit_owned(
        self,
        coro: Coroutine[Any, Any, Any],
        priority: Priority,
        label: str,
        trace_id: str,
        timeout_s: float,
        *,
        source: str = "",
        coalesce_key: Hashable | None = None,
        stale_after_s: float | None = None,
    ) -> asyncio.Future:
        future = self._new_future()
        if self._reject_if_not_accepting(coro, future):
            return future

        registry_key = self._registry_key(priority, source, coalesce_key)
        if registry_key is not None:
            primary = self._active_coalesced.get(registry_key)
            if primary is not None:
                try:
                    coro.close()
                except Exception:
                    pass
                self._coalesced_count += 1
                self._reason_counts["coalesced_exact_key"] += 1
                self._refresh_load_snapshot()
                return self._proxy_primary(primary, future)

        decision = self.admission_decision(priority)
        if not decision.accepted:
            try:
                coro.close()
            except Exception:
                pass
            self._record_reason(decision.reason or "denied_hard_limit", denied=True)
            message = (
                f"scheduler admission denied {priority.name} task {label!r}: "
                f"{decision.reason}"
            )
            log.warning("AccessibilityScheduler: %s", message)
            future.set_exception(asyncio.QueueFull(message))
            return future

        if stale_after_s is not None and stale_after_s < 0:
            try:
                coro.close()
            except Exception:
                pass
            future.set_exception(ValueError("stale_after_s must be non-negative"))
            return future

        self._seq += 1
        enqueue_ts = time.monotonic()
        stale_deadline = None
        if priority in (Priority.VOICE, Priority.GESTURE) and stale_after_s is not None:
            stale_deadline = enqueue_ts + stale_after_s
        metadata = _TaskMetadata(
            priority=priority,
            label=label,
            source=source,
            enqueue_ts=enqueue_ts,
            trace_id=trace_id,
            stale_deadline=stale_deadline,
            coalesce_registry_key=registry_key,
            primary_future=future,
        )
        if registry_key is not None:
            self._active_coalesced[registry_key] = future
        self._queue.put_nowait(
            (priority.value, self._seq, coro, future, label,
             timeout_s, trace_id, enqueue_ts, metadata)
        )
        self._mark_queued(metadata)
        return future

    def submit_plan(
        self,
        coro: Coroutine[Any, Any, Any],
        label: str = "",
        trace_id: str = "",
    ) -> asyncio.Future:
        """Schedule a full DevAgent plan at DEV_AGENT priority with the 5-min ceiling.

        The submitted coroutine must not submit-and-await another heavy task while
        it holds the scheduler's single heavy permit.
        """
        return self._submit_owned(
            coro,
            Priority.DEV_AGENT,
            label,
            trace_id,
            _PLAN_TASK_TIMEOUT_S,
            source="dev_agent",
        )

    def submit(
        self,
        coro: Coroutine[Any, Any, Any],
        priority: Priority,
        label: str = "",
        trace_id: str = "",
        *,
        source: str = "",
        coalesce_key: Hashable | None = None,
        stale_after_s: float | None = None,
    ) -> asyncio.Future:
        """Schedule work, optionally with explicit exact-key/staleness metadata.

        Existing callers remain compatible.  Coalescing is available only when a
        VOICE/GESTURE caller explicitly supplies both ``source`` and a hashable
        ``coalesce_key``.
        """
        return self._submit_owned(
            coro,
            priority,
            label,
            trace_id,
            _DEV_TASK_TIMEOUT_S,
            source=source,
            coalesce_key=coalesce_key,
            stale_after_s=stale_after_s,
        )

    # ── Sub-agent fan-out (gap #1) ──────────────────────────────────────────────

    async def fan_out(
        self,
        coros: list[Coroutine[Any, Any, Any]],
        label: str = "",
        timeout_s: float = _DEV_TASK_TIMEOUT_S,
        return_exceptions: bool = True,
        trace_id: str = "",
    ) -> list[Any]:
        """Run independent sub-step coroutines concurrently and await all of them.

        Bypasses the priority queue: the CALLER (a dev task already holding the
        single `_dev_sem` permit) awaits the batch directly. Children run under
        `_subagent_sem` (N>1), DISTINCT from `_dev_sem`, so they never contend for
        the parent's permit — this is what keeps fan-out deadlock-free (invariant
        2). Accessibility / voice / gesture remain ungated and unaffected.

        Each child is wrapped in its own `timeout_s` guard so one hung sub-step
        can't stall the batch. Results are returned in input order. With
        `return_exceptions=True` (default) a failed/timed-out child yields its
        exception object instead of aborting the batch.

        IMPORTANT: only fan out steps that are genuinely INDEPENDENT (ideally
        read-only / idempotent). Ordering and inter-step dependencies are the
        caller's responsibility — this method does not serialise anything.
        """
        if not coros:
            return []

        async def _run_one(coro: Coroutine[Any, Any, Any]) -> Any:
            async with self._subagent_sem:
                self._subagent_inflight += 1
                self._publish_gauges()
                try:
                    return await asyncio.wait_for(coro, timeout=timeout_s)
                finally:
                    self._subagent_inflight -= 1
                    self._publish_gauges()

        log.info(
            "Scheduler.fan_out: %d sub-task(s) %r (max_parallel=%d, timeout=%.0fs)",
            len(coros), label, _MAX_CONCURRENT_SUBAGENTS, timeout_s,
        )
        # Gap C: mark the fan-out boundary in the run trace. The children run as
        # gather-spawned tasks that copy the current ContextVar trace, so their
        # own spans attach to the same run tree automatically.
        from monitoring.trace import get_tracer
        _t0 = time.monotonic()
        try:
            results = await asyncio.gather(
                *[_run_one(c) for c in coros], return_exceptions=return_exceptions
            )
        finally:
            try:
                get_tracer().record_span(
                    "fan_out", trace_id=trace_id or None,
                    dur_ms=(time.monotonic() - _t0) * 1000,
                    count=len(coros), label=label,
                )
            except Exception:
                pass
        return results

    # ── Worker ────────────────────────────────────────────────────────────────

    async def _worker(self) -> None:
        """Single consumer: dequeue and run coroutines in priority order.

        ACCESSIBILITY / VOICE / GESTURE tasks bypass the semaphore and run
        fully concurrently (matching prior asyncio.create_task behaviour).

        DEV_AGENT / BACKGROUND tasks are gated by _dev_sem (max 1 concurrent)
        and wrapped in a 30-second timeout so a hung specialist never locks the
        semaphore permanently.
        """
        while self._running:
            try:
                item = await self._queue.get()
            except asyncio.CancelledError:
                break
            (priority_val, _seq, coro, future, label,
             timeout_s, trace_id, enqueue_ts, metadata) = item
            dispatch_coro = None
            try:
                priority = Priority(priority_val)
                if (
                    metadata.stale_deadline is not None
                    and time.monotonic() >= metadata.stale_deadline
                ):
                    try:
                        coro.close()
                    except Exception:
                        pass
                    self._record_reason("stale_before_dispatch", stale=True)
                    if not future.done():
                        future.set_exception(asyncio.TimeoutError(
                            f"stale {priority.name} task {label!r} was not executed"
                        ))
                    self._mark_terminal(metadata)
                    continue
                # A dequeued item remains accounted as running while waiting at
                # the flare gate or semaphore, and through actual execution.
                self._mark_running(metadata)

                # Cross-layer trace: record the queue-wait as a `dispatch` span.
                if trace_id:
                    from monitoring.trace import get_tracer
                    _t = get_tracer()
                    if _t.enabled:
                        _t.record_span(
                            "dispatch", trace_id=trace_id,
                            dur_ms=(time.monotonic() - enqueue_ts) * 1000,
                            priority=priority.name,
                        )

                if priority >= Priority.DEV_AGENT:
                    # Heavy task — acquire semaphore, launch with timeout
                    dispatch_coro = self._run_dev_task(
                        coro, future, label, timeout_s, metadata,
                    )
                    task = asyncio.create_task(
                        dispatch_coro,
                        name=f"sched_dev_{label}",
                    )
                else:
                    # Accessibility / voice / gesture — run concurrently, no gate
                    dispatch_coro = self._run_task(coro, future, label, metadata)
                    task = asyncio.create_task(
                        dispatch_coro,
                        name=f"sched_acc_{label}",
                    )

                # Track dispatched tasks so stop() can cancel any still in flight.
                self._inflight_tasks.add(task)
                task.add_done_callback(
                    lambda done, m=metadata, f=future, c=coro:
                    self._on_dispatch_done(done, m, f, c)
                )

            except asyncio.CancelledError:
                # Resolve + close the in-hand item so its awaiter never hangs and
                # the coroutine isn't leaked (#21), then exit.
                if not future.done():
                    future.set_exception(asyncio.CancelledError())
                if dispatch_coro is not None:
                    dispatch_coro.close()
                try:
                    coro.close()
                except Exception:
                    pass
                self._mark_terminal(metadata)
                break
            except Exception as exc:
                # Dispatch failed AFTER dequeue (e.g. create_task on a closing
                # loop). Without this the future never resolves and the coro
                # leaks "never awaited" (#21).
                log.warning("AccessibilityScheduler._worker dispatch error: %s", exc)
                if not future.done():
                    future.set_exception(exc)
                if dispatch_coro is not None:
                    dispatch_coro.close()
                try:
                    coro.close()
                except Exception:
                    pass
                self._mark_terminal(metadata)
            finally:
                self._queue.task_done()

    async def _run_task(
        self,
        coro: Coroutine[Any, Any, Any],
        future: asyncio.Future,
        label: str,
        metadata: _TaskMetadata,
    ) -> None:
        """Run a non-gated accessibility/voice/gesture coroutine."""
        metadata.coro_started = True
        try:
            result = await coro
            if not future.done():
                future.set_result(result)
        except asyncio.CancelledError:
            if not future.done():
                future.cancel()
            raise
        except Exception as exc:
            log.error("Scheduler task %r raised: %s", label, exc)
            if not future.done():
                future.set_exception(exc)
        finally:
            self._mark_terminal(metadata)

    async def _run_dev_task(
        self,
        coro: Coroutine[Any, Any, Any],
        future: asyncio.Future,
        label: str,
        timeout_s: float,
        metadata: _TaskMetadata,
    ) -> None:
        """Run a gated dev/background coroutine under the semaphore with timeout."""
        try:
            # Flare admission gate (gap #3): park here — holding NO permit and not
            # counting against the timeout — until dev work is resumed.
            if not self._dev_resume_event.is_set():
                if self._dev_parked >= self.config.max_parked_dev:
                    self._record_reason("denied_parked_limit", denied=True)
                    msg = (f"dev task {label!r} shed — "
                           f"{self.config.max_parked_dev} already parked "
                           f"(flare admission paused)")
                    log.warning("Scheduler: %s (total shed=%d)", msg, self._shed_count)
                    coro.close()
                    if not future.done():
                        future.set_exception(asyncio.QueueFull(msg))
                    return
                self._dev_parked += 1
                self._refresh_load_snapshot()
                log.info(
                    "Scheduler: dev task %r parked (%d/%d) — admission paused (flare)",
                    label, self._dev_parked, self.config.max_parked_dev,
                )
                try:
                    await self._dev_resume_event.wait()
                finally:
                    self._dev_parked -= 1
                    self._refresh_load_snapshot()

            async with self._dev_sem:
                # `_dev_inflight` mirrors the held permit. Increment after acquire
                # and decrement in `finally` so the compatibility gauge cannot leak.
                self._dev_inflight += 1
                self._publish_gauges()
                try:
                    metadata.coro_started = True
                    result = await asyncio.wait_for(coro, timeout=timeout_s)
                    if not future.done():
                        future.set_result(result)
                except asyncio.TimeoutError:
                    msg = f"dev task {label!r} timed out after {timeout_s}s"
                    log.warning("Scheduler: %s", msg)
                    if not future.done():
                        future.set_exception(asyncio.TimeoutError(msg))
                except Exception as exc:
                    log.error("Scheduler dev task %r raised: %s", label, exc)
                    if not future.done():
                        future.set_exception(exc)
                finally:
                    self._dev_inflight -= 1
                    self._publish_gauges()
        except asyncio.CancelledError:
            if not future.done():
                future.cancel()
            raise
        finally:
            self._mark_terminal(metadata)

    # ── Flare admission control (gap #3) ────────────────────────────────────

    def pause_dev(self) -> None:
        """Stop admitting NEW DEV_AGENT/BACKGROUND tasks (called by ResourceGovernor
        on a flare). In-flight dev tasks are not force-killed — asyncio can't
        preempt a running coroutine — but the governor's VRAM eviction reclaims
        their resources and no new heavy task starts until resume_dev(). The fast
        accessibility/voice/gesture path is never gated. Idempotent."""
        if not self._dev_paused:
            self._dev_paused = True
            self._dev_resume_event.clear()
            log.info("AccessibilityScheduler: DEV/BACKGROUND admission PAUSED (flare)")

    def resume_dev(self) -> None:
        """Resume admitting dev/background tasks (flare ended / shutdown). Idempotent."""
        if self._dev_paused:
            self._dev_paused = False
            self._dev_resume_event.set()
            log.info("AccessibilityScheduler: DEV/BACKGROUND admission RESUMED")

    @property
    def dev_paused(self) -> bool:
        return self._dev_paused

    async def wait_dev_admission(self) -> None:
        """Block until dev/background admission is open (flare gate).

        For out-of-band dev producers (DevAgent.drain_goal_queue) that run
        plans as their own tasks rather than through submit(): awaiting this
        before claiming a goal ensures no NEW heavy plan starts mid-flare.
        Returns immediately when admission is open. Does not hold a permit
        and does not count against the parked-dev cap.
        """
        await self._dev_resume_event.wait()

    # ── Supervision (gap #2) ────────────────────────────────────────────────

    def is_healthy(self) -> bool:
        """True iff the worker task is supposed to be and actually still running.

        Cheap, non-blocking — safe for the Supervisor to poll. False when the
        worker died unexpectedly while `_running` is still set.
        """
        return (
            self._running
            and self._worker_task is not None
            and not self._worker_task.done()
        )

    async def restart(self) -> None:
        """Relaunch a dead worker without disturbing the queue (Supervisor-driven).

        Idempotent: no-op if not running (deliberate teardown) or already healthy.
        Surfaces the dead task's exception before relaunching so a recurring crash
        is visible rather than silently papered over.
        """
        if not self._running:
            return  # deliberately stopped — don't fight shutdown
        if self._worker_task is not None and not self._worker_task.done():
            return  # already healthy
        if self._worker_task is not None and self._worker_task.done() \
                and not self._worker_task.cancelled():
            exc = self._worker_task.exception()
            if exc is not None:
                log.error("Scheduler worker had died with %r — relaunching", exc)
        self._worker_task = asyncio.create_task(self._worker(), name="scheduler_worker")
        log.warning("AccessibilityScheduler: worker relaunched by supervisor")

    # ── Status ────────────────────────────────────────────────────────────────

    def get_status(self) -> dict:
        snapshot = self.load_snapshot()
        return {
            "queue_size": snapshot.queued_total,
            "running": self._running,
            "max_concurrent_dev": _MAX_CONCURRENT_DEV,
            "dev_inflight": self._dev_inflight,
            "max_concurrent_subagents": _MAX_CONCURRENT_SUBAGENTS,
            "subagent_inflight": self._subagent_inflight,
            "dev_paused": self._dev_paused,
            "dev_parked": self._dev_parked,
            "max_parked_dev": self.config.max_parked_dev,
            "hard_limit": self.config.hard_limit,
            "accessibility_reserve": self.config.accessibility_reserve,
            "non_accessibility_limit": self.config.non_accessibility_limit,
            "dev_agent_limit": self.config.dev_agent_limit,
            "background_limit": self.config.background_limit,
            "max_queue_depth": self.config.hard_limit,
            "shed_count": self._shed_count,
            "denied_count": snapshot.denied_total,
            "coalesced_count": snapshot.coalesced_total,
            "stale_count": snapshot.stale_total,
            "reason_counts": dict(snapshot.reason_counts),
            "worker_alive": self.is_healthy(),
            "queued_total": snapshot.queued_total,
            "running_total": snapshot.running_total,
            "queued_by_priority": {
                priority.name: snapshot.queued_by_priority[priority]
                for priority in Priority
            },
            "running_by_priority": {
                priority.name: snapshot.running_by_priority[priority]
                for priority in Priority
            },
        }
