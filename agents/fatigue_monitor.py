"""agents/fatigue_monitor.py — FatigueMonitorAgent (R-1 reference observer).

Watches two EventBus signals for the classic fatigue pattern:
  - command.executed   → latency_ms (a rising trend = the user is slowing / struggling)
  - voice.drift        → vocal clarity declining (only emitted when drift fires)

When command latency is trending up AND a recent voice-drift event is present, the
monitor is ADVISORY (per the locked design decision):
  1. publishes `fatigue.detected` on the bus (other rules/observers can react),
  2. suggests switching to direct touch control via the Notifier (Danielle + iPad),
  3. feeds a bounded fatigue signal into PainDayEngine
     (BehavioralTwinState.record_fatigue_signal) — the engine's hysteresis still
     OWNS pain-day activation; the monitor only contributes.

It never calls the ResourceGovernor directly and never forces the pain-day score.
A cooldown prevents notification spam.
"""

from __future__ import annotations

import logging
import time

from agents.observer_base import ObserverAgent

log = logging.getLogger(__name__)

TOPIC_FATIGUE_DETECTED = "fatigue.detected"


class FatigueMonitorAgent(ObserverAgent):
    # Latency EWMA smoothing (fast tracks recent, slow tracks baseline).
    _ALPHA_FAST = 0.30
    _ALPHA_SLOW = 0.02
    _MIN_SAMPLES = 8                 # need a baseline before judging a "rise"
    _LATENCY_RISE_FACTOR = 1.5       # fast EWMA this many× the slow baseline = rising
    _DRIFT_FRESH_S = 300.0           # a voice.drift within this window = "declining clarity"
    _FATIGUE_BAR = 0.30              # min fatigue level to notify / suggest touch
    _COOLDOWN_S = 300.0              # min seconds between user-facing suggestions

    def __init__(self, event_bus, notifier=None, twin_state=None, clock=time.monotonic) -> None:
        super().__init__(event_bus, name="fatigue_monitor")
        self._notifier = notifier
        self._twin = twin_state
        self._now = clock
        self._lat_fast: float = 0.0
        self._lat_slow: float = 0.0
        self._samples: int = 0
        self._last_drift_ts: float = -1e9
        self._last_drift_pct: float = 0.0
        self._last_notify_ts: float = -1e9

    def topics(self) -> list[str]:
        return ["command.%", "voice.drift"]

    async def on_event(self, evt: dict) -> None:
        topic = evt.get("topic", "")
        payload = evt.get("payload") or {}
        if topic == "voice.drift":
            self._last_drift_ts = self._now()
            self._last_drift_pct = float(payload.get("drift_pct", 0.0) or 0.0)
        elif topic.startswith("command."):
            lat = payload.get("latency_ms")
            if lat is None:
                return
            self._observe_latency(float(lat))
        await self._evaluate()

    def _observe_latency(self, lat_ms: float) -> None:
        if self._samples == 0:
            self._lat_fast = self._lat_slow = lat_ms
        else:
            self._lat_fast += self._ALPHA_FAST * (lat_ms - self._lat_fast)
            self._lat_slow += self._ALPHA_SLOW * (lat_ms - self._lat_slow)
        self._samples += 1

    def _fatigue_level(self) -> float:
        """Compute current fatigue in [0,1]; 0 unless BOTH signals indicate fatigue."""
        if self._samples < self._MIN_SAMPLES or self._lat_slow <= 0.0:
            return 0.0
        drift_fresh = (self._now() - self._last_drift_ts) <= self._DRIFT_FRESH_S
        rising = self._lat_fast >= self._lat_slow * self._LATENCY_RISE_FACTOR
        if not (drift_fresh and rising):
            return 0.0
        latency_excess = max(0.0, (self._lat_fast / self._lat_slow) - 1.0)  # ≥0.5 when rising
        drift_sev = max(0.0, min(1.0, self._last_drift_pct / 100.0))
        return max(0.0, min(1.0, 0.6 * min(1.0, latency_excess) + 0.4 * drift_sev))

    async def _evaluate(self) -> None:
        level = self._fatigue_level()
        # Always feed PainDayEngine (0.0 lets a prior nudge decay) — advisory only.
        if self._twin is not None:
            try:
                self._twin.record_fatigue_signal(level)
            except Exception as exc:
                log.debug("FatigueMonitor: record_fatigue_signal failed: %s", exc)
        if level < self._FATIGUE_BAR:
            return
        if (self._now() - self._last_notify_ts) < self._COOLDOWN_S:
            return
        self._last_notify_ts = self._now()
        # Publish for other rules/observers.
        if self._bus is not None:
            try:
                await self._bus.publish(
                    TOPIC_FATIGUE_DETECTED,
                    {"level": round(level, 3),
                     "latency_fast_ms": round(self._lat_fast, 1),
                     "latency_slow_ms": round(self._lat_slow, 1),
                     "drift_pct": round(self._last_drift_pct, 1)},
                    source="fatigue_monitor",
                )
            except Exception as exc:
                log.debug("FatigueMonitor: fatigue.detected publish failed: %s", exc)
        # Suggest direct touch control (advisory).
        if self._notifier is not None:
            try:
                await self._notifier.notify(
                    "You seem to be tiring",
                    "Commands are slowing and your voice is softening — "
                    "switching to direct touch control may be easier right now.",
                )
            except Exception as exc:
                log.debug("FatigueMonitor: notify failed: %s", exc)
        log.info("FatigueMonitor: fatigue detected (level=%.2f) — advised touch control", level)
