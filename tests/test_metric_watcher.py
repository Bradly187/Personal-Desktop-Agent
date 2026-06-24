"""Tests for monitoring.metric_watcher — threshold alerting coroutine.

Run:
    python -m pytest tests/test_metric_watcher.py -q
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from monitoring.metric_watcher import MetricWatcher, _breached, _recovered, _ThresholdState

_PATCH_GET_METRICS = "monitoring.metrics.get_metrics"


# ---------------------------------------------------------------------------
# Unit tests for threshold math
# ---------------------------------------------------------------------------

def test_breached_gt():
    s = _ThresholdState("cloud_escalation_rate", ">", 0.40, "msg")
    assert _breached(s, 0.41)
    assert not _breached(s, 0.40)
    assert not _breached(s, 0.39)


def test_breached_lt():
    s = _ThresholdState("success_rate_1m", "<", 0.70, "msg")
    assert _breached(s, 0.69)
    assert not _breached(s, 0.70)
    assert not _breached(s, 0.71)


def test_recovered_gt():
    s = _ThresholdState("latency_ema_ms", ">", 3000.0, "msg")
    s.fired = True
    # 10% hysteresis → must drop to ≤ 3000 - 300 = 2700
    assert _recovered(s, 2700.0)  # boundary: recovered (<=)
    assert _recovered(s, 2699.0)
    assert not _recovered(s, 2701.0)
    assert not _recovered(s, 2900.0)


def test_recovered_lt():
    s = _ThresholdState("success_rate_1m", "<", 0.70, "msg")
    s.fired = True
    # 10% hysteresis → must rise to ≥ 0.70 + 0.07 = 0.77
    assert _recovered(s, 0.77)   # boundary: recovered (>=)
    assert _recovered(s, 0.78)
    assert not _recovered(s, 0.769)
    assert not _recovered(s, 0.71)


# ---------------------------------------------------------------------------
# Integration tests (async, patching metrics singleton)
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_event_bus():
    bus = MagicMock()
    bus.publish = AsyncMock()
    return bus


@pytest.mark.asyncio
async def test_alert_fires_on_breach(fake_event_bus):
    watcher = MetricWatcher(event_bus=fake_event_bus, interval_s=0)
    snapshot = {"gauges": {"cloud_escalation_rate": 0.50}}

    with patch(_PATCH_GET_METRICS) as mock_gm:
        mock_gm.return_value.get_snapshot.return_value = snapshot
        await watcher._check()

    assert fake_event_bus.publish.called
    call_kwargs = fake_event_bus.publish.call_args
    assert call_kwargs[0][0] == "metric.threshold_crossed"
    payload = call_kwargs[1]["payload"]
    assert payload["metric"] == "cloud_escalation_rate"
    assert payload["value"] == pytest.approx(0.50)


@pytest.mark.asyncio
async def test_alert_fires_once_until_recovery(fake_event_bus):
    watcher = MetricWatcher(event_bus=fake_event_bus, interval_s=0)

    with patch(_PATCH_GET_METRICS) as mock_gm:
        # First check: above threshold → fires
        mock_gm.return_value.get_snapshot.return_value = {
            "gauges": {"cloud_escalation_rate": 0.50}
        }
        await watcher._check()
        count_after_first = fake_event_bus.publish.call_count
        assert count_after_first >= 1

        # Second check: still above → should NOT fire again
        await watcher._check()
        assert fake_event_bus.publish.call_count == count_after_first

        # Third check: recovered (≤ 0.40 - 10% band = 0.36)
        mock_gm.return_value.get_snapshot.return_value = {
            "gauges": {"cloud_escalation_rate": 0.30}
        }
        await watcher._check()
        assert fake_event_bus.publish.call_count == count_after_first + 1

        # Fourth check: breaches again → fires once more
        mock_gm.return_value.get_snapshot.return_value = {
            "gauges": {"cloud_escalation_rate": 0.50}
        }
        await watcher._check()
        assert fake_event_bus.publish.call_count == count_after_first + 2


@pytest.mark.asyncio
async def test_no_alert_when_metric_absent(fake_event_bus):
    watcher = MetricWatcher(event_bus=fake_event_bus, interval_s=0)
    with patch(_PATCH_GET_METRICS) as mock_gm:
        mock_gm.return_value.get_snapshot.return_value = {"gauges": {}}
        await watcher._check()

    assert not fake_event_bus.publish.called


@pytest.mark.asyncio
async def test_no_event_bus_does_not_raise():
    watcher = MetricWatcher(event_bus=None, interval_s=0)
    with patch(_PATCH_GET_METRICS) as mock_gm:
        mock_gm.return_value.get_snapshot.return_value = {
            "gauges": {"cloud_escalation_rate": 0.90}
        }
        await watcher._check()  # must not raise


@pytest.mark.asyncio
async def test_run_loop_cancels_cleanly(fake_event_bus):
    watcher = MetricWatcher(event_bus=fake_event_bus, interval_s=9999)
    task = asyncio.create_task(watcher.run())
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert task.done()
