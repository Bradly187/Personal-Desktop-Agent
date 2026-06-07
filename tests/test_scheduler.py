"""Tests for AccessibilityScheduler — Gap 1 (AIOS alignment).

Covers:
- Priority ordering: ACCESSIBILITY runs before DEV_AGENT
- Semaphore gate: only 1 DEV_AGENT/BACKGROUND task at a time
- Future resolution: caller gets return value
- Timeout: DEV_AGENT task exceeding 30s → TimeoutError
- Lifecycle: start/stop; get_status
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.scheduler import AccessibilityScheduler, Priority


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _coro(result=None, delay: float = 0.0):
    if delay:
        await asyncio.sleep(delay)
    return result


class _FakeMetrics:
    """Minimal metrics stub exposing set(name, value) like the real singleton."""
    def __init__(self):
        self.gauges: dict = {}
        self.max_queue_depth = 0

    def set(self, name, value):
        self.gauges[name] = value
        if name == "scheduler_queue_depth":
            self.max_queue_depth = max(self.max_queue_depth, value)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

class TestSchedulerLifecycle:
    @pytest.mark.asyncio
    async def test_start_sets_running(self):
        sched = AccessibilityScheduler()
        await sched.start()
        try:
            assert sched._running is True
            assert sched._worker_task is not None
        finally:
            await sched.stop()

    @pytest.mark.asyncio
    async def test_stop_clears_running(self):
        sched = AccessibilityScheduler()
        await sched.start()
        await sched.stop()
        assert sched._running is False

    def test_get_status_before_start(self):
        sched = AccessibilityScheduler()
        status = sched.get_status()
        assert "queue_size" in status
        assert "running" in status
        assert status["running"] is False

    @pytest.mark.asyncio
    async def test_get_status_while_running(self):
        sched = AccessibilityScheduler()
        await sched.start()
        try:
            status = sched.get_status()
            assert status["running"] is True
            assert status["max_concurrent_dev"] == 1
        finally:
            await sched.stop()


# ---------------------------------------------------------------------------
# Future resolution
# ---------------------------------------------------------------------------

class TestFutureResolution:
    @pytest.mark.asyncio
    async def test_accessibility_future_resolves_to_return_value(self):
        sched = AccessibilityScheduler()
        await sched.start()
        try:
            fut = sched.submit(_coro("hello"), Priority.ACCESSIBILITY)
            result = await asyncio.wait_for(fut, timeout=2.0)
            assert result == "hello"
        finally:
            await sched.stop()

    @pytest.mark.asyncio
    async def test_voice_future_resolves(self):
        sched = AccessibilityScheduler()
        await sched.start()
        try:
            fut = sched.submit(_coro(42), Priority.VOICE)
            result = await asyncio.wait_for(fut, timeout=2.0)
            assert result == 42
        finally:
            await sched.stop()

    @pytest.mark.asyncio
    async def test_dev_agent_future_resolves(self):
        sched = AccessibilityScheduler()
        await sched.start()
        try:
            fut = sched.submit(_coro("dev_result"), Priority.DEV_AGENT)
            result = await asyncio.wait_for(fut, timeout=5.0)
            assert result == "dev_result"
        finally:
            await sched.stop()

    @pytest.mark.asyncio
    async def test_future_propagates_exception(self):
        async def _raise():
            raise ValueError("boom")

        sched = AccessibilityScheduler()
        await sched.start()
        try:
            fut = sched.submit(_raise(), Priority.ACCESSIBILITY)
            with pytest.raises(ValueError, match="boom"):
                await asyncio.wait_for(fut, timeout=2.0)
        finally:
            await sched.stop()


# ---------------------------------------------------------------------------
# Priority ordering
# ---------------------------------------------------------------------------

class TestPriorityOrdering:
    @pytest.mark.asyncio
    async def test_accessibility_completes_before_concurrent_dev_agent(self):
        """An ACCESSIBILITY coroutine should complete before a DEV_AGENT
        coroutine that was submitted at the same time and sleeps 300ms."""
        completed_order = []

        async def _acc():
            completed_order.append("acc")

        async def _dev():
            await asyncio.sleep(0.3)
            completed_order.append("dev")

        sched = AccessibilityScheduler()
        await sched.start()
        try:
            fut_dev = sched.submit(_dev(), Priority.DEV_AGENT, label="dev")
            fut_acc = sched.submit(_acc(), Priority.ACCESSIBILITY, label="acc")
            await asyncio.wait_for(asyncio.gather(fut_acc, fut_dev), timeout=3.0)
            assert completed_order[0] == "acc"
        finally:
            await sched.stop()

    @pytest.mark.asyncio
    async def test_voice_completes_before_gesture_when_queued(self):
        """When the worker is busy, VOICE items dequeue before GESTURE."""
        dequeued = []

        async def _tag(name):
            dequeued.append(name)

        sched = AccessibilityScheduler()
        await sched.start()
        try:
            # Stall the worker briefly so the queue builds up
            blocker = sched.submit(_coro(delay=0.05), Priority.ACCESSIBILITY)
            await asyncio.sleep(0)  # yield so worker picks up blocker first

            fut_gesture = sched.submit(_tag("gesture"), Priority.GESTURE)
            fut_voice = sched.submit(_tag("voice"), Priority.VOICE)
            await asyncio.wait_for(
                asyncio.gather(blocker, fut_voice, fut_gesture), timeout=2.0
            )
            # VOICE priority (1) < GESTURE priority (2), so voice dequeues first
            voice_idx = dequeued.index("voice") if "voice" in dequeued else 999
            gesture_idx = dequeued.index("gesture") if "gesture" in dequeued else 999
            assert voice_idx <= gesture_idx
        finally:
            await sched.stop()


# ---------------------------------------------------------------------------
# Semaphore gate
# ---------------------------------------------------------------------------

class TestSemaphoreGate:
    @pytest.mark.asyncio
    async def test_only_one_dev_task_runs_at_a_time(self):
        """5 BACKGROUND tasks submitted together → only 1 active concurrently."""
        active_count = 0
        max_active = 0

        async def _slow_background():
            nonlocal active_count, max_active
            active_count += 1
            max_active = max(max_active, active_count)
            await asyncio.sleep(0.05)
            active_count -= 1

        sched = AccessibilityScheduler()
        await sched.start()
        try:
            futs = [
                sched.submit(_slow_background(), Priority.BACKGROUND, label=f"bg{i}")
                for i in range(5)
            ]
            await asyncio.wait_for(asyncio.gather(*futs), timeout=5.0)
            assert max_active == 1, f"Expected max 1 concurrent, got {max_active}"
        finally:
            await sched.stop()

    @pytest.mark.asyncio
    async def test_dev_and_background_share_semaphore(self):
        """1 DEV_AGENT running → BACKGROUND task waits for the semaphore."""
        active_count = 0
        max_active = 0

        async def _job():
            nonlocal active_count, max_active
            active_count += 1
            max_active = max(max_active, active_count)
            await asyncio.sleep(0.05)
            active_count -= 1

        sched = AccessibilityScheduler()
        await sched.start()
        try:
            fut_dev = sched.submit(_job(), Priority.DEV_AGENT, label="dev")
            fut_bg = sched.submit(_job(), Priority.BACKGROUND, label="bg")
            await asyncio.wait_for(asyncio.gather(fut_dev, fut_bg), timeout=5.0)
            assert max_active == 1
        finally:
            await sched.stop()

    @pytest.mark.asyncio
    async def test_accessibility_unaffected_by_dev_semaphore(self):
        """ACCESSIBILITY tasks bypass the semaphore even when DEV_AGENT holds it."""
        acc_completed = asyncio.Event()

        async def _long_dev():
            await asyncio.sleep(0.2)

        async def _quick_acc():
            acc_completed.set()

        sched = AccessibilityScheduler()
        await sched.start()
        try:
            fut_dev = sched.submit(_long_dev(), Priority.DEV_AGENT)
            fut_acc = sched.submit(_quick_acc(), Priority.ACCESSIBILITY)
            # acc should complete well before dev finishes
            await asyncio.wait_for(acc_completed.wait(), timeout=1.0)
            assert acc_completed.is_set()
            await asyncio.wait_for(fut_dev, timeout=2.0)
        finally:
            await sched.stop()


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------

class TestDevTaskTimeout:
    @pytest.mark.asyncio
    async def test_dev_task_timeout_sets_future_exception(self):
        """A DEV_AGENT task that never completes should raise TimeoutError on its future."""
        from core.scheduler import _DEV_TASK_TIMEOUT_S

        # Patch timeout to a tiny value so the test runs fast
        import core.scheduler as sched_mod
        original = sched_mod._DEV_TASK_TIMEOUT_S
        sched_mod._DEV_TASK_TIMEOUT_S = 0.1

        async def _hang():
            await asyncio.sleep(999)

        sched = AccessibilityScheduler()
        sched._running = True  # rebuild semaphore with patched const
        await sched.start()
        try:
            fut = sched.submit(_hang(), Priority.DEV_AGENT)
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(fut, timeout=2.0)
        finally:
            sched_mod._DEV_TASK_TIMEOUT_S = original
            await sched.stop()

    @pytest.mark.asyncio
    async def test_dev_inflight_released_after_timeout(self):
        """Release-safe: scheduler_dev_inflight returns to 0 even when a dev task times out."""
        import core.scheduler as sched_mod
        original = sched_mod._DEV_TASK_TIMEOUT_S
        sched_mod._DEV_TASK_TIMEOUT_S = 0.1

        async def _hang():
            await asyncio.sleep(999)

        sched = AccessibilityScheduler()
        m = _FakeMetrics()
        sched.set_metrics(m)
        await sched.start()
        try:
            fut = sched.submit(_hang(), Priority.DEV_AGENT)
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(fut, timeout=2.0)
            await asyncio.sleep(0.05)  # let the finally block run
            assert m.gauges["scheduler_dev_inflight"] == 0
            assert sched._dev_inflight == 0
        finally:
            sched_mod._DEV_TASK_TIMEOUT_S = original
            await sched.stop()

    @pytest.mark.asyncio
    async def test_semaphore_released_after_timeout(self):
        """After a dev task times out the semaphore must be released for the next task."""
        import core.scheduler as sched_mod
        original = sched_mod._DEV_TASK_TIMEOUT_S
        sched_mod._DEV_TASK_TIMEOUT_S = 0.1

        async def _hang():
            await asyncio.sleep(999)

        sched = AccessibilityScheduler()
        await sched.start()
        try:
            fut1 = sched.submit(_hang(), Priority.DEV_AGENT, label="hang")
            # Wait for it to time out
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(fut1, timeout=2.0)

            # Now a second dev task should run without blocking
            fut2 = sched.submit(_coro("ok"), Priority.DEV_AGENT, label="ok")
            result = await asyncio.wait_for(fut2, timeout=2.0)
            assert result == "ok"
        finally:
            sched_mod._DEV_TASK_TIMEOUT_S = original
            await sched.stop()


# ---------------------------------------------------------------------------
# Metrics: queue depth + dev-inflight gauges
# ---------------------------------------------------------------------------

class TestSchedulerMetrics:
    @pytest.mark.asyncio
    async def test_queue_depth_published_and_drains(self):
        """Submitting a burst grows scheduler_queue_depth; it returns to 0 after drain."""
        sched = AccessibilityScheduler()
        m = _FakeMetrics()
        sched.set_metrics(m)
        await sched.start()
        try:
            # No await between submits → the worker can't drain yet → depth builds.
            futs = [sched.submit(_coro(i, delay=0.01), Priority.ACCESSIBILITY)
                    for i in range(5)]
            assert m.max_queue_depth >= 2
            await asyncio.wait_for(asyncio.gather(*futs), timeout=3.0)
            assert m.gauges["scheduler_queue_depth"] == 0
        finally:
            await sched.stop()

    @pytest.mark.asyncio
    async def test_dev_inflight_tracks_and_releases(self):
        """scheduler_dev_inflight is 1 while a DEV task runs, 0 after it completes."""
        sched = AccessibilityScheduler()
        m = _FakeMetrics()
        sched.set_metrics(m)
        await sched.start()
        try:
            gate = asyncio.Event()

            async def _dev():
                await gate.wait()

            fut = sched.submit(_dev(), Priority.DEV_AGENT)
            await asyncio.sleep(0.05)            # let it acquire the permit
            assert m.gauges["scheduler_dev_inflight"] == 1
            assert sched._dev_inflight == 1
            gate.set()
            await asyncio.wait_for(fut, timeout=2.0)
            await asyncio.sleep(0.02)            # let the finally block run
            assert m.gauges["scheduler_dev_inflight"] == 0
            assert sched._dev_inflight == 0
        finally:
            await sched.stop()


# ---------------------------------------------------------------------------
# Deadlock safety (single-permit re-entrancy invariant)
# ---------------------------------------------------------------------------

class TestSchedulerDeadlockSafety:
    @pytest.mark.asyncio
    async def test_fast_tier_can_submit_and_await_dev(self):
        """SUPPORTED pattern: a non-permit-holder (fast tier) submits AND awaits a
        DEV task — completes with no deadlock. (The unsupported case — a DEV task
        awaiting another DEV task — is documented as an invariant, not exercised,
        because by design it would hang.)"""
        sched = AccessibilityScheduler()
        await sched.start()
        try:
            async def _parent():
                child = sched.submit(_coro("child"), Priority.DEV_AGENT)
                return await child

            fut = sched.submit(_parent(), Priority.ACCESSIBILITY)
            result = await asyncio.wait_for(fut, timeout=3.0)
            assert result == "child"
        finally:
            await sched.stop()


# ---------------------------------------------------------------------------
# Teardown cleanup
# ---------------------------------------------------------------------------

class TestSchedulerTeardown:
    @pytest.mark.asyncio
    async def test_stop_cancels_inflight_tasks(self):
        """stop() cancels dispatched tasks still in flight (no lingering tasks/permits)."""
        sched = AccessibilityScheduler()
        await sched.start()
        started = asyncio.Event()

        async def _long():
            started.set()
            await asyncio.sleep(999)

        sched.submit(_long(), Priority.ACCESSIBILITY)
        await asyncio.wait_for(started.wait(), timeout=2.0)
        assert len(sched._inflight_tasks) >= 1
        await sched.stop()
        assert len(sched._inflight_tasks) == 0
