"""Tests for core.async_utils.fire_and_log — safe fire-and-forget scheduling."""

import asyncio

from core.async_utils import fire_and_log, _pending


def test_runs_coroutine_and_cleans_reference():
    async def run():
        ran = {}

        async def work():
            ran["done"] = True

        task = fire_and_log(work())
        assert task is not None
        assert task in _pending           # strong ref held while in flight
        await asyncio.sleep(0.01)
        assert ran.get("done") is True
        assert task not in _pending        # reference released on completion

    asyncio.run(run())


def test_exception_is_swallowed_and_logged_not_raised():
    async def run():
        async def boom():
            raise ValueError("telemetry exploded")

        task = fire_and_log(boom(), label="unit-write")
        await asyncio.sleep(0.01)          # let it run + done-callback fire
        assert task.done()
        assert isinstance(task.exception(), ValueError)  # captured, not propagated
        assert task not in _pending

    asyncio.run(run())  # must not raise


def test_no_running_loop_is_safe():
    async def work():
        return 1

    coro = work()
    task = fire_and_log(coro)  # sync context: no running loop — must not raise
    if task is None:
        assert coro.cr_frame is None  # coroutine closed, not leaked
    else:
        task.cancel()
