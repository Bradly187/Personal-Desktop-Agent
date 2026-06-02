"""Tests for ResourceGovernor — Gap 3 (AIOS alignment).

Covers:
- Flare activation when score >= 0.6 after poll cycle
- Flare recovery when score < 0.4
- Hysteresis: score in (0.4, 0.6) leaves state unchanged
- CodebaseIndexer pause/resume wired correctly
- FusionEngine apply_pain_day called with correct bool
- Ollama keepalive POST fired with keep_alive="0" on flare, "5m" on recovery
- stop() always restores resources regardless of _flare_active state
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.resource_governor import ResourceGovernor, _ACTIVATE_THRESHOLD, _DEACTIVATE_THRESHOLD


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_memory(score=0.0, active=False):
    mem = MagicMock()
    mem.get_pain_day_score.return_value = score
    mem.get_pain_day_active.return_value = active
    return mem


def _make_governor(score=0.0):
    mem = _make_memory(score=score)
    gov = ResourceGovernor(memory=mem)
    return gov, mem


def _make_fusion():
    fusion = MagicMock()
    fusion.apply_pain_day = MagicMock()
    return fusion


def _make_indexer():
    indexer = MagicMock()
    indexer._paused = False

    def _pause():
        indexer._paused = True

    def _resume():
        indexer._paused = False

    indexer.pause.side_effect = _pause
    indexer.resume.side_effect = _resume
    return indexer


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

class TestThresholds:
    def test_activate_threshold_is_0_6(self):
        assert abs(_ACTIVATE_THRESHOLD - 0.6) < 1e-9

    def test_deactivate_threshold_is_0_4(self):
        assert abs(_DEACTIVATE_THRESHOLD - 0.4) < 1e-9


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

class TestGovernorLifecycle:
    @pytest.mark.asyncio
    async def test_start_sets_running(self):
        gov, _ = _make_governor()
        await gov.start()
        try:
            assert gov._running is True
            assert gov._task is not None
        finally:
            await gov.stop()

    @pytest.mark.asyncio
    async def test_stop_clears_running(self):
        gov, _ = _make_governor()
        await gov.start()
        await gov.stop()
        assert gov._running is False

    def test_get_status_before_start(self):
        gov, _ = _make_governor(score=0.3)
        status = gov.get_status()
        assert "flare_active" in status
        assert "pain_day_score" in status
        assert "running" in status
        assert status["flare_active"] is False
        assert status["running"] is False


# ---------------------------------------------------------------------------
# Flare activation
# ---------------------------------------------------------------------------

class TestFlareActivation:
    @pytest.mark.asyncio
    async def test_flare_starts_when_score_above_threshold(self):
        """After one poll cycle with score=0.7, _flare_active becomes True
        and fusion.apply_pain_day(True) is called."""
        mem = _make_memory(score=0.7)
        gov = ResourceGovernor(memory=mem)
        fusion = _make_fusion()
        gov.set_fusion_engine(fusion)

        async def _noop(fn, *a, **kw):
            try:
                fn(*a, **kw)
            except Exception:
                pass

        with patch("core.resource_governor.asyncio.to_thread", side_effect=_noop):
            await gov._on_flare_start(0.7)

        assert gov._flare_active is True
        fusion.apply_pain_day.assert_called_once_with(True)

    @pytest.mark.asyncio
    async def test_indexer_paused_on_flare_start(self):
        mem = _make_memory(score=0.7)
        gov = ResourceGovernor(memory=mem)
        indexer = _make_indexer()
        gov.set_indexer(indexer)

        async def _noop(fn, *a, **kw):
            try:
                fn(*a, **kw)
            except Exception:
                pass

        with patch("core.resource_governor.asyncio.to_thread", side_effect=_noop):
            await gov._on_flare_start(0.7)

        assert indexer._paused is True
        indexer.pause.assert_called_once()

    @pytest.mark.asyncio
    async def test_ollama_keepalive_set_to_zero_on_flare(self):
        """_reduce_ollama_keepalive must POST keep_alive='0' to Ollama."""
        gov, _ = _make_governor()
        posted_bodies = []

        import urllib.request
        import json

        class _FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        def _fake_urlopen(req, timeout=None):
            posted_bodies.append(json.loads(req.data))
            return _FakeResp()

        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            gov._reduce_ollama_keepalive()

        assert len(posted_bodies) == 1
        assert posted_bodies[0]["keep_alive"] == "0"
        assert posted_bodies[0]["model"] == "qwen3-vl:30b"


# ---------------------------------------------------------------------------
# Flare recovery
# ---------------------------------------------------------------------------

class TestFlareRecovery:
    @pytest.mark.asyncio
    async def test_flare_ends_when_score_drops_below_threshold(self):
        mem = _make_memory(score=0.2)
        gov = ResourceGovernor(memory=mem)
        gov._flare_active = True
        fusion = _make_fusion()
        gov.set_fusion_engine(fusion)

        async def _noop(fn, *a, **kw):
            try:
                fn(*a, **kw)
            except Exception:
                pass

        with patch("core.resource_governor.asyncio.to_thread", side_effect=_noop):
            await gov._on_flare_end(0.2)

        assert gov._flare_active is False
        fusion.apply_pain_day.assert_called_once_with(False)

    @pytest.mark.asyncio
    async def test_indexer_resumed_on_flare_end(self):
        mem = _make_memory(score=0.2)
        gov = ResourceGovernor(memory=mem)
        gov._flare_active = True
        indexer = _make_indexer()
        indexer._paused = True
        gov.set_indexer(indexer)

        async def _noop(fn, *a, **kw):
            try:
                fn(*a, **kw)
            except Exception:
                pass

        with patch("core.resource_governor.asyncio.to_thread", side_effect=_noop):
            await gov._on_flare_end(0.2)

        assert indexer._paused is False
        indexer.resume.assert_called_once()

    def test_ollama_keepalive_restored_to_5m_on_recovery(self):
        gov, _ = _make_governor()
        import json
        posted_bodies = []

        class _FakeResp:
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass

        def _fake_urlopen(req, timeout=None):
            posted_bodies.append(json.loads(req.data))
            return _FakeResp()

        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            gov._restore_ollama_keepalive()

        assert len(posted_bodies) == 1
        assert posted_bodies[0]["keep_alive"] == "5m"


# ---------------------------------------------------------------------------
# Hysteresis
# ---------------------------------------------------------------------------

class TestHysteresis:
    @pytest.mark.asyncio
    async def test_score_in_dead_band_does_not_activate(self):
        """Score of 0.5 (between 0.4 and 0.6) should not trigger a flare."""
        mem = _make_memory(score=0.5)
        gov = ResourceGovernor(memory=mem)
        gov.POLL_INTERVAL_S = 0.05
        await gov.start()
        await asyncio.sleep(0.15)  # let 2–3 polls run
        assert gov._flare_active is False
        await gov.stop()

    @pytest.mark.asyncio
    async def test_score_above_threshold_activates_via_poll(self):
        """Score = 0.7 should activate flare within a few poll cycles."""
        mem = _make_memory(score=0.7)
        gov = ResourceGovernor(memory=mem)
        gov.POLL_INTERVAL_S = 0.05
        fusion = _make_fusion()
        gov.set_fusion_engine(fusion)

        with (
            patch.object(gov, "_raise_whisper_priority"),
            patch.object(gov, "_reduce_ollama_keepalive"),
        ):
            await gov.start()
            await asyncio.sleep(0.2)  # allow poll cycle

        try:
            assert gov._flare_active is True
        finally:
            await gov.stop()

    @pytest.mark.asyncio
    async def test_active_flare_with_score_above_deactivate_stays_active(self):
        """Flare stays active while score remains at 0.5 (above 0.4 deactivate threshold)."""
        mem = _make_memory(score=0.5)
        gov = ResourceGovernor(memory=mem)
        gov._flare_active = True
        gov.POLL_INTERVAL_S = 0.05

        fusion = _make_fusion()
        gov.set_fusion_engine(fusion)

        with (
            patch.object(gov, "_restore_whisper_priority"),
            patch.object(gov, "_restore_ollama_keepalive"),
        ):
            await gov.start()
            await asyncio.sleep(0.2)

        try:
            assert gov._flare_active is True
            fusion.apply_pain_day.assert_not_called()  # no transition fired
        finally:
            await gov.stop()


# ---------------------------------------------------------------------------
# stop() always restores
# ---------------------------------------------------------------------------

class TestStopAlwaysRestores:
    @pytest.mark.asyncio
    async def test_stop_restores_when_flare_was_active(self):
        gov, _ = _make_governor()
        gov._flare_active = True
        fusion = _make_fusion()
        gov.set_fusion_engine(fusion)
        indexer = _make_indexer()
        indexer._paused = True
        gov.set_indexer(indexer)

        with (
            patch.object(gov, "_restore_whisper_priority"),
            patch.object(gov, "_restore_ollama_keepalive"),
        ):
            await gov.start()
            await gov.stop()

        fusion.apply_pain_day.assert_called_with(False)
        assert indexer._paused is False

    @pytest.mark.asyncio
    async def test_stop_restores_when_flare_was_inactive(self):
        """stop() must call _restore_resources_sync even when no flare was active."""
        gov, _ = _make_governor()
        gov._flare_active = False
        fusion = _make_fusion()
        gov.set_fusion_engine(fusion)

        restored = []

        def _record_restore():
            restored.append(True)

        gov._restore_resources_sync = _record_restore

        await gov.start()
        await gov.stop()

        assert len(restored) >= 1

    @pytest.mark.asyncio
    async def test_stop_without_optional_components_does_not_raise(self):
        """Governor without fusion/whisper/indexer must stop cleanly."""
        gov, _ = _make_governor()
        with (
            patch.object(gov, "_restore_whisper_priority"),
            patch.object(gov, "_restore_ollama_keepalive"),
        ):
            await gov.start()
            await gov.stop()  # should not raise


# ---------------------------------------------------------------------------
# _restore_resources_sync idempotency
# ---------------------------------------------------------------------------

class TestRestoreIdempotency:
    def test_restore_calls_fusion_apply_false(self):
        gov, _ = _make_governor()
        fusion = _make_fusion()
        gov.set_fusion_engine(fusion)

        with (
            patch.object(gov, "_restore_whisper_priority"),
            patch.object(gov, "_restore_ollama_keepalive"),
        ):
            gov._restore_resources_sync()
            gov._restore_resources_sync()  # called twice — idempotent

        assert fusion.apply_pain_day.call_count == 2
        fusion.apply_pain_day.assert_called_with(False)

    def test_restore_resumes_indexer(self):
        gov, _ = _make_governor()
        indexer = _make_indexer()
        gov.set_indexer(indexer)

        with (
            patch.object(gov, "_restore_whisper_priority"),
            patch.object(gov, "_restore_ollama_keepalive"),
        ):
            gov._restore_resources_sync()

        indexer.resume.assert_called_once()

    def test_restore_with_no_components_does_not_raise(self):
        gov, _ = _make_governor()
        with (
            patch.object(gov, "_restore_whisper_priority"),
            patch.object(gov, "_restore_ollama_keepalive"),
        ):
            gov._restore_resources_sync()  # no fusion, no indexer, no whisper


# ---------------------------------------------------------------------------
# asyncio.to_thread shim used in tests
# ---------------------------------------------------------------------------

class _NoopToThread:
    """Replaces asyncio.to_thread: runs the callable synchronously in tests."""

    def __init__(self, func, *args, **kwargs):
        self._func = func
        self._args = args
        self._kwargs = kwargs

    def __await__(self):
        try:
            self._func(*self._args, **self._kwargs)
        except Exception:
            pass
        return iter([])


def _noop_to_thread_factory():
    """Factory used by patch(new_callable=...) to produce the async shim."""
    async def _shim(func, *args, **kwargs):
        try:
            func(*args, **kwargs)
        except Exception:
            pass
    return _shim
