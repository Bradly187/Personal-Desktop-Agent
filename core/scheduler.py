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

    - ACCESSIBILITY / VOICE / GESTURE run UNCAPPED and concurrently (no gate).
      Deliberate: capping the fast path would add latency to an RA user's
      commands. Do NOT add backpressure here.
    - At most _MAX_CONCURRENT_DEV (=1) DEV_AGENT/BACKGROUND tasks run at once,
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
    - Queue items are (priority_int, seq, coro, future, label, timeout_s); seq
      breaks ties in FIFO order within a priority.
    - DEV_AGENT/BACKGROUND tasks run under asyncio.wait_for(timeout) so a hung
      specialist never holds the permit indefinitely.
"""

from __future__ import annotations

import asyncio
import logging
import time
from enum import IntEnum
from typing import Any, Coroutine

log = logging.getLogger(__name__)

_MAX_CONCURRENT_DEV = 1    # only one heavy dev/background task at a time
_DEV_TASK_TIMEOUT_S = 30   # ceiling for single specialist-model inferences
_PLAN_TASK_TIMEOUT_S = 300 # ceiling for full DevAgent plan_and_run() (5 min)

# Parked-task bound (gap F): during a long flare, dev tasks dequeued by the worker
# park in _run_dev_task waiting for resume. Without a cap they accumulate unbounded
# (off-queue, so the queue bound doesn't cover them). Beyond this many parked, new
# dev tasks are shed instead of parked — a brief flare still parks-and-resumes, a
# long one stops growing memory.
_MAX_PARKED_DEV = 16

# Queue bound (gap #4): cap pending items so a runaway producer (gesture
# misfires, a voice-hallucination loop, a stuck dev replan) can't grow the queue
# without limit. Only DEV_AGENT/BACKGROUND submissions are shed when full — the
# fast accessibility/voice/gesture path is NEVER shed (those are tiny and bounded
# by sensor rates; dropping a user's command is unacceptable).
_MAX_QUEUE_DEPTH = 256

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

    def __init__(self) -> None:
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._seq: int = 0
        self._worker_task: asyncio.Task | None = None
        self._running = False
        self._dev_sem = asyncio.Semaphore(_MAX_CONCURRENT_DEV)
        self._subagent_sem = asyncio.Semaphore(_MAX_CONCURRENT_SUBAGENTS)  # gap #1 fan-out
        self._metrics = None                              # set via set_metrics()
        self._dev_inflight: int = 0                       # DEV/BACKGROUND tasks holding the permit
        self._subagent_inflight: int = 0                  # sub-agent fan-out tasks in flight
        self._shed_count: int = 0                         # DEV/BACKGROUND submissions shed (queue full or parked cap)
        self._dev_parked: int = 0                          # dev tasks parked at the flare gate
        self._inflight_tasks: set[asyncio.Task] = set()   # dispatched tasks, for stop() cleanup
        # Flare admission gate (gap #3): set == dev work admitted; cleared == paused.
        self._dev_resume_event = asyncio.Event()
        self._dev_resume_event.set()
        self._dev_paused: bool = False

    def set_metrics(self, metrics) -> None:
        """Wire the Metrics singleton for queue-depth / in-flight visibility."""
        self._metrics = metrics
        self._publish_gauges()

    def _publish_gauges(self) -> None:
        """Mirror queue depth + dev-permit usage into the metrics gauges (non-fatal)."""
        if self._metrics is None:
            return
        try:
            self._metrics.set("scheduler_queue_depth", self._queue.qsize())
            self._metrics.set("scheduler_dev_inflight", self._dev_inflight)
            self._metrics.set("scheduler_dev_parked", self._dev_parked)
            self._metrics.set("scheduler_subagent_inflight", self._subagent_inflight)
        except Exception:
            pass

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._running:
            log.warning("AccessibilityScheduler.start() called while already running — ignored")
            return
        self._running = True
        self._worker_task = asyncio.create_task(
            self._worker(), name="scheduler_worker"
        )
        log.info("AccessibilityScheduler started (max_concurrent_dev=%d)", _MAX_CONCURRENT_DEV)

    async def stop(self) -> None:
        self._running = False
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        # Cancel any dispatched tasks still in flight so no task (and no held
        # _dev_sem permit) lingers past shutdown — resource invariant 4.
        inflight = list(self._inflight_tasks)
        for t in inflight:
            t.cancel()
        for t in inflight:
            try:
                await t
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        self._inflight_tasks.clear()
        self._publish_gauges()
        log.info("AccessibilityScheduler stopped")

    # ── Public submit API ──────────────────────────────────────────────────────

    def _shed_if_full(self, priority: "Priority", future: asyncio.Future, label: str) -> bool:
        """Reject a DEV/BACKGROUND submission when the queue is at capacity (gap #4).

        Returns True (resolving `future` with QueueFull) if shed. The fast
        accessibility/voice/gesture path is never shed — it returns False here.
        """
        if priority < Priority.DEV_AGENT:
            return False
        if self._queue.qsize() < _MAX_QUEUE_DEPTH:
            return False
        self._shed_count += 1
        msg = (f"scheduler queue full ({_MAX_QUEUE_DEPTH}) — shed {priority.name} "
               f"task {label!r}")
        log.warning("AccessibilityScheduler: %s (total shed=%d)", msg, self._shed_count)
        if self._metrics is not None:
            try:
                self._metrics.set("scheduler_shed_total", self._shed_count)
            except Exception:
                pass
        if not future.done():
            future.set_exception(asyncio.QueueFull(msg))
        return True

    def submit_plan(
        self,
        coro: Coroutine[Any, Any, Any],
        label: str = "",
        trace_id: str = "",
    ) -> asyncio.Future:
        """Schedule a full DevAgent plan at DEV_AGENT priority with the 5-min ceiling.

        Use this instead of submit(..., Priority.DEV_AGENT) when the coroutine is
        plan_and_run() rather than a single specialist-model inference.

        NOTE (resource invariant 2/3): this holds the single `_dev_sem` permit for
        up to _PLAN_TASK_TIMEOUT_S. The submitted coroutine MUST NOT itself
        submit-and-await another DEV/BACKGROUND task on this scheduler.
        """
        self._seq += 1
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        if self._shed_if_full(Priority.DEV_AGENT, future, label):
            coro.close()   # shed → never enqueued; close it so it isn't leaked
            return future
        self._queue.put_nowait(
            (Priority.DEV_AGENT.value, self._seq, coro, future, label,
             _PLAN_TASK_TIMEOUT_S, trace_id, time.monotonic())
        )
        self._publish_gauges()
        return future

    def submit(
        self,
        coro: Coroutine[Any, Any, Any],
        priority: Priority,
        label: str = "",
        trace_id: str = "",
    ) -> asyncio.Future:
        """Schedule a coroutine at the given priority.

        Returns a Future that resolves to the coroutine's return value.
        Callers may await it or ignore it (fire-and-forget).
        """
        self._seq += 1
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        if self._shed_if_full(priority, future, label):
            coro.close()   # shed → never enqueued; close it so it isn't leaked
            return future
        self._queue.put_nowait(
            (priority.value, self._seq, coro, future, label,
             _DEV_TASK_TIMEOUT_S, trace_id, time.monotonic())
        )
        self._publish_gauges()
        return future

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
             timeout_s, trace_id, enqueue_ts) = item
            try:
                self._publish_gauges()   # depth dropped by one
                priority = Priority(priority_val)

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
                    t = asyncio.create_task(
                        self._run_dev_task(coro, future, label, timeout_s),
                        name=f"sched_dev_{label}",
                    )
                else:
                    # Accessibility / voice / gesture — run concurrently, no gate
                    t = asyncio.create_task(
                        self._run_task(coro, future, label),
                        name=f"sched_acc_{label}",
                    )

                # Track dispatched tasks so stop() can cancel any still in flight.
                self._inflight_tasks.add(t)
                t.add_done_callback(self._inflight_tasks.discard)

            except asyncio.CancelledError:
                # Resolve + close the in-hand item so its awaiter never hangs and
                # the coroutine isn't leaked (#21), then exit.
                if not future.done():
                    future.set_exception(asyncio.CancelledError())
                coro.close()
                self._queue.task_done()
                break
            except Exception as exc:
                # Dispatch failed AFTER dequeue (e.g. create_task on a closing
                # loop). Without this the future never resolves and the coro
                # leaks "never awaited" (#21).
                log.warning("AccessibilityScheduler._worker dispatch error: %s", exc)
                if not future.done():
                    future.set_exception(exc)
                try:
                    coro.close()
                except Exception:
                    pass
                self._queue.task_done()
                continue

            self._queue.task_done()

    async def _run_task(
        self,
        coro: Coroutine[Any, Any, Any],
        future: asyncio.Future,
        label: str,
    ) -> None:
        """Run a non-gated accessibility/voice/gesture coroutine."""
        try:
            result = await coro
            if not future.done():
                future.set_result(result)
        except Exception as exc:
            log.error("Scheduler task %r raised: %s", label, exc)
            if not future.done():
                future.set_exception(exc)

    async def _run_dev_task(
        self,
        coro: Coroutine[Any, Any, Any],
        future: asyncio.Future,
        label: str,
        timeout_s: float = _DEV_TASK_TIMEOUT_S,
    ) -> None:
        """Run a gated dev/background coroutine under the semaphore with timeout."""
        # Flare admission gate (gap #3): park here — holding NO permit and not
        # counting against the timeout — until dev work is resumed. Accessibility
        # never reaches this path. A parked task is still cancellable by stop().
        if not self._dev_resume_event.is_set():
            # Bound parked tasks (gap F): a long flare must not pile up unbounded
            # parked coroutines. Beyond the cap, shed rather than park.
            if self._dev_parked >= _MAX_PARKED_DEV:
                self._shed_count += 1
                msg = (f"dev task {label!r} shed — {_MAX_PARKED_DEV} already parked "
                       f"(flare admission paused)")
                log.warning("Scheduler: %s (total shed=%d)", msg, self._shed_count)
                self._publish_gauges()
                coro.close()   # never awaited — release it
                if not future.done():
                    future.set_exception(asyncio.QueueFull(msg))
                return
            self._dev_parked += 1
            self._publish_gauges()
            log.info("Scheduler: dev task %r parked (%d/%d) — admission paused (flare)",
                     label, self._dev_parked, _MAX_PARKED_DEV)
            try:
                await self._dev_resume_event.wait()
            finally:
                # finally → decremented on resume, cancellation, or error.
                self._dev_parked -= 1
                self._publish_gauges()
        async with self._dev_sem:
            # `_dev_inflight` mirrors the held permit. Increment after acquire and
            # decrement in `finally` so the gauge can NEVER leak on timeout,
            # exception, or cancellation (resource invariant 1).
            self._dev_inflight += 1
            self._publish_gauges()
            try:
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
        return {
            "queue_size": self._queue.qsize(),
            "running": self._running,
            "max_concurrent_dev": _MAX_CONCURRENT_DEV,
            "dev_inflight": self._dev_inflight,
            "max_concurrent_subagents": _MAX_CONCURRENT_SUBAGENTS,
            "subagent_inflight": self._subagent_inflight,
            "dev_paused": self._dev_paused,
            "dev_parked": self._dev_parked,
            "max_parked_dev": _MAX_PARKED_DEV,
            "max_queue_depth": _MAX_QUEUE_DEPTH,
            "shed_count": self._shed_count,
            "worker_alive": self.is_healthy(),
        }
