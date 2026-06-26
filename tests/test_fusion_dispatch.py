"""Dispatch-path tests for FusionEngine._emit (Gap 5b).

The 60 Hz fusion loop hands a resolved Command to the coordinator via one of two
paths: through the AccessibilityScheduler's bounded priority pool when one is
wired, or — as an INTENTIONAL graceful-degradation fallback — a bare
fire-and-forget create_task when it is not (startup window, or the watchdog
deliberately set_scheduler(None) because the scheduler wedged). These pin the
dual-path contract and that the route-task circuit breaker bounds the fallback.
"""

import asyncio

import pytest
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


async def test_dispatch_with_scheduler_uses_submit_with_priority():
    e = _engine()
    # MagicMock (not Async) so building the work item never creates an un-awaited
    # coroutine — the scheduler is what would await it in production.
    e._coordinator.route = MagicMock(return_value="work-item")
    sched = MagicMock()
    e.set_scheduler(sched)

    await e._emit(Command(source="voice", text="hi", action="CLICK", trace_id="t2"))

    sched.submit.assert_called_once()
    args, kwargs = sched.submit.call_args
    assert args[0] == "work-item"          # the coordinator.route(cmd) work item
    assert kwargs.get("priority") is not None
    assert kwargs.get("trace_id") == "t2"


async def test_route_breaker_sheds_in_fallback_path():
    e = _engine()
    e._coordinator.route = AsyncMock()
    # Saturate the in-flight set past the breaker's open threshold (>20).
    fillers = [asyncio.ensure_future(asyncio.sleep(5)) for _ in range(25)]
    for t in fillers:
        e._route_tasks.add(t)
    try:
        await e._emit(Command(source="voice", text="hi", action="CLICK", trace_id="t3"))
        assert e._route_breaker_open is True       # breaker tripped
        e._coordinator.route.assert_not_called()    # command shed, not dispatched
    finally:
        for t in fillers:
            t.cancel()
