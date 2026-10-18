"""Phase 3 tests for bounded degraded Fusion dispatch."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from core.command_executor import Command
from core.fusion_engine import FusionEngine
from core.scheduler import Priority, SchedulerConfig
from monitoring.metrics import Metrics


def _engine(config: SchedulerConfig | None = None) -> tuple[FusionEngine, AsyncMock, Metrics]:
    engine = FusionEngine(screen_width=1920, screen_height=1080)
    if config is not None:
        engine._scheduler_config = config
    metrics = Metrics()
    engine.set_metrics(metrics)
    coordinator = AsyncMock()
    engine.set_coordinator(coordinator)
    return engine, coordinator, metrics


def _tiny() -> SchedulerConfig:
    return SchedulerConfig(
        hard_limit=5,
        accessibility_reserve=2,
        non_accessibility_limit=3,
        dev_agent_limit=2,
        background_limit=1,
        max_parked_dev=2,
    )


def test_production_defaults_preserve_measured_evidence_anchors():
    config = SchedulerConfig()
    assert config.background_limit == 10
    assert config.max_parked_dev == config.dev_agent_limit == 16
    assert config.non_accessibility_limit == 21
    assert config.hard_limit == 256
    assert config.accessibility_reserve == 16


@pytest.mark.asyncio
async def test_degraded_non_accessibility_limit_denies_without_creating_coroutine():
    engine, coordinator, metrics = _engine(_tiny())
    engine._degraded_by_priority[Priority.VOICE] = 3

    await engine._emit(Command(source="voice", text="unique", action="DICTATE"))

    coordinator.route.assert_not_called()
    assert engine._route_breaker_open is True
    assert engine._degraded_denied_total == 1
    gauges = metrics.get_snapshot()["gauges"]
    assert gauges["fusion_degraded_reason_denied_accessibility_reserve"] == 1.0


@pytest.mark.asyncio
async def test_degraded_background_sheds_before_dev_and_fast_lane():
    engine, coordinator, _ = _engine(_tiny())
    release = asyncio.Event()

    async def _blocked(_cmd):
        await release.wait()

    coordinator.route = AsyncMock(side_effect=_blocked)
    try:
        engine._degraded_by_priority[Priority.BACKGROUND] = 1
        # The command source mapper does not produce heavy tiers; exercise the
        # shared pure decision state directly and then prove voice remains open.
        from core.scheduler import decide_admission
        background = decide_admission(_tiny(), engine._degraded_by_priority, Priority.BACKGROUND)
        assert background.reason == "denied_background_limit"

        await engine._emit(Command(source="voice", text="fast", action="DICTATE"))
        assert len(engine._route_tasks) == 1
    finally:
        release.set()
        if engine._route_tasks:
            await asyncio.gather(*engine._route_tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_degraded_task_gauge_and_compatibility_total_drain_to_zero():
    engine, coordinator, metrics = _engine(_tiny())
    release = asyncio.Event()

    async def _blocked(_cmd):
        await release.wait()

    coordinator.route = AsyncMock(side_effect=_blocked)
    await engine._emit(Command(source="voice", text="one", action="DICTATE"))
    assert metrics.get_snapshot()["gauges"]["coordinator_route_tasks_inflight"] == 1.0

    release.set()
    for _ in range(20):
        if not engine._route_tasks:
            break
        await asyncio.sleep(0)
    gauges = metrics.get_snapshot()["gauges"]
    assert gauges["fusion_degraded_route_tasks_inflight"] == 0.0
    assert gauges["coordinator_route_tasks_inflight"] == 0.0


def test_metrics_predeclare_degraded_admission_reasons():
    gauges = Metrics()._gauges
    expected = {
        "fusion_degraded_route_tasks_inflight",
        "fusion_degraded_denied_total",
        "fusion_degraded_coalesced_total",
        "fusion_voice_exact_coalesced_total",
        "fusion_degraded_reason_denied_background_limit",
        "fusion_degraded_reason_denied_dev_agent_limit",
        "fusion_degraded_reason_denied_accessibility_reserve",
        "fusion_degraded_reason_denied_hard_limit",
        "fusion_degraded_reason_coalesced_exact_key",
    }
    assert expected <= gauges.keys()
