"""Tests for scheduler queue bounds + priority-aware load shedding (gap #4).

DEV_AGENT/BACKGROUND submissions are shed when the queue is at capacity; the
fast accessibility/voice/gesture path is NEVER shed.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import core.scheduler as sched_mod
from core.scheduler import AccessibilityScheduler, Priority


async def _idle():
    return None


def _fill_queue(sched, n):
    """Enqueue n background coros WITHOUT starting the worker (so they stay queued)."""
    for i in range(n):
        sched.submit(_idle(), Priority.BACKGROUND, label=f"bg{i}")


async def test_dev_submission_shed_when_full(monkeypatch):
    monkeypatch.setattr(sched_mod, "_MAX_QUEUE_DEPTH", 4)
    sched = AccessibilityScheduler()              # worker NOT started → queue holds items
    _fill_queue(sched, 4)
    assert sched._queue.qsize() == 4

    fut = sched.submit(_idle(), Priority.DEV_AGENT, label="overflow")
    assert fut.done()
    with pytest.raises(asyncio.QueueFull):
        fut.result()
    assert sched.get_status()["shed_count"] == 1
    # Drain the un-awaited coroutines to avoid "never awaited" warnings.
    _drain(sched)


async def test_accessibility_never_shed(monkeypatch):
    monkeypatch.setattr(sched_mod, "_MAX_QUEUE_DEPTH", 2)
    sched = AccessibilityScheduler()
    _fill_queue(sched, 2)
    assert sched._queue.qsize() == 2

    # Even over capacity, an accessibility command is admitted (not shed).
    fut = sched.submit(_idle(), Priority.ACCESSIBILITY, label="user_click")
    assert not (fut.done() and fut.exception() is not None)
    assert sched._queue.qsize() == 3
    assert sched.get_status()["shed_count"] == 0
    _drain(sched)


async def test_submit_plan_shed_when_full(monkeypatch):
    monkeypatch.setattr(sched_mod, "_MAX_QUEUE_DEPTH", 1)
    sched = AccessibilityScheduler()
    _fill_queue(sched, 1)
    fut = sched.submit_plan(_idle(), label="big_plan")
    assert fut.done()
    with pytest.raises(asyncio.QueueFull):
        fut.result()
    _drain(sched)


async def test_under_capacity_admits_dev(monkeypatch):
    monkeypatch.setattr(sched_mod, "_MAX_QUEUE_DEPTH", 8)
    sched = AccessibilityScheduler()
    fut = sched.submit(_idle(), Priority.DEV_AGENT, label="ok")
    assert not fut.done()                          # queued, not shed
    assert sched.get_status()["shed_count"] == 0
    _drain(sched)


def _drain(sched):
    """Close out queued coroutines so pytest doesn't warn about un-awaited coros."""
    while not sched._queue.empty():
        item = sched._queue.get_nowait()
        coro = item[2]
        coro.close()
