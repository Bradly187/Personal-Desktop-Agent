"""Tests for cluster/laptop offload remediations.

Covers:
- H1: ClusterHealthMonitor debounce/hysteresis (no single-blip eviction).
- H2: mark_suspect on-demand re-probe (thread-safe, deduped, no-op when unknown).
- M4: _ping returns (ok, reason, latency_ms) instead of a bare bool.
- C1: WhisperStream lazy local fallback when a remote (laptop) service fails.
- M3: DevAgent RAG falls back to local when the remote indexer returns empty.
"""

import asyncio

import numpy as np
import pytest

from core.cluster_config import ClusterConfig
from core.cluster_health import ClusterHealthMonitor


def _mon(fail: int = 2, recover: int = 1) -> ClusterHealthMonitor:
    cfg = ClusterConfig(enabled=True, laptop_whisper_url="http://x:8888")
    return ClusterHealthMonitor(cfg, fail_threshold=fail, recover_threshold=recover)


# ── H1: debounce ───────────────────────────────────────────────────────────

def test_single_failure_does_not_evict():
    m = _mon(fail=2)
    m._apply_probe("whisper", "u", True, "HTTP 200", 5.0)
    assert m.is_healthy("whisper") is True
    m._apply_probe("whisper", "u", False, "timeout", 2000.0)   # 1 miss
    assert m.is_healthy("whisper") is True                      # still up
    m._apply_probe("whisper", "u", False, "timeout", 2000.0)   # 2 misses
    assert m.is_healthy("whisper") is False                     # now down


def test_recovery_needs_recover_threshold():
    m = _mon(recover=1)
    m._apply_probe("whisper", "u", True, "HTTP 200", 5.0)
    assert m.is_healthy("whisper") is True


def test_transient_blip_streak_resets_on_agreeing_probe():
    m = _mon(fail=2)
    m._apply_probe("whisper", "u", True, "HTTP 200", 5.0)       # up
    m._apply_probe("whisper", "u", False, "timeout", 1.0)      # 1 miss
    m._apply_probe("whisper", "u", True, "HTTP 200", 5.0)       # agrees → streak reset
    m._apply_probe("whisper", "u", False, "timeout", 1.0)      # 1 miss again (not 2)
    assert m.is_healthy("whisper") is True                      # never evicted


def test_unknown_service_defaults_down():
    m = _mon()
    assert m.is_healthy("whisper") is False  # never probed → fail-safe local


# ── M4: probe returns a reason + latency ────────────────────────────────────

def test_ping_returns_reason_and_latency():
    m = _mon()
    ok, reason, dt_ms = m._ping("http://127.0.0.1:1/health")  # nothing listening
    assert ok is False
    assert isinstance(reason, str) and reason          # exception class name
    assert isinstance(dt_ms, float) and dt_ms >= 0.0


# ── start()/stop() lifecycle — guards the _loop method vs _evloop attr collision
# (a self._loop attribute would shadow the _loop() poll coroutine and crash start)
# ─────────────────────────────────────────────────────────────────────────────

def test_start_does_not_shadow_poll_loop(monkeypatch):
    async def run():
        m = _mon()
        monkeypatch.setattr(m, "_ping", lambda url: (True, "HTTP 200", 5.0))
        await m.start()          # must not raise (regression: _loop() was un-callable)
        assert m._task is not None and not m._task.done()
        assert m.is_healthy("whisper") is True
        await m.stop()
        assert m._task is None

    asyncio.run(run())


# ── H2: mark_suspect ────────────────────────────────────────────────────────

def test_mark_suspect_is_noop_without_loop_or_unknown_service():
    m = _mon()
    m._evloop = None
    m.mark_suspect("whisper")        # loop None → no-op, must not raise
    m._evloop = asyncio.new_event_loop()
    m.mark_suspect("does_not_exist")  # unknown service → no-op
    m._evloop.close()


def test_mark_suspect_triggers_immediate_reprobe(monkeypatch):
    async def run():
        m = _mon(fail=1)
        m._evloop = asyncio.get_running_loop()
        m._apply_probe("whisper", "u", True, "HTTP 200", 5.0)  # start healthy
        assert m.is_healthy("whisper") is True
        # Next probe will fail; fail_threshold=1 so one re-probe evicts it.
        monkeypatch.setattr(m, "_ping", lambda url: (False, "ConnectionRefusedError", 1.0))
        m.mark_suspect("whisper")
        await asyncio.sleep(0.1)  # let the scheduled _recheck run
        assert m.is_healthy("whisper") is False
        assert "whisper" not in m._recheck_pending  # cleared after re-probe

    asyncio.run(run())


# ── C1: WhisperStream lazy local fallback ───────────────────────────────────

class _FakeModel:
    def transcribe(self, audio, **kw):
        seg = type("S", (), {"text": "hello", "avg_logprob": -0.1,
                             "no_speech_prob": 0.1, "start": 0.0, "end": 1.0})()
        info = type("I", (), {"language": "en", "language_probability": 0.9,
                              "duration": 1.0})()
        return iter([seg]), info


class _FailingRemote:
    def transcribe(self, *a, **k):
        raise RuntimeError("laptop whisper down")


class _Health:
    def __init__(self):
        self.suspect = []
    def is_healthy(self, s):
        return True
    def mark_suspect(self, s):
        self.suspect.append(s)


def _ws():
    from sensors.whisper_stream import WhisperStream
    ws = WhisperStream.__new__(WhisperStream)
    ws._remote_client = _FailingRemote()
    ws._cluster_health = _Health()
    ws._model = None
    ws._silence_s = 0.6
    return ws


def test_remote_failure_lazy_loads_local_and_marks_suspect(monkeypatch):
    ws = _ws()
    # Stub the lazy loader so the test never downloads a real model.
    monkeypatch.setattr(ws, "_ensure_fallback_model",
                        lambda: setattr(ws, "_model", _FakeModel()))
    segs, info = ws._run_whisper(np.zeros(16000, dtype=np.float32), "prompt")
    assert ws._cluster_health.suspect == ["whisper"]   # H2: re-probe nudged
    assert segs and segs[0].text == "hello"            # C1: degraded to local
    assert info.language == "en"


def test_remote_failure_raises_when_no_fallback(monkeypatch):
    ws = _ws()
    monkeypatch.setattr(ws, "_ensure_fallback_model", lambda: None)  # load fails
    with pytest.raises(Exception):
        ws._run_whisper(np.zeros(16000, dtype=np.float32), "prompt")


def test_ensure_fallback_is_noop_when_model_present():
    ws = _ws()
    ws._model = _FakeModel()
    before = ws._model
    ws._ensure_fallback_model()
    assert ws._model is before  # never reloads over an existing model


# ── M3: DevAgent RAG falls back when remote indexer returns empty ───────────

class _EmptyRemoteIndexer:
    async def query_combined(self, q, n):
        return []  # flaking remote returns no hits


class _RecordingLocalIndexer:
    available = True
    def __init__(self):
        self.called = False
    async def query_combined(self, q, n):
        self.called = True
        return []


def test_empty_remote_result_falls_back_to_local():
    from inference.dev_agent import DevAgent
    da = DevAgent.__new__(DevAgent)
    da._remote_indexer = _EmptyRemoteIndexer()
    da._cluster_health = None
    local = _RecordingLocalIndexer()
    da._indexer = local
    asyncio.run(da._rag_context("how does routing work", n=3))
    # Old code (`if hits is None`) skipped local on an empty remote list.
    assert local.called is True
