"""Phase 2 watchdog coverage for authoritative scheduler load reporting."""

from types import SimpleNamespace

from core.scheduler import AccessibilityScheduler, Priority
from main import _watchdog_route_load
from monitoring.metrics import Metrics


async def _idle():
    return None


async def test_watchdog_reads_scheduler_snapshot_and_keeps_fallback_separate():
    metrics = Metrics()
    scheduler = AccessibilityScheduler()
    scheduler.set_metrics(metrics)
    futures = [
        scheduler.submit(_idle(), Priority.VOICE),
        scheduler.submit(_idle(), Priority.BACKGROUND),
    ]
    fallback_task = object()
    fusion = SimpleNamespace(_route_tasks={fallback_task})
    assert metrics.set_many(
        {"fusion_degraded_route_tasks_inflight": 1.0}, blocking=False
    )

    assert _watchdog_route_load(fusion, scheduler) == (2, 0, 1)
    assert _watchdog_route_load(fusion, None) == (0, 0, 1)
    gauges = metrics.get_snapshot()["gauges"]
    assert gauges["scheduler_queued_total"] == 2
    assert gauges["fusion_degraded_route_tasks_inflight"] == 1.0
    assert gauges["coordinator_route_tasks_inflight"] == 3.0

    fusion._route_tasks.remove(fallback_task)
    assert metrics.set_many(
        {"fusion_degraded_route_tasks_inflight": 0.0}, blocking=False
    )
    assert metrics.get_snapshot()["gauges"]["coordinator_route_tasks_inflight"] == 2.0

    await scheduler.stop()
    assert all(future.cancelled() for future in futures)
    assert _watchdog_route_load(fusion, scheduler) == (0, 0, 0)
    assert metrics.get_snapshot()["gauges"]["coordinator_route_tasks_inflight"] == 0.0
