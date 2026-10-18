"""Compatibility tests for Phase 3 total-work scheduler bounds."""

from __future__ import annotations

import asyncio

import pytest

from core.scheduler import AccessibilityScheduler, Priority, SchedulerConfig


async def _idle():
    return None


def _config() -> SchedulerConfig:
    return SchedulerConfig(
        hard_limit=4,
        accessibility_reserve=1,
        non_accessibility_limit=3,
        dev_agent_limit=2,
        background_limit=1,
        max_parked_dev=1,
    )


def _drain(scheduler: AccessibilityScheduler) -> None:
    while not scheduler._queue.empty():
        item = scheduler._queue.get_nowait()
        item[2].close()


async def test_background_sheds_before_dev_limit():
    scheduler = AccessibilityScheduler(_config())
    first = scheduler.submit(_idle(), Priority.BACKGROUND)
    rejected = scheduler.submit(_idle(), Priority.BACKGROUND)
    with pytest.raises(asyncio.QueueFull, match="denied_background_limit"):
        rejected.result()
    assert scheduler.get_status()["shed_count"] == 1
    assert not first.done()
    await scheduler.stop()


async def test_accessibility_consumes_reserve_until_hard_ceiling():
    scheduler = AccessibilityScheduler(_config())
    non_access = [
        scheduler.submit(_idle(), priority)
        for priority in (Priority.BACKGROUND, Priority.DEV_AGENT, Priority.VOICE)
    ]
    reserve = scheduler.submit(_idle(), Priority.ACCESSIBILITY)
    assert not reserve.done()
    overflow = scheduler.submit(_idle(), Priority.ACCESSIBILITY)
    with pytest.raises(asyncio.QueueFull, match="denied_hard_limit"):
        overflow.result()
    assert scheduler.load_snapshot().queued_total == _config().hard_limit
    await scheduler.stop()
    assert all(future.cancelled() for future in [*non_access, reserve])


async def test_submit_plan_uses_dev_limit():
    scheduler = AccessibilityScheduler(_config())
    scheduler.submit(_idle(), Priority.BACKGROUND)
    scheduler.submit_plan(_idle(), label="first-plan")
    rejected = scheduler.submit_plan(_idle(), label="overflow-plan")
    with pytest.raises(asyncio.QueueFull, match="denied_dev_agent_limit"):
        rejected.result()
    await scheduler.stop()


async def test_under_capacity_admits_dev():
    scheduler = AccessibilityScheduler(_config())
    future = scheduler.submit(_idle(), Priority.DEV_AGENT, label="ok")
    assert not future.done()
    assert scheduler.get_status()["shed_count"] == 0
    await scheduler.stop()
