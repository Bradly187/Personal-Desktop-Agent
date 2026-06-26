"""MetricWatcher — background coroutine that alerts on KPI threshold crossings.

Polls `monitoring.metrics.get_snapshot()` every `interval_s` seconds and
publishes a `metric.threshold_crossed` EventBus topic whenever a metric
breaches its configured threshold.

Default thresholds (can be overridden via DA_METRIC_THRESHOLDS JSON env var):
  cloud_escalation_rate > 0.40  — too many local failures, check Ollama
  success_rate_1m       < 0.70  — accuracy degrading
  latency_ema_ms        > 3000  — P50 latency well above budget
  scheduler_queue_depth > 20    — scheduler backing up

Threshold crossings are edge-triggered: a second alert for the same metric
fires only after the metric recovers past the threshold by at least the
`hysteresis` fraction (default 10%) of the threshold value, preventing
alert storms on values hovering at the boundary.

Wire-up in main.py::_run_pipeline:
    watcher = MetricWatcher(event_bus)
    watcher_task = asyncio.create_task(watcher.run())
    ...
    watcher_task.cancel(); await watcher_task
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

_DEFAULT_THRESHOLDS: "list[dict]" = [
    {"metric": "cloud_escalation_rate", "op": ">", "value": 0.40,
     "message": "Cloud escalation rate {value:.0%} — check Ollama or VRAM"},
    {"metric": "success_rate_1m",       "op": "<", "value": 0.70,
     "message": "Success rate {value:.0%} over last 60 commands"},
    {"metric": "latency_ema_ms",        "op": ">", "value": 3000.0,
     "message": "Latency EMA {value:.0f} ms — system under load"},
    {"metric": "scheduler_queue_depth", "op": ">", "value": 20,
     "message": "Scheduler queue depth {value} — possible coordinator stall"},
]

_HYSTERESIS = 0.10  # 10% recovery band before re-alerting


@dataclass
class _ThresholdState:
    metric: str
    op: str           # ">" or "<"
    value: float
    message: str
    fired: bool = False
    last_observed: Optional[float] = None


def _load_thresholds() -> "list[_ThresholdState]":
    raw = _DEFAULT_THRESHOLDS.copy()
    env = os.environ.get("DA_METRIC_THRESHOLDS")
    if env:
        try:
            raw = json.loads(env)
        except Exception as exc:
            log.warning("DA_METRIC_THRESHOLDS parse error (%s) — using defaults", exc)
    states = []
    for item in raw:
        try:
            states.append(_ThresholdState(
                metric=item["metric"],
                op=item["op"],
                value=float(item["value"]),
                message=item.get("message", "threshold crossed"),
            ))
        except Exception as exc:
            log.warning("Skipping malformed threshold entry %s: %s", item, exc)
    return states


def _breached(state: _ThresholdState, current: float) -> bool:
    if state.op == ">":
        return current > state.value
    if state.op == "<":
        return current < state.value
    return False


def _recovered(state: _ThresholdState, current: float) -> bool:
    """True when the metric has moved back through the hysteresis band."""
    band = abs(state.value) * _HYSTERESIS
    if state.op == ">":
        return current <= state.value - band
    if state.op == "<":
        return current >= state.value + band
    return True


class MetricWatcher:
    """Poll in-process metrics and publish EventBus alerts on threshold crossings."""

    def __init__(self, event_bus=None, interval_s: float = 60.0) -> None:
        self._event_bus = event_bus
        self._interval_s = interval_s
        self._states = _load_thresholds()

    async def run(self) -> None:
        """Main loop — runs until cancelled."""
        log.info(
            "MetricWatcher started: %d thresholds, interval=%.0fs",
            len(self._states), self._interval_s,
        )
        while True:
            try:
                await asyncio.sleep(self._interval_s)
                await self._check()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.warning("MetricWatcher check error: %s", exc)

    async def _check(self) -> None:
        try:
            from monitoring.metrics import get_metrics
            snapshot = get_metrics().get_snapshot()
        except Exception:
            return

        gauges = snapshot.get("gauges", {})

        for state in self._states:
            current = gauges.get(state.metric)
            if current is None:
                continue
            current = float(current)
            state.last_observed = current

            if not state.fired and _breached(state, current):
                state.fired = True
                msg = state.message.format(value=current)
                log.warning("METRIC ALERT %s: %s", state.metric, msg)
                await self._publish(state.metric, current, msg)

            elif state.fired and _recovered(state, current):
                state.fired = False
                log.info("METRIC RECOVERED %s: %.3f", state.metric, current)
                await self._publish(state.metric, current,
                                    f"{state.metric} recovered ({current:.3f})",
                                    severity="info")

    async def _publish(
        self,
        metric: str,
        value: float,
        message: str,
        severity: str = "warning",
    ) -> None:
        if self._event_bus is None:
            return
        try:
            from core.events import TOPIC_METRIC_THRESHOLD
            await self._event_bus.publish(
                TOPIC_METRIC_THRESHOLD,
                source="metric_watcher",
                payload={"metric": metric, "value": value,
                         "message": message, "severity": severity},
            )
        except Exception as exc:
            log.debug("MetricWatcher publish failed: %s", exc)
