"""Tests for the parked dev-task bound during a long flare (orchestration gap F).

When dev admission is paused (flare), dequeued dev tasks park in _run_dev_task.
Under the cap they park-and-resume; beyond the cap they are shed (coro closed,
future → QueueFull) so a long flare can't accumulate unbounded parked coroutines.
Accessibility never parks.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.scheduler import AccessibilityScheduler, Priority, SchedulerConfig


async def _idle():
    return "done"


async def test_parked_tasks_resume_under_cap():
    sched = AccessibilityScheduler()
    await sched.start()
    try:
        sched.pause_dev()
        futs = [sched.submit(_idle(), Priority.DEV_AGENT, label=f"d{i}") for i in range(3)]
        await asyncio.sleep(0.05)               # let the worker dispatch + park them
        assert sched.get_status()["dev_parked"] == 3
        assert all(not f.done() for f in futs)

        sched.resume_dev()
        results = await asyncio.wait_for(asyncio.gather(*futs), timeout=2.0)
        assert results == ["done", "done", "done"]
        assert sched.get_status()["dev_parked"] == 0
    finally:
        await sched.stop()


async def test_excess_parked_tasks_are_shed():
    config = SchedulerConfig(
        hard_limit=8,
        accessibility_reserve=2,
        non_accessibility_limit=6,
        dev_agent_limit=5,
        background_limit=2,
        max_parked_dev=2,
    )
    sched = AccessibilityScheduler(config)
    await sched.start()
    try:
        sched.pause_dev()
        futs = [sched.submit(_idle(), Priority.DEV_AGENT, label=f"d{i}") for i in range(5)]
        await asyncio.sleep(0.05)

        # Exactly 2 parked; the other 3 shed with QueueFull.
        assert sched.get_status()["dev_parked"] == 2
        shed = [f for f in futs if f.done()]
        assert len(shed) == 3
        for f in shed:
            with pytest.raises(asyncio.QueueFull):
                f.result()
        assert sched.get_status()["shed_count"] == 3

        # The 2 parked still resume cleanly.
        sched.resume_dev()
        parked = [f for f in futs if not f.cancelled() and (not f.done() or f.exception() is None)]
        done = await asyncio.wait_for(
            asyncio.gather(*[f for f in futs if not f.done()], return_exceptions=True),
            timeout=2.0,
        )
        assert all(r == "done" for r in done)
    finally:
        await sched.stop()


async def test_accessibility_never_parks_during_flare():
    sched = AccessibilityScheduler()
    await sched.start()
    try:
        sched.pause_dev()
        fut = sched.submit(_idle(), Priority.ACCESSIBILITY, label="click")
        assert await asyncio.wait_for(fut, timeout=2.0) == "done"
        assert sched.get_status()["dev_parked"] == 0
    finally:
        await sched.stop()


async def test_parked_count_resets_on_stop_cancel():
    """A parked task cancelled by stop() decrements the parked gauge (finally)."""
    sched = AccessibilityScheduler()
    await sched.start()
    sched.pause_dev()
    sched.submit(_idle(), Priority.DEV_AGENT, label="d")
    await asyncio.sleep(0.05)
    assert sched.get_status()["dev_parked"] == 1
    await sched.stop()                     # cancels the parked task
    assert sched._dev_parked == 0


async def test_parked_work_remains_authoritatively_running():
    sched = AccessibilityScheduler()
    await sched.start()
    sched.pause_dev()
    try:
        futures = [
            sched.submit(_idle(), Priority.DEV_AGENT, label=f"parked-{index}")
            for index in range(3)
        ]
        for _ in range(100):
            if sched.load_snapshot().parked_dev == 3:
                break
            await asyncio.sleep(0)

        snapshot = sched.load_snapshot()
        assert snapshot.queued_total == 0
        assert snapshot.running_total == 3
        assert snapshot.running_by_priority[Priority.DEV_AGENT] == 3
        assert snapshot.parked_dev == 3

        sched.resume_dev()
        assert await asyncio.wait_for(asyncio.gather(*futures), timeout=2.0) == [
            "done", "done", "done"
        ]
        assert sched.load_snapshot().running_total == 0
    finally:
        await sched.stop()
