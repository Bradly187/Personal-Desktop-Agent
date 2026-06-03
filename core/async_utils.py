"""Helpers for fire-and-forget async work.

Non-critical background coroutines (telemetry, analytics writes) are scheduled
without awaiting. `fire_and_log` schedules one *safely*:

  - holds a strong reference until completion, so the task can't be
    garbage-collected mid-flight (CPython only weakly references bare tasks);
  - logs any exception at DEBUG instead of leaking an asyncio
    "Task exception was never retrieved" warning;
  - no-ops (closing the coroutine) when no event loop is running, rather than
    raising RuntimeError from a synchronous call site.

Use for writes whose failure is tolerable (the pipeline must not crash because a
telemetry row didn't persist). Do NOT use for work whose result is needed.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Coroutine, Optional, Set

_log = logging.getLogger(__name__)

# Module-level strong references to in-flight fire-and-forget tasks.
_pending: Set[asyncio.Future] = set()


def fire_and_log(
    coro: "Coroutine",
    logger: Optional[logging.Logger] = None,
    label: str = "background task",
) -> Optional[asyncio.Future]:
    """Schedule `coro` fire-and-forget; log (don't raise) on failure.

    Returns the scheduled task, or None if there was no running event loop
    (in which case the coroutine is closed to avoid a 'never awaited' warning).
    """
    lg = logger or _log
    try:
        task = asyncio.ensure_future(coro)
    except RuntimeError:
        coro.close()
        return None

    _pending.add(task)

    def _done(t: asyncio.Future) -> None:
        _pending.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            lg.debug("%s failed: %s", label, exc)

    task.add_done_callback(_done)
    return task
