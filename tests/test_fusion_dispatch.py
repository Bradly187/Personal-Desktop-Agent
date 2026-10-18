"""Dispatch-path tests for FusionEngine._emit (Gap 5b).

The 60 Hz fusion loop hands a resolved Command to the coordinator via one of two
paths: through the AccessibilityScheduler's bounded priority pool when one is
wired, or — as an INTENTIONAL graceful-degradation fallback — a bare
fire-and-forget create_task when it is not (startup window, or the watchdog
deliberately set_scheduler(None) because the scheduler wedged). These pin the
dual-path contract and that the route-task circuit breaker bounds the fallback.
"""

import asyncio

from unittest.mock import AsyncMock, MagicMock

from core.command_executor import Command
from core.fusion_engine import FusionEngine


def _engine():
    e = FusionEngine(screen_width=1920, screen_height=1080)
    e.set_coordinator(AsyncMock())
    return e


async def test_fallback_dispatch_without_scheduler_uses_create_task():
    e = _engine()
    e._coordinator.route = AsyncMock(return_value={"status": "ok"})
    assert e._scheduler is None   # no scheduler wired → fallback path

    await e._emit(Command(source="voice", text="hi", action="CLICK", trace_id="t1"))
    await asyncio.sleep(0)        # let the created task run to completion

    e._coordinator.route.assert_awaited_once()


async def test_first_degraded_task_is_published_and_terminal_drain_clears_it():
    from monitoring.metrics import Metrics

    engine = _engine()
    metrics = Metrics()
    engine.set_metrics(metrics)
    started = asyncio.Event()
    release = asyncio.Event()

    async def _blocked_route(_cmd):
        started.set()
        await release.wait()
        return {"status": "ok"}

    engine._coordinator.route = AsyncMock(side_effect=_blocked_route)
    await engine._emit(
        Command(source="voice", text="hi", action="CLICK", trace_id="degraded")
    )

    gauges = metrics.get_snapshot()["gauges"]
    assert gauges["fusion_degraded_route_tasks_inflight"] == 1.0
    assert gauges["coordinator_route_tasks_inflight"] == 1.0
    await asyncio.wait_for(started.wait(), timeout=1.0)

    release.set()
    for _ in range(100):
        if not engine._route_tasks:
            break
        await asyncio.sleep(0)

    gauges = metrics.get_snapshot()["gauges"]
    assert gauges["fusion_degraded_route_tasks_inflight"] == 0.0
    assert gauges["coordinator_route_tasks_inflight"] == 0.0


async def test_live_snapshot_combines_scheduler_and_degraded_load_after_dropped_drains():
    from core.scheduler import AccessibilityScheduler
    from monitoring.metrics import Metrics

    engine = _engine()
    scheduler = AccessibilityScheduler()
    metrics = Metrics()
    scheduler.set_metrics(metrics)
    engine.set_metrics(metrics)
    engine.set_scheduler(scheduler)
    await scheduler.start()

    normal_started = asyncio.Event()
    degraded_started = asyncio.Event()
    normal_release = asyncio.Event()
    degraded_release = asyncio.Event()
    lock_held = False

    async def _blocked_route(cmd):
        if cmd.text == "normal":
            normal_started.set()
            await normal_release.wait()
        else:
            degraded_started.set()
            await degraded_release.wait()
        return {"status": "ok"}

    engine._coordinator.route = AsyncMock(side_effect=_blocked_route)
    try:
        await engine._emit(
            Command(source="voice", text="normal", action="CLICK", trace_id="normal")
        )
        await asyncio.wait_for(normal_started.wait(), timeout=1.0)

        engine.set_scheduler(None)
        await engine._emit(
            Command(
                source="gesture",
                text="degraded",
                action="CLICK",
                trace_id="degraded",
            )
        )
        await asyncio.wait_for(degraded_started.wait(), timeout=1.0)

        gauges = metrics.get_snapshot()["gauges"]
        assert gauges["scheduler_running_total"] == 1
        assert gauges["fusion_degraded_route_tasks_inflight"] == 1.0
        assert gauges["coordinator_route_tasks_inflight"] == 2.0

        metrics._lock.acquire()
        lock_held = True
        normal_release.set()
        degraded_release.set()
        for _ in range(100):
            if (
                scheduler.load_snapshot().running_total == 0
                and not engine._route_tasks
            ):
                break
            await asyncio.sleep(0)

        assert scheduler.load_snapshot().running_total == 0
        assert not engine._route_tasks
        assert metrics._gauges["scheduler_running_total"] == 1
        assert metrics._gauges["fusion_degraded_route_tasks_inflight"] == 1.0

        metrics._lock.release()
        lock_held = False
        gauges = metrics.get_snapshot()["gauges"]
        assert gauges["scheduler_queued_total"] == 0
        assert gauges["scheduler_running_total"] == 0
        assert gauges["fusion_degraded_route_tasks_inflight"] == 0.0
        assert gauges["coordinator_route_tasks_inflight"] == 0.0
    finally:
        if lock_held:
            metrics._lock.release()
        normal_release.set()
        degraded_release.set()
        await scheduler.stop()
        if engine._route_tasks:
            await asyncio.gather(*engine._route_tasks, return_exceptions=True)


async def test_dispatch_with_scheduler_uses_submit_with_priority():
    e = _engine()
    # MagicMock (not Async) so building the work item never creates an un-awaited
    # coroutine — the scheduler is what would await it in production.
    e._coordinator.route = MagicMock(return_value="work-item")
    from core.scheduler import SchedulerConfig

    sched = MagicMock()
    sched.config = SchedulerConfig()
    e.set_scheduler(sched)

    await e._emit(Command(source="voice", text="hi", action="CLICK", trace_id="t2"))

    sched.submit.assert_called_once()
    args, kwargs = sched.submit.call_args
    assert args[0] == "work-item"          # the coordinator.route(cmd) work item
    assert kwargs.get("priority") is not None
    assert kwargs.get("trace_id") == "t2"
    assert kwargs.get("source") == "voice"
    assert kwargs.get("coalesce_key") == ("CLICK", "hi", "{}", None, ())
    assert kwargs.get("stale_after_s") == 2.5


async def test_degraded_admission_sheds_at_configured_non_accessibility_limit():
    from core.scheduler import Priority

    e = _engine()
    e._coordinator.route = AsyncMock()
    e._degraded_by_priority[Priority.VOICE] = e._scheduler_config.non_accessibility_limit

    await e._emit(Command(source="voice", text="hi", action="CLICK", trace_id="t3"))

    assert e._route_breaker_open is True
    e._coordinator.route.assert_not_called()
    assert e._degraded_reason_counts["denied_accessibility_reserve"] == 1


async def test_scheduler_backed_route_is_counted_queued_then_running_without_awaiting():
    from core.scheduler import AccessibilityScheduler, Priority
    from monitoring.metrics import Metrics

    engine = _engine()
    scheduler = AccessibilityScheduler()
    metrics = Metrics()
    scheduler.set_metrics(metrics)
    engine.set_scheduler(scheduler)
    await scheduler.start()
    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow_route(_cmd):
        started.set()
        await release.wait()
        return {"status": "ok"}

    engine._coordinator.route = AsyncMock(side_effect=_slow_route)
    try:
        # _emit returns while route() is still blocked; no 60 Hz path await added.
        await engine._emit(
            Command(source="voice", text="slow", action="CLICK", trace_id="phase-2")
        )
        queued = scheduler.load_snapshot()
        assert queued.queued_total == 1
        assert queued.queued_by_priority[Priority.VOICE] == 1
        assert engine._route_tasks == set()

        await asyncio.wait_for(started.wait(), timeout=1.0)
        running = scheduler.load_snapshot()
        assert running.queued_total == 0
        assert running.running_total == 1
        assert running.running_by_priority[Priority.VOICE] == 1
        assert (
            metrics.get_snapshot()["gauges"]["coordinator_route_tasks_inflight"]
            == 1
        )

        release.set()
        for _ in range(100):
            if scheduler.load_snapshot().running_total == 0:
                break
            await asyncio.sleep(0)
        assert scheduler.load_snapshot().running_total == 0
        assert (
            metrics.get_snapshot()["gauges"]["coordinator_route_tasks_inflight"]
            == 0
        )
    finally:
        release.set()
        await scheduler.stop()


async def test_scheduler_path_does_not_activate_phase_3_admission():
    engine = _engine()
    engine._coordinator.route = MagicMock(return_value="work-item")
    scheduler = MagicMock()
    engine.set_scheduler(scheduler)
    fillers = [asyncio.ensure_future(asyncio.sleep(5)) for _ in range(25)]
    engine._route_tasks.update(fillers)
    try:
        await engine._emit(
            Command(source="voice", text="still admitted", action="CLICK", trace_id="observe")
        )
        scheduler.submit.assert_called_once()
        assert engine._route_breaker_open is False
    finally:
        for task in fillers:
            task.cancel()


async def test_degraded_route_exception_is_observed_once(caplog):
    engine = _engine()

    async def _raise(_cmd):
        raise RuntimeError("degraded boom")

    engine._coordinator.route = AsyncMock(side_effect=_raise)
    with caplog.at_level("ERROR", logger="core.fusion_engine"):
        await engine._emit(
            Command(source="voice", text="fail", action="CLICK", trace_id="fallback")
        )
        for _ in range(20):
            if not engine._route_tasks:
                break
            await asyncio.sleep(0)
        await asyncio.sleep(0)

    observed = [
        record for record in caplog.records
        if "FusionEngine route task failed: degraded boom" in record.getMessage()
    ]
    assert len(observed) == 1


async def test_degraded_fallback_uses_same_tiny_hard_limit_and_reserve():
    from core.scheduler import SchedulerConfig

    engine = _engine()
    engine._scheduler_config = SchedulerConfig(
        hard_limit=6,
        accessibility_reserve=2,
        non_accessibility_limit=4,
        dev_agent_limit=3,
        background_limit=1,
        max_parked_dev=2,
    )
    release = asyncio.Event()

    async def _blocked(_cmd):
        await release.wait()
        return {"status": "ok"}

    engine._coordinator.route = AsyncMock(side_effect=_blocked)
    try:
        for index in range(4):
            await engine._emit(Command(
                source="voice", text=f"voice-{index}", action="DICTATE"
            ))
        denied_voice = engine._degraded_denied_total
        await engine._emit(Command(source="gesture", text="overflow", action="SCROLL"))
        assert engine._degraded_denied_total == denied_voice + 1

        await engine._emit(Command(source="touch", text="direct-1", action="CLICK"))
        await engine._emit(Command(source="multimodal", text="direct-2", action="CLICK"))
        assert len(engine._route_tasks) == 6

        await engine._emit(Command(source="touch", text="hard", action="CLICK"))
        assert len(engine._route_tasks) == 6
        assert engine._degraded_reason_counts["denied_hard_limit"] == 1
    finally:
        release.set()
        if engine._route_tasks:
            await asyncio.gather(*engine._route_tasks, return_exceptions=True)


async def test_degraded_exact_gesture_duplicate_coalesces_while_primary_active():
    engine = _engine()
    release = asyncio.Event()

    async def _blocked(_cmd):
        await release.wait()
        return "done"

    engine._coordinator.route = AsyncMock(side_effect=_blocked)
    command = Command(
        source="gesture", text="scroll", action="SCROLL", params={"dy": 3}
    )
    try:
        await engine._emit(command)
        await engine._emit(Command(
            source="gesture", text="scroll", action="SCROLL", params={"dy": 3}
        ))
        assert len(engine._route_tasks) == 1
        assert engine._degraded_coalesced_total == 1
        assert engine._coordinator.route.call_count == 1
    finally:
        release.set()
        if engine._route_tasks:
            await asyncio.gather(*engine._route_tasks, return_exceptions=True)
