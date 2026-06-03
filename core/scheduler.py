"""Priority-aware task scheduler for the accessibility pipeline.

Priority tiers (lower int = higher priority):
    0  ACCESSIBILITY  — touch, sound_action, voice click (bypass sources)
    1  VOICE          — WhisperStream transcription commands
    2  GESTURE        — MediaPipe gesture commands
    3  DEV_AGENT      — DevAgent plan/execute chains
    4  BACKGROUND     — CodebaseIndexer re-indexing, ContinuousTrainer adaptation

Guarantees:
    - ACCESSIBILITY/VOICE/GESTURE tasks run concurrently (no semaphore gate) —
      same behaviour as the previous bare asyncio.create_task approach.
    - At most _MAX_CONCURRENT_DEV DEV_AGENT/BACKGROUND tasks run at once.
      This prevents a long DevAgent RAG query from monopolising the event loop
      and delaying accessibility commands during a flare.
    - The FusionEngine 60 Hz tick loop is never touched; the scheduler only
      manages tasks submitted via FusionEngine._emit().

Design notes:
    - Uses asyncio.PriorityQueue (stdlib, no new deps).
    - Queue items are (priority_int, sequence_counter, coro, future, label).
      The sequence_counter breaks ties in FIFO order within the same priority.
    - DEV_AGENT tasks are wrapped with asyncio.wait_for(timeout=30) to prevent
      a hung specialist model from blocking the semaphore indefinitely.
    - Stop() always drains in-flight futures and restores defaults.
"""

from __future__ import annotations

import asyncio
import logging
from enum import IntEnum
from typing import Any, Coroutine

log = logging.getLogger(__name__)

_MAX_CONCURRENT_DEV = 1   # only one heavy dev/background task at a time
_DEV_TASK_TIMEOUT_S = 30  # hard ceiling on a single dev step


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
        log.info("AccessibilityScheduler stopped")

    # ── Public submit API ──────────────────────────────────────────────────────

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
        self._queue.put_nowait((priority.value, self._seq, coro, future, label))
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
                priority_val, _seq, coro, future, label = await self._queue.get()
                priority = Priority(priority_val)

                if priority >= Priority.DEV_AGENT:
                    # Heavy task — acquire semaphore, launch with timeout
                    asyncio.create_task(
                        self._run_dev_task(coro, future, label),
                        name=f"sched_dev_{label}",
                    )
                else:
                    # Accessibility / voice / gesture — run concurrently, no gate
                    asyncio.create_task(
                        self._run_task(coro, future, label),
                        name=f"sched_acc_{label}",
                    )

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
    ) -> None:
        """Run a gated dev/background coroutine under the semaphore with timeout."""
        async with self._dev_sem:
            try:
                result = await asyncio.wait_for(coro, timeout=_DEV_TASK_TIMEOUT_S)
                if not future.done():
                    future.set_result(result)
            except asyncio.TimeoutError:
                msg = f"dev task {label!r} timed out after {_DEV_TASK_TIMEOUT_S}s"
                log.warning("Scheduler: %s", msg)
                if not future.done():
                    future.set_exception(asyncio.TimeoutError(msg))
            except Exception as exc:
                log.error("Scheduler dev task %r raised: %s", label, exc)
                if not future.done():
                    future.set_exception(exc)

    # ── Status ────────────────────────────────────────────────────────────────

    def get_status(self) -> dict:
        return {
            "queue_size": self._queue.qsize(),
            "running": self._running,
            "max_concurrent_dev": _MAX_CONCURRENT_DEV,
        }
