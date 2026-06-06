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

Multi-agent fan-out (deferred — design note): to run sub-steps concurrently from
    within a dev task, gather independent READ-ONLY coroutines directly
    (asyncio.gather), or use a SEPARATE sub-task semaphore with N>1 permits that
    is distinct from `_dev_sem`. Never await children that contend for a permit
    the parent is holding (invariant 2).

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
from enum import IntEnum
from typing import Any, Coroutine

log = logging.getLogger(__name__)

_MAX_CONCURRENT_DEV = 1    # only one heavy dev/background task at a time
_DEV_TASK_TIMEOUT_S = 30   # ceiling for single specialist-model inferences
_PLAN_TASK_TIMEOUT_S = 300 # ceiling for full DevAgent plan_and_run() (5 min)


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
        self._metrics = None                              # set via set_metrics()
        self._dev_inflight: int = 0                       # DEV/BACKGROUND tasks holding the permit
        self._inflight_tasks: set[asyncio.Task] = set()   # dispatched tasks, for stop() cleanup

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

    def submit_plan(
        self,
        coro: Coroutine[Any, Any, Any],
        label: str = "",
    ) -> asyncio.Future:
        """Schedule a full DevAgent plan at DEV_AGENT priority with the 5-min ceiling.

        Use this instead of submit(..., Priority.DEV_AGENT) when the coroutine is
        plan_and_run() rather than a single specialist-model inference.
        """
        self._seq += 1
        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        self._queue.put_nowait(
            (Priority.DEV_AGENT.value, self._seq, coro, future, label, _PLAN_TASK_TIMEOUT_S)
        )
        self._publish_gauges()
        return future

    def submit(
        self,
        coro: Coroutine[Any, Any, Any],
        priority: Priority,
        label: str = "",
    ) -> asyncio.Future:
        """Schedule a coroutine at the given priority.

        Returns a Future that resolves to the coroutine's return value.
        Callers may await it or ignore it (fire-and-forget).
        """
        self._seq += 1
        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        self._queue.put_nowait(
            (priority.value, self._seq, coro, future, label, _DEV_TASK_TIMEOUT_S)
        )
        self._publish_gauges()
        return future

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
                priority_val, _seq, coro, future, label, timeout_s = await self._queue.get()
                self._publish_gauges()   # depth dropped by one
                priority = Priority(priority_val)

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
                self._queue.task_done()

            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.warning("AccessibilityScheduler._worker error: %s", exc)

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

    # ── Status ────────────────────────────────────────────────────────────────

    def get_status(self) -> dict:
        return {
            "queue_size": self._queue.qsize(),
            "running": self._running,
            "max_concurrent_dev": _MAX_CONCURRENT_DEV,
            "dev_inflight": self._dev_inflight,
        }
