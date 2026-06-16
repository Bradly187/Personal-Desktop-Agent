"""PR 1 (R-1) — FatigueMonitorAgent advisory logic.

Drives on_event directly with an injected clock for determinism: rising latency +
fresh voice drift fires (publish + notify + PainDayEngine signal); neither signal
alone fires; cooldown holds.
"""
import pytest

from agents.fatigue_monitor import FatigueMonitorAgent, TOPIC_FATIGUE_DETECTED


class _RecorderBus:
    def __init__(self):
        self.published = []

    async def publish(self, topic, payload, source, **kw):
        self.published.append((topic, payload, source))


class _FakeTwin:
    def __init__(self):
        self.signals = []

    def record_fatigue_signal(self, value):
        self.signals.append(value)


class _FakeNotifier:
    def __init__(self):
        self.calls = []

    async def notify(self, title, body="", **kw):
        self.calls.append((title, body))


def _evt(topic, payload):
    return {"topic": topic, "payload": payload}


async def _build():
    clock = {"t": 1000.0}
    bus, twin, notifier = _RecorderBus(), _FakeTwin(), _FakeNotifier()
    fm = FatigueMonitorAgent(bus, notifier=notifier, twin_state=twin,
                             clock=lambda: clock["t"])
    return fm, bus, twin, notifier, clock


async def _warm_baseline(fm, n=8, lat=100.0):
    for _ in range(n):
        await fm.on_event(_evt("command.executed", {"latency_ms": lat}))


async def test_rising_latency_plus_drift_fires():
    fm, bus, twin, notifier, clock = await _build()
    await _warm_baseline(fm)
    await fm.on_event(_evt("voice.drift", {"drift_pct": 50.0}))   # fresh drift
    await fm.on_event(_evt("command.executed", {"latency_ms": 500.0}))  # latency spikes

    assert twin.signals and twin.signals[-1] > 0.0
    assert any(t == TOPIC_FATIGUE_DETECTED for t, _, _ in bus.published)
    assert len(notifier.calls) == 1
    assert "touch" in notifier.calls[0][1].lower()


async def test_latency_without_drift_does_not_fire():
    fm, bus, twin, notifier, clock = await _build()
    await _warm_baseline(fm)
    for _ in range(3):
        await fm.on_event(_evt("command.executed", {"latency_ms": 500.0}))
    assert notifier.calls == []
    assert all(t != TOPIC_FATIGUE_DETECTED for t, _, _ in bus.published)
    assert twin.signals[-1] == 0.0


async def test_drift_without_rising_latency_does_not_fire():
    fm, bus, twin, notifier, clock = await _build()
    await _warm_baseline(fm)
    await fm.on_event(_evt("voice.drift", {"drift_pct": 80.0}))
    await fm.on_event(_evt("command.executed", {"latency_ms": 100.0}))  # flat
    assert notifier.calls == []
    assert twin.signals[-1] == 0.0


async def test_cooldown_holds_then_releases():
    fm, bus, twin, notifier, clock = await _build()
    await _warm_baseline(fm)
    await fm.on_event(_evt("voice.drift", {"drift_pct": 50.0}))
    await fm.on_event(_evt("command.executed", {"latency_ms": 500.0}))
    assert len(notifier.calls) == 1

    # Immediate re-trigger within cooldown → no second notification.
    await fm.on_event(_evt("command.executed", {"latency_ms": 600.0}))
    assert len(notifier.calls) == 1

    # Advance past cooldown and re-arm drift → fires again.
    clock["t"] += fm._COOLDOWN_S + 1.0
    await fm.on_event(_evt("voice.drift", {"drift_pct": 50.0}))
    await fm.on_event(_evt("command.executed", {"latency_ms": 700.0}))
    assert len(notifier.calls) == 2


async def test_topics():
    fm, *_ = await _build()
    assert "command.%" in fm.topics()
    assert "voice.drift" in fm.topics()
