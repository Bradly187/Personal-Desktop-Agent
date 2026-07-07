"""Tests for scheduler-integrated resource control (orchestration gap #3).

A flare must pause NEW dev/background admission (so freed VRAM isn't immediately
re-consumed by queued heavy work) while leaving the fast accessibility path
ungated. Recovery resumes admission. Covers the scheduler gate primitive and the
governor → scheduler wiring on flare start/end/stop.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent))

from core.scheduler import AccessibilityScheduler, Priority
from core.resource_governor import ResourceGovernor


# ---------------------------------------------------------------------------
# Scheduler pause/resume primitive
# ---------------------------------------------------------------------------

async def test_pause_blocks_dev_admission_resume_releases():
    sched = AccessibilityScheduler()
    await sched.start()
    try:
        sched.pause_dev()
        assert sched.dev_paused is True

        started = asyncio.Event()

        async def _dev_work():
            started.set()
            return "done"

        fut = sched.submit(_dev_work(), Priority.DEV_AGENT)
        # Parked at the admission gate — the body must not start.
        await asyncio.sleep(0.05)
        assert not started.is_set()
        assert not fut.done()

        sched.resume_dev()
        assert await asyncio.wait_for(fut, timeout=2.0) == "done"
        assert started.is_set()
    finally:
        await sched.stop()


async def test_pause_does_not_gate_accessibility():
    """Accessibility/voice/gesture must run even while dev admission is paused."""
    sched = AccessibilityScheduler()
    await sched.start()
    try:
        sched.pause_dev()
        fut = sched.submit(_ok(), Priority.ACCESSIBILITY)
        assert await asyncio.wait_for(fut, timeout=2.0) == "ok"
    finally:
        await sched.stop()


async def test_pause_resume_idempotent():
    sched = AccessibilityScheduler()
    sched.pause_dev()
    sched.pause_dev()
    assert sched.dev_paused is True
    sched.resume_dev()
    sched.resume_dev()
    assert sched.dev_paused is False


async def _ok():
    return "ok"


# ---------------------------------------------------------------------------
# Governor → scheduler wiring
# ---------------------------------------------------------------------------

class _FakeMemory:
    def __init__(self, score=0.0):
        self._score = score

    def get_pain_day_score(self):
        return self._score


def _governor_with_scheduler(score=0.0):
    gov = ResourceGovernor(memory=_FakeMemory(score))
    gov._post_keepalive = lambda *a, **k: None   # no live Ollama
    sched = AccessibilityScheduler()
    gov.set_scheduler(sched)
    return gov, sched


async def test_flare_start_pauses_scheduler():
    gov, sched = _governor_with_scheduler()
    await gov._on_flare_start(0.7)
    assert sched.dev_paused is True
    assert gov.get_status()["dev_paused"] is True


async def test_flare_end_resumes_scheduler():
    gov, sched = _governor_with_scheduler()
    await gov._on_flare_start(0.7)
    assert sched.dev_paused is True
    await gov._on_flare_end(0.3)
    assert sched.dev_paused is False


async def test_stop_never_leaves_scheduler_paused():
    gov, sched = _governor_with_scheduler()
    await gov.start()
    await gov._on_flare_start(0.7)
    assert sched.dev_paused is True
    await gov.stop()
    assert sched.dev_paused is False


async def test_governor_status_includes_vram_and_pause():
    gov, sched = _governor_with_scheduler()
    st = gov.get_status()
    assert "vram_free_gb" in st
    assert st["dev_paused"] is False   # scheduler wired, not paused
