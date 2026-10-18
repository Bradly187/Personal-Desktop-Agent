"""Deterministic Phase 3 admission, coalescing, and staleness tests."""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError

import pytest

from core.scheduler import (
    AccessibilityScheduler,
    AdmissionDecision,
    Priority,
    SchedulerConfig,
)


def _tiny_config() -> SchedulerConfig:
    return SchedulerConfig(
        hard_limit=6,
        accessibility_reserve=2,
        non_accessibility_limit=4,
        dev_agent_limit=3,
        background_limit=1,
        max_parked_dev=2,
        voice_stale_after_s=1.0,
        gesture_stale_after_s=1.0,
    )


async def _value(value):
    return value


def _assert_denied(future: asyncio.Future, reason: str) -> None:
    assert future.done()
    with pytest.raises(asyncio.QueueFull, match=reason):
        future.result()


def test_config_and_decision_are_frozen_and_validated():
    config = _tiny_config()
    decision = AdmissionDecision(True, None)
    with pytest.raises(FrozenInstanceError):
        config.hard_limit = 7
    with pytest.raises(FrozenInstanceError):
        decision.accepted = False
    with pytest.raises(ValueError):
        SchedulerConfig(hard_limit=4, accessibility_reserve=4)


@pytest.mark.asyncio
async def test_mixed_saturation_sheds_background_then_dev_and_preserves_reserve():
    scheduler = AccessibilityScheduler(_tiny_config())

    bg_primary = scheduler.submit(_value("bg"), Priority.BACKGROUND)
    bg_rejected_coro = _value("bg-overflow")
    bg_rejected = scheduler.submit(bg_rejected_coro, Priority.BACKGROUND)
    _assert_denied(bg_rejected, "denied_background_limit")
    assert bg_rejected_coro.cr_frame is None

    dev_futures = [
        scheduler.submit(_value(index), Priority.DEV_AGENT)
        for index in range(2)
    ]
    dev_rejected = scheduler.submit(_value("dev-overflow"), Priority.DEV_AGENT)
    _assert_denied(dev_rejected, "denied_dev_agent_limit")

    voice = scheduler.submit(_value("voice"), Priority.VOICE)
    gesture_rejected = scheduler.submit(_value("gesture"), Priority.GESTURE)
    _assert_denied(gesture_rejected, "denied_accessibility_reserve")

    accessibility = [
        scheduler.submit(_value(index), Priority.ACCESSIBILITY)
        for index in range(2)
    ]
    hard_rejected = scheduler.submit(_value("hard"), Priority.ACCESSIBILITY)
    _assert_denied(hard_rejected, "denied_hard_limit")

    snapshot = scheduler.load_snapshot()
    assert snapshot.queued_total == _tiny_config().hard_limit
    assert snapshot.running_total == 0
    assert snapshot.denied_total == 4
    assert snapshot.reason_counts["denied_background_limit"] == 1
    assert snapshot.reason_counts["denied_dev_agent_limit"] == 1
    assert snapshot.reason_counts["denied_accessibility_reserve"] == 1
    assert snapshot.reason_counts["denied_hard_limit"] == 1

    await scheduler.stop()
    assert all(
        future.cancelled()
        for future in [bg_primary, *dev_futures, voice, *accessibility]
    )


@pytest.mark.asyncio
async def test_slow_mixed_storm_never_exceeds_hard_total():
    scheduler = AccessibilityScheduler(_tiny_config())
    release = asyncio.Event()
    started = 0

    async def _slow():
        nonlocal started
        started += 1
        await release.wait()
        return "done"

    await scheduler.start()
    accepted = []
    rejected = []
    try:
        priorities = [
            Priority.BACKGROUND,
            Priority.DEV_AGENT,
            Priority.DEV_AGENT,
            Priority.VOICE,
            Priority.GESTURE,
            Priority.ACCESSIBILITY,
            Priority.ACCESSIBILITY,
            Priority.ACCESSIBILITY,
        ]
        for priority in priorities:
            future = scheduler.submit(_slow(), priority, source=priority.name.lower())
            (rejected if future.done() else accepted).append(future)
            snapshot = scheduler.load_snapshot()
            assert snapshot.queued_total + snapshot.running_total <= _tiny_config().hard_limit

        for _ in range(20):
            if scheduler.load_snapshot().running_total == len(accepted):
                break
            await asyncio.sleep(0)
        snapshot = scheduler.load_snapshot()
        assert snapshot.running_total == len(accepted) == _tiny_config().hard_limit
        await asyncio.sleep(0)
        # Heavy work waiting on the single permit remains authoritatively running;
        # one heavy body plus all three fast-lane bodies have actually started.
        assert started == 4
        assert rejected

        release.set()
        assert await asyncio.gather(*accepted) == ["done"] * len(accepted)
        assert scheduler.load_snapshot().running_total == 0
    finally:
        release.set()
        await scheduler.stop()


@pytest.mark.asyncio
async def test_exact_duplicate_returns_independent_proxy_and_closes_new_coroutine():
    scheduler = AccessibilityScheduler(_tiny_config())
    release = asyncio.Event()

    async def _primary():
        await release.wait()
        return "primary-result"

    primary = scheduler.submit(
        _primary(), Priority.VOICE, source="voice", coalesce_key=("OPEN", "chrome")
    )
    duplicate_coro = _value("must-not-run")
    proxy = scheduler.submit(
        duplicate_coro,
        Priority.VOICE,
        source="voice",
        coalesce_key=("OPEN", "chrome"),
    )
    second_proxy = scheduler.submit(
        _value("also-must-not-run"),
        Priority.VOICE,
        source="voice",
        coalesce_key=("OPEN", "chrome"),
    )

    assert duplicate_coro.cr_frame is None
    assert scheduler.load_snapshot().queued_total == 1
    assert scheduler.load_snapshot().coalesced_total == 2
    proxy.cancel()
    assert not primary.cancelled()

    await scheduler.start()
    try:
        await asyncio.sleep(0)
        release.set()
        assert await primary == "primary-result"
        assert await second_proxy == "primary-result"
        assert proxy.cancelled()
        assert scheduler.load_snapshot().queued_total == 0
        assert scheduler.load_snapshot().running_total == 0
        assert not scheduler._active_coalesced
    finally:
        release.set()
        await scheduler.stop()


@pytest.mark.asyncio
async def test_coalescing_requires_exact_explicit_voice_or_gesture_identity():
    scheduler = AccessibilityScheduler(_tiny_config())
    futures = [
        scheduler.submit(_value("a"), Priority.VOICE, source="voice", coalesce_key="a"),
        scheduler.submit(_value("b"), Priority.VOICE, source="voice", coalesce_key="b"),
        scheduler.submit(_value("other-source"), Priority.VOICE, source="voice_local", coalesce_key="a"),
        scheduler.submit(_value("accessibility"), Priority.ACCESSIBILITY, source="touch", coalesce_key="a"),
    ]
    assert scheduler.load_snapshot().queued_total == 4
    assert scheduler.load_snapshot().coalesced_total == 0
    await scheduler.stop()
    assert all(future.cancelled() for future in futures)


@pytest.mark.asyncio
@pytest.mark.parametrize("priority", [Priority.VOICE, Priority.GESTURE])
async def test_stale_fast_lane_finalizes_without_executing(priority):
    scheduler = AccessibilityScheduler(_tiny_config())
    executed = False

    async def _work():
        nonlocal executed
        executed = True

    future = scheduler.submit(
        _work(),
        priority,
        source=priority.name.lower(),
        stale_after_s=0.0,
    )
    await scheduler.start()
    try:
        for _ in range(10):
            if future.done():
                break
            await asyncio.sleep(0)
        with pytest.raises(asyncio.TimeoutError, match="was not executed"):
            await future
        assert executed is False
        snapshot = scheduler.load_snapshot()
        assert snapshot.stale_total == 1
        assert snapshot.reason_counts["stale_before_dispatch"] == 1
        assert snapshot.queued_total == snapshot.running_total == 0
    finally:
        await scheduler.stop()
