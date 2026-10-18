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
import gc
import inspect
import sys
import threading
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
    """Metrics stub that enforces the scheduler's nonblocking bulk contract."""
    def __init__(self):
        self.gauges: dict = {}
        self.max_queue_depth = 0
        self.publish_enabled = True
        self.bulk_calls: list[tuple[dict, bool]] = []

    def set(self, name, value):
        raise AssertionError("scheduler gauges must use set_many")

    def set_many(self, values, *, blocking=True):
        self.bulk_calls.append((dict(values), blocking))
        assert blocking is False
        if not self.publish_enabled:
            return False
        self.gauges.update(values)
        self.max_queue_depth = max(
            self.max_queue_depth,
            values.get("scheduler_queue_depth", 0),
        )
        return True


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
    def test_real_bulk_publication_skips_when_lock_is_busy(self):
        from monitoring.metrics import Metrics

        metrics = Metrics()
        metrics._lock.acquire()
        try:
            assert metrics.set_many(
                {"scheduler_queued_total": 7.0}, blocking=False
            ) is False
        finally:
            metrics._lock.release()

        assert metrics._gauges["scheduler_queued_total"] == 0.0
        assert metrics.set_many(
            {
                "scheduler_queued_total": 2.0,
                "scheduler_running_total": 3.0,
            },
            blocking=False,
        ) is True
        gauges = metrics.get_snapshot()["gauges"]
        assert gauges["scheduler_queued_total"] == 2.0
        assert gauges["scheduler_running_total"] == 3.0
        assert gauges["coordinator_route_tasks_inflight"] == 5.0

    def test_live_gauge_provider_replaces_by_name_until_explicit_unregister(self):
        from monitoring.metrics import Metrics

        class _Provider:
            def __init__(self, value):
                self.value = value

            def gauges(self):
                return {"scheduler_queued_total": self.value}

        metrics = Metrics()
        first = _Provider(1.0)
        second = _Provider(2.0)
        metrics.register_gauge_provider("scheduler", first.gauges)
        metrics.register_gauge_provider("scheduler", second.gauges)

        assert len(metrics._gauge_providers) == 1
        assert metrics._gauge_providers["scheduler"].provider.__self__ is second
        assert (
            metrics.get_snapshot()["gauges"]["coordinator_route_tasks_inflight"]
            == 2.0
        )

        assert metrics.unregister_gauge_provider("scheduler") is True
        assert metrics.unregister_gauge_provider("scheduler") is False
        gauges = metrics.get_snapshot()["gauges"]
        assert gauges["scheduler_queued_total"] == 0.0
        assert gauges["coordinator_route_tasks_inflight"] == 0.0
        assert metrics._gauge_providers == {}

    def test_unregister_clears_persisted_component_and_aggregate_gauges(self):
        from monitoring.metrics import Metrics

        class _Provider:
            def gauges(self):
                return {
                    "scheduler_running_total": 3.0,
                    "fusion_degraded_route_tasks_inflight": 4.0,
                }

        metrics = Metrics()
        owner = _Provider()
        metrics.register_gauge_provider("route_load", owner.gauges)
        assert metrics.get_snapshot()["gauges"][
            "coordinator_route_tasks_inflight"
        ] == 7.0

        assert metrics.unregister_gauge_provider("route_load") is True
        gauges = metrics.get_snapshot()["gauges"]
        assert gauges["scheduler_running_total"] == 0.0
        assert gauges["fusion_degraded_route_tasks_inflight"] == 0.0
        assert gauges["coordinator_route_tasks_inflight"] == 0.0
        assert metrics._gauge_providers == {}

    def test_replacement_clears_omissions_only_after_last_owner_releases_key(self):
        from monitoring.metrics import Metrics

        class _Provider:
            def __init__(self, values):
                self.values = values

            def gauges(self):
                return self.values

        metrics = Metrics()
        first = _Provider(
            {
                "scheduler_running_total": 7.0,
                "fusion_degraded_route_tasks_inflight": 5.0,
            }
        )
        shared = _Provider({"scheduler_running_total": 7.0})
        replacement = _Provider({"scheduler_queued_total": 2.0})
        metrics.register_gauge_provider("route_load", first.gauges)
        metrics.register_gauge_provider("shared_load", shared.gauges)

        metrics.register_gauge_provider("route_load", replacement.gauges)
        assert metrics._gauges["fusion_degraded_route_tasks_inflight"] == 0.0
        assert metrics._gauges["scheduler_running_total"] == 7.0

        assert metrics.unregister_gauge_provider("shared_load") is True
        gauges = metrics.get_snapshot()["gauges"]
        assert gauges["scheduler_running_total"] == 0.0
        assert gauges["scheduler_queued_total"] == 2.0
        assert gauges["coordinator_route_tasks_inflight"] == 2.0

    def test_snapshot_retries_when_provider_replaced_during_collection(self):
        from monitoring.metrics import Metrics

        entered = threading.Event()
        release = threading.Event()
        snapshots = []

        class _BlockingProvider:
            def __init__(self):
                self.calls = 0

            def gauges(self):
                self.calls += 1
                if self.calls > 1:
                    entered.set()
                    release.wait(timeout=2.0)
                return {"scheduler_running_total": 1.0}

        metrics = Metrics()
        old = _BlockingProvider()
        metrics.register_gauge_provider("scheduler", old.gauges)
        snapshot_thread = threading.Thread(
            target=lambda: snapshots.append(metrics.get_snapshot()), daemon=True
        )
        snapshot_thread.start()
        try:
            assert entered.wait(timeout=1.0)
            metrics.register_gauge_provider(
                "scheduler", lambda: {"scheduler_running_total": 2.0}
            )
        finally:
            release.set()
            snapshot_thread.join(timeout=2.0)

        assert not snapshot_thread.is_alive()
        assert snapshots[0]["gauges"]["scheduler_running_total"] == 2.0
        assert snapshots[0]["gauges"]["coordinator_route_tasks_inflight"] == 2.0

    def test_concurrent_shared_key_registration_and_replacement_is_atomic(self):
        from monitoring.metrics import Metrics

        snapshot_entered = threading.Event()
        release_snapshot = threading.Event()
        registration_entered = threading.Event()
        release_registration = threading.Event()
        snapshots = []

        class _OldProvider:
            def __init__(self):
                self.calls = 0

            def gauges(self):
                self.calls += 1
                if self.calls > 1:
                    snapshot_entered.set()
                    release_snapshot.wait(timeout=2.0)
                return {"scheduler_running_total": 7.0}

        class _SharedProvider:
            def __init__(self):
                self.calls = 0

            def gauges(self):
                self.calls += 1
                if self.calls == 1:
                    registration_entered.set()
                    release_registration.wait(timeout=2.0)
                return {"scheduler_running_total": 7.0}

        metrics = Metrics()
        old = _OldProvider()
        shared = _SharedProvider()
        metrics.register_gauge_provider("route_load", old.gauges)

        snapshot_thread = threading.Thread(
            target=lambda: snapshots.append(metrics.get_snapshot()), daemon=True
        )
        registration_thread = threading.Thread(
            target=lambda: metrics.register_gauge_provider(
                "shared_load", shared.gauges
            ),
            daemon=True,
        )
        snapshot_thread.start()
        registration_thread.start()
        try:
            assert snapshot_entered.wait(timeout=1.0)
            assert registration_entered.wait(timeout=1.0)
            metrics.register_gauge_provider(
                "route_load", lambda: {"scheduler_queued_total": 2.0}
            )
            release_registration.set()
            registration_thread.join(timeout=2.0)
            assert not registration_thread.is_alive()
            assert metrics._gauges["scheduler_running_total"] == 7.0
        finally:
            release_registration.set()
            release_snapshot.set()
            registration_thread.join(timeout=2.0)
            snapshot_thread.join(timeout=2.0)

        assert not snapshot_thread.is_alive()
        gauges = snapshots[0]["gauges"]
        assert gauges["scheduler_running_total"] == 7.0
        assert gauges["scheduler_queued_total"] == 2.0
        assert gauges["coordinator_route_tasks_inflight"] == 9.0

        assert metrics.unregister_gauge_provider("route_load") is True
        assert metrics._gauges["scheduler_running_total"] == 7.0
        assert metrics.unregister_gauge_provider("shared_load") is True
        assert metrics._gauges["scheduler_running_total"] == 0.0

    @pytest.mark.asyncio
    async def test_lock_busy_export_does_not_block_snapshot_accounting(self):
        sched = AccessibilityScheduler()
        metrics = _FakeMetrics()
        sched.set_metrics(metrics)
        published = dict(metrics.gauges)
        metrics.publish_enabled = False

        future = sched.submit(_coro("queued"), Priority.VOICE)
        assert sched.load_snapshot().queued_total == 1
        assert metrics.gauges == published
        assert metrics.bulk_calls[-1][1] is False

        await sched.stop()
        assert future.cancelled()
        assert sched.load_snapshot().queued_total == 0

    @pytest.mark.asyncio
    async def test_live_snapshot_clears_stale_gauge_after_lock_busy_terminal(self):
        from monitoring.metrics import Metrics

        sched = AccessibilityScheduler()
        metrics = Metrics()
        sched.set_metrics(metrics)
        await sched.start()
        started = asyncio.Event()
        release = asyncio.Event()
        lock_held = False

        async def _blocked():
            started.set()
            await release.wait()
            return "done"

        try:
            future = sched.submit(_blocked(), Priority.VOICE)
            await asyncio.wait_for(started.wait(), timeout=1.0)
            assert metrics._gauges["scheduler_running_total"] == 1

            metrics._lock.acquire()
            lock_held = True
            release.set()
            assert await asyncio.wait_for(future, timeout=1.0) == "done"
            for _ in range(100):
                if sched.load_snapshot().running_total == 0:
                    break
                await asyncio.sleep(0)

            assert sched.load_snapshot().running_total == 0
            assert metrics._gauges["scheduler_running_total"] == 1

            metrics._lock.release()
            lock_held = False
            gauges = metrics.get_snapshot()["gauges"]
            assert gauges["scheduler_queued_total"] == 0
            assert gauges["scheduler_running_total"] == 0
            assert gauges["scheduler_running_voice"] == 0
            assert gauges["coordinator_route_tasks_inflight"] == 0.0
        finally:
            if lock_held:
                metrics._lock.release()
            release.set()
            await sched.stop()

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

    @pytest.mark.asyncio
    @pytest.mark.parametrize("submit_api", ["submit", "submit_plan"])
    async def test_submission_after_stop_is_terminal_and_explicit_start_reopens(
        self, submit_api, recwarn
    ):
        sched = AccessibilityScheduler()
        await sched.start()
        await sched.stop()
        assert sched._accepting_submissions is False
        ran = False

        async def _probe():
            nonlocal ran
            ran = True
            return "stale"

        coro = _probe()
        snapshot_before = sched.load_snapshot()
        seq_before = sched._seq
        if submit_api == "submit":
            future = sched.submit(coro, Priority.ACCESSIBILITY, label="after-stop")
        else:
            future = sched.submit_plan(coro, label="after-stop")

        assert future.cancelled()
        assert inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED
        assert sched.load_snapshot() is snapshot_before
        assert sched._seq == seq_before
        assert snapshot_before.queued_total == 0
        assert snapshot_before.running_total == 0
        assert all(snapshot_before.queued_by_priority[p] == 0 for p in Priority)
        assert all(snapshot_before.running_by_priority[p] == 0 for p in Priority)

        del coro
        gc.collect()
        await asyncio.sleep(0)
        assert not [
            warning for warning in recwarn
            if "was never awaited" in str(warning.message)
        ]

        await sched.start()
        try:
            assert sched._accepting_submissions is True
            await asyncio.sleep(0)
            assert ran is False
            if submit_api == "submit":
                fresh = sched.submit(
                    _coro("fresh"), Priority.ACCESSIBILITY, label="after-restart"
                )
            else:
                fresh = sched.submit_plan(_coro("fresh"), label="after-restart")
            assert await asyncio.wait_for(fresh, timeout=1.0) == "fresh"
            assert sched.load_snapshot().queued_total == 0
            assert sched.load_snapshot().running_total == 0
        finally:
            await sched.stop()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("submit_api", ["submit", "submit_plan"])
    async def test_submission_while_stop_is_in_progress_is_terminal(
        self, submit_api, recwarn
    ):
        sched = AccessibilityScheduler()
        await sched.start()
        started = asyncio.Event()
        cancellation_seen = asyncio.Event()
        release_cancellation = asyncio.Event()

        async def _blocked():
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancellation_seen.set()
                await release_cancellation.wait()
                raise

        inflight_future = sched.submit(_blocked(), Priority.ACCESSIBILITY)
        await asyncio.wait_for(started.wait(), timeout=1.0)
        stop_task = asyncio.create_task(sched.stop())
        await asyncio.wait_for(cancellation_seen.wait(), timeout=1.0)
        assert sched._accepting_submissions is False

        async def _probe():
            return "never"

        coro = _probe()
        snapshot_before = sched.load_snapshot()
        seq_before = sched._seq
        if submit_api == "submit":
            future = sched.submit(coro, Priority.VOICE, label="during-stop")
        else:
            future = sched.submit_plan(coro, label="during-stop")

        assert future.cancelled()
        assert inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED
        assert sched.load_snapshot() is snapshot_before
        assert sched._seq == seq_before

        del coro
        gc.collect()
        await asyncio.sleep(0)
        assert not [
            warning for warning in recwarn
            if "was never awaited" in str(warning.message)
        ]

        release_cancellation.set()
        await asyncio.wait_for(stop_task, timeout=1.0)
        assert inflight_future.cancelled()
        snapshot = sched.load_snapshot()
        assert snapshot.queued_total == 0
        assert snapshot.running_total == 0
        assert all(snapshot.queued_by_priority[p] == 0 for p in Priority)
        assert all(snapshot.running_by_priority[p] == 0 for p in Priority)
        assert len(sched._inflight_tasks) == 0


# ---------------------------------------------------------------------------
# Phase 2 / PR 2A: authoritative load snapshots
# ---------------------------------------------------------------------------

class TestSchedulerLoadSnapshot:
    def test_snapshot_and_priority_maps_are_truly_immutable(self):
        from dataclasses import FrozenInstanceError

        sched = AccessibilityScheduler()
        snapshot = sched.load_snapshot()

        assert sched.load_snapshot() is snapshot
        with pytest.raises(FrozenInstanceError):
            snapshot.queued_total = 1
        with pytest.raises(TypeError):
            snapshot.queued_by_priority[Priority.ACCESSIBILITY] = 1
        with pytest.raises(TypeError):
            snapshot.running_by_priority[Priority.VOICE] = 1

    @pytest.mark.asyncio
    async def test_every_priority_is_counted_while_queued(self):
        sched = AccessibilityScheduler()
        futures = [
            sched.submit(_coro(priority.name), priority, label=priority.name)
            for priority in Priority
        ]

        snapshot = sched.load_snapshot()
        assert snapshot.queued_total == len(Priority)
        assert snapshot.running_total == 0
        assert all(snapshot.queued_by_priority[p] == 1 for p in Priority)
        assert all(snapshot.running_by_priority[p] == 0 for p in Priority)

        await sched.stop()
        assert all(future.cancelled() for future in futures)
        assert sched.load_snapshot().queued_total == 0
        assert sched.load_snapshot().running_total == 0

    @pytest.mark.asyncio
    async def test_every_priority_is_counted_while_dispatched_or_waiting(self):
        sched = AccessibilityScheduler()
        metrics = _FakeMetrics()
        sched.set_metrics(metrics)
        await sched.start()
        release = asyncio.Event()

        async def _blocked():
            await release.wait()
            return "done"

        try:
            futures = [
                sched.submit(_blocked(), priority, label=priority.name)
                for priority in Priority
            ]
            for _ in range(100):
                if sched.load_snapshot().running_total == len(Priority):
                    break
                await asyncio.sleep(0)

            snapshot = sched.load_snapshot()
            assert snapshot.queued_total == 0
            assert snapshot.running_total == len(Priority)
            assert all(snapshot.running_by_priority[p] == 1 for p in Priority)
            assert metrics.gauges["scheduler_running_total"] == len(Priority)
            assert metrics.gauges["scheduler_running_dev_agent"] == 1
            assert metrics.gauges["scheduler_running_background"] == 1

            release.set()
            assert await asyncio.wait_for(asyncio.gather(*futures), timeout=2.0) == [
                "done"
            ] * len(Priority)
            assert sched.load_snapshot().running_total == 0
        finally:
            release.set()
            await sched.stop()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("priority", [Priority.ACCESSIBILITY, Priority.DEV_AGENT])
    async def test_success_and_exception_return_accounting_to_zero(self, priority):
        async def _raise():
            raise ValueError("terminal boom")

        sched = AccessibilityScheduler()
        await sched.start()
        try:
            assert await sched.submit(_coro("ok"), priority) == "ok"
            assert sched.load_snapshot().running_total == 0

            future = sched.submit(_raise(), priority)
            with pytest.raises(ValueError, match="terminal boom"):
                await future
            assert sched.load_snapshot().queued_total == 0
            assert sched.load_snapshot().running_total == 0
        finally:
            await sched.stop()

    @pytest.mark.asyncio
    async def test_timeout_returns_running_accounting_to_zero(self, monkeypatch):
        import core.scheduler as sched_mod

        monkeypatch.setattr(sched_mod, "_DEV_TASK_TIMEOUT_S", 0.01)
        sched = AccessibilityScheduler()
        await sched.start()
        try:
            future = sched.submit(asyncio.sleep(60), Priority.DEV_AGENT)
            with pytest.raises(asyncio.TimeoutError):
                await future
            assert sched.load_snapshot().running_total == 0
            assert sched.load_snapshot().running_by_priority[Priority.DEV_AGENT] == 0
        finally:
            await sched.stop()

    @pytest.mark.asyncio
    async def test_dispatched_cancellation_is_terminal_and_cancels_future(self):
        sched = AccessibilityScheduler()
        await sched.start()
        started = asyncio.Event()

        async def _blocked():
            started.set()
            await asyncio.Event().wait()

        try:
            future = sched.submit(_blocked(), Priority.VOICE)
            await asyncio.wait_for(started.wait(), timeout=1.0)
            task = next(iter(sched._inflight_tasks))
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.sleep(0)

            assert future.cancelled()
            assert sched.load_snapshot().running_total == 0
        finally:
            await sched.stop()

    @pytest.mark.asyncio
    async def test_dispatch_failure_is_terminal(self, monkeypatch):
        import core.scheduler as sched_mod

        sched = AccessibilityScheduler()
        await sched.start()
        try:
            def _fail_create_task(*_args, **_kwargs):
                raise RuntimeError("dispatch unavailable")

            monkeypatch.setattr(sched_mod.asyncio, "create_task", _fail_create_task)
            future = sched.submit(_coro("never"), Priority.GESTURE)
            for _ in range(20):
                if future.done():
                    break
                await asyncio.sleep(0)

            with pytest.raises(RuntimeError, match="dispatch unavailable"):
                future.result()
            assert sched.load_snapshot().queued_total == 0
            assert sched.load_snapshot().running_total == 0
        finally:
            await sched.stop()

    @pytest.mark.asyncio
    async def test_restart_preserves_queued_accounting(self):
        sched = AccessibilityScheduler()
        await sched.start()
        try:
            sched._worker_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await sched._worker_task

            future = sched.submit(_coro("after restart"), Priority.ACCESSIBILITY)
            assert sched.load_snapshot().queued_total == 1
            await sched.restart()
            assert await asyncio.wait_for(future, timeout=1.0) == "after restart"
            assert sched.load_snapshot().queued_total == 0
            assert sched.load_snapshot().running_total == 0
        finally:
            await sched.stop()

    @pytest.mark.asyncio
    async def test_ignored_future_exception_is_observed_once_and_remains_awaitable(
        self, caplog
    ):
        async def _raise():
            raise RuntimeError("observable failure")

        sched = AccessibilityScheduler()
        await sched.start()
        try:
            with caplog.at_level("ERROR", logger="core.scheduler"):
                future = sched.submit(_raise(), Priority.ACCESSIBILITY)
                for _ in range(20):
                    if future.done() and sched.load_snapshot().running_total == 0:
                        break
                    await asyncio.sleep(0)
                await asyncio.sleep(0)

                observed = [
                    record for record in caplog.records
                    if "observable failure" in record.getMessage()
                ]
                assert len(observed) == 1
                with pytest.raises(RuntimeError, match="observable failure"):
                    await future
                with pytest.raises(RuntimeError, match="observable failure"):
                    await future
        finally:
            await sched.stop()


def test_metrics_predeclare_authoritative_and_compatibility_scheduler_gauges():
    from monitoring.metrics import Metrics

    metrics = Metrics()
    stored_gauges = metrics._gauges
    expected = {
        "scheduler_queue_depth",
        "scheduler_dev_inflight",
        "scheduler_dev_parked",
        "scheduler_subagent_inflight",
        "scheduler_shed_total",
        "scheduler_queued_total",
        "scheduler_running_total",
        "fusion_degraded_route_tasks_inflight",
    }
    expected.update(f"scheduler_queued_{priority.name.lower()}" for priority in Priority)
    expected.update(f"scheduler_running_{priority.name.lower()}" for priority in Priority)
    assert expected <= stored_gauges.keys()
    assert "coordinator_route_tasks_inflight" not in stored_gauges
    assert (
        metrics.get_snapshot()["gauges"]["coordinator_route_tasks_inflight"]
        == 0.0
    )
