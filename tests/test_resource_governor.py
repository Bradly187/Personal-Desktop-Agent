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
from unittest.mock import AsyncMock, MagicMock, patch, call

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


def _patch_ollama(fn):
    """Run fn() with urllib.request.urlopen patched; return the list of POSTed JSON bodies."""
    import json
    posted = []

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    def _fake_urlopen(req, timeout=None):
        posted.append(json.loads(req.data))
        return _FakeResp()

    with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
        fn()
    return posted


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
    async def test_evict_uses_default_heavy_set_without_router(self):
        """Without a router, _evict_heavy_models POSTs keep_alive='0' for each default heavy model."""
        from core.resource_governor import _DEFAULT_HEAVY_MODELS
        gov, _ = _make_governor()
        posted = _patch_ollama(lambda: gov._evict_heavy_models())

        assert {b["keep_alive"] for b in posted} == {"0"}
        assert {b["model"] for b in posted} == set(_DEFAULT_HEAVY_MODELS)

    @pytest.mark.asyncio
    async def test_evict_targets_router_heavy_set(self):
        """When a router is wired, eviction targets router.heavy_model_names() — never a stale hardcoded name."""
        gov, _ = _make_governor()
        router = MagicMock()
        router.heavy_model_names.return_value = ["qwen3-coder:30b", "gemma3:27b"]
        gov.set_model_router(router)

        posted = _patch_ollama(lambda: gov._evict_heavy_models())

        assert {b["model"] for b in posted} == {"qwen3-coder:30b", "gemma3:27b"}
        assert {b["keep_alive"] for b in posted} == {"0"}
        # The old hardcoded target must not leak in when the router doesn't list it.
        assert "qwen3-vl:30b" not in {b["model"] for b in posted}

    @pytest.mark.asyncio
    async def test_flare_start_sleeps_vllm_specialists(self):
        """_on_flare_start awaits router.sleep_specialists() to free the vLLM pool."""
        gov, _ = _make_governor()
        router = MagicMock()
        router.heavy_model_names.return_value = ["qwen3-coder:30b"]
        router.sleep_specialists = AsyncMock()
        gov.set_model_router(router)

        async def _noop(fn, *a, **kw):
            try:
                fn(*a, **kw)
            except Exception:
                pass

        with patch("core.resource_governor.asyncio.to_thread", side_effect=_noop):
            await gov._on_flare_start(0.7)

        router.sleep_specialists.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_restore_heavy_models_does_not_preload(self):
        """_restore_heavy_models is a no-op — it must NOT POST anything (an
        empty-prompt keep_alive='5m' is Ollama's LOAD idiom and would thrash
        VRAM by eagerly loading two 30B models; audit fix 2026-06-09)."""
        gov, _ = _make_governor()
        router = MagicMock()
        router.heavy_model_names.return_value = ["qwen3-coder:30b", "gemma3:27b"]
        gov.set_model_router(router)

        posted = _patch_ollama(lambda: gov._restore_heavy_models())

        assert posted == []   # no load POSTs


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

    def test_no_model_preload_on_recovery(self):
        """Flare recovery must NOT preload heavy models (the next real inference
        loads them on demand). Asserts _restore_heavy_models issues no POSTs."""
        gov, _ = _make_governor()
        posted_bodies = _patch_ollama(lambda: gov._restore_heavy_models())
        assert posted_bodies == []


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
            patch.object(gov, "_evict_heavy_models"),
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
            patch.object(gov, "_restore_heavy_models"),
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
            patch.object(gov, "_restore_heavy_models"),
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
            patch.object(gov, "_restore_heavy_models"),
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
            patch.object(gov, "_restore_heavy_models"),
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
            patch.object(gov, "_restore_heavy_models"),
        ):
            gov._restore_resources_sync()

        indexer.resume.assert_called_once()

    def test_restore_with_no_components_does_not_raise(self):
        gov, _ = _make_governor()
        with (
            patch.object(gov, "_restore_whisper_priority"),
            patch.object(gov, "_restore_heavy_models"),
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


# ---------------------------------------------------------------------------
# Flare fast-path: notify_pain_day_change()
# ---------------------------------------------------------------------------

class TestFlareFastPath:
    """notify_pain_day_change() must trigger flare start/end immediately
    without waiting for the 5-second poll cycle."""

    @pytest.mark.asyncio
    async def test_notify_above_threshold_triggers_flare_start(self):
        """score=1.0 → _on_flare_start called immediately via create_task."""
        gov, _ = _make_governor(score=0.0)
        fusion = _make_fusion()
        gov.set_fusion_engine(fusion)
        gov._running = True

        with (
            patch.object(gov, "_raise_whisper_priority"),
            patch.object(gov, "_evict_heavy_models"),
        ):
            gov.notify_pain_day_change(1.0)
            await asyncio.sleep(0)   # yield so the created task runs

        assert gov._flare_active is True
        fusion.apply_pain_day.assert_called_once_with(True)

    @pytest.mark.asyncio
    async def test_notify_below_threshold_triggers_flare_end(self):
        """score=0.0 while flare active → _on_flare_end called immediately."""
        gov, _ = _make_governor(score=0.0)
        gov._flare_active = True
        fusion = _make_fusion()
        gov.set_fusion_engine(fusion)
        gov._running = True

        with (
            patch.object(gov, "_restore_whisper_priority"),
            patch.object(gov, "_restore_heavy_models"),
        ):
            gov.notify_pain_day_change(0.0)
            await asyncio.sleep(0)

        assert gov._flare_active is False
        fusion.apply_pain_day.assert_called_once_with(False)

    def test_notify_does_nothing_when_not_running(self):
        """If governor is stopped, notify_pain_day_change is a no-op."""
        gov, _ = _make_governor(score=0.0)
        gov._running = False
        # Should not raise and should not change flare state
        gov.notify_pain_day_change(1.0)
        assert gov._flare_active is False

    def test_notify_in_dead_band_does_not_trigger(self):
        """score=0.5 (between 0.4 and 0.6) → no state change."""
        gov, _ = _make_governor()
        gov._running = True
        gov.notify_pain_day_change(0.5)
        assert gov._flare_active is False

    def test_notify_when_already_active_does_not_double_trigger(self):
        """Calling notify with score=1.0 while already in flare is a no-op."""
        gov, _ = _make_governor()
        gov._flare_active = True
        gov._running = True
        called = []
        gov._on_flare_start = lambda s: called.append(s)
        gov.notify_pain_day_change(1.0)
        assert called == []   # already active — no second start

    @pytest.mark.asyncio
    async def test_flare_fast_path_wired_from_twin_state(self):
        """BehavioralTwinState.set_manual_pain_day(True) fires governor immediately."""
        from adaptive.behavioral_twin_state import BehavioralTwinState

        db = AsyncMock()
        db.available = False
        twin = BehavioralTwinState(agent_db=db)
        mock_semantic = MagicMock()
        mock_semantic.available = False
        mock_semantic.add = AsyncMock()
        twin._semantic_memory = mock_semantic

        gov, _ = _make_governor()
        gov._running = True
        fusion = _make_fusion()
        gov.set_fusion_engine(fusion)
        twin.set_resource_governor(gov)

        with (
            patch.object(gov, "_raise_whisper_priority"),
            patch.object(gov, "_evict_heavy_models"),
        ):
            twin.set_manual_pain_day(True)
            await asyncio.sleep(0)

        assert gov._flare_active is True
        fusion.apply_pain_day.assert_called_once_with(True)

    def test_resource_governor_initialized_to_none_in_twin(self):
        """_resource_governor must be None by default (not missing from __init__)."""
        from adaptive.behavioral_twin_state import BehavioralTwinState
        db = AsyncMock()
        db.available = False
        twin = BehavioralTwinState(agent_db=db)
        assert twin._resource_governor is None


# ---------------------------------------------------------------------------
# Circuit-breaker: _run_cloud() timeout
# ---------------------------------------------------------------------------

class TestCloudCircuitBreaker:
    """_run_cloud() must time out and return CLARIFY instead of hanging."""

    @pytest.mark.asyncio
    async def test_run_cloud_returns_clarify_on_timeout(self):
        """If cloud inference hangs longer than _CLOUD_TIMEOUT_S, _run_cloud
        returns a CLARIFY string instead of stalling."""
        from core.hybrid_coordinator import HybridCoordinator, CoordinatorConfig
        from core.command_executor import Command

        local = MagicMock()
        local.infer = AsyncMock(return_value="CLICK button")
        local.get_status = MagicMock(return_value={"model": "test", "backend": "ollama"})

        coord = HybridCoordinator(local=local, config=CoordinatorConfig())

        # Replace internal _cloud with a hanging mock
        async def _hang(*a, **kw):
            await asyncio.sleep(999)

        coord._cloud = MagicMock()
        coord._cloud.infer = _hang

        from core.inference_runner import InferenceRunner
        original_timeout = InferenceRunner._CLOUD_TIMEOUT_S
        InferenceRunner._CLOUD_TIMEOUT_S = 0.05

        try:
            cmd = Command(text="open notepad", action="OPEN", source="voice")
            result = await coord._inference.run_cloud(cmd)
        finally:
            InferenceRunner._CLOUD_TIMEOUT_S = original_timeout

        assert result.startswith("CLARIFY")
        assert "timed out" in result.lower()

    @pytest.mark.asyncio
    async def test_run_cloud_succeeds_within_timeout(self):
        """Fast cloud inference completes normally and returns the action string."""
        from core.hybrid_coordinator import HybridCoordinator, CoordinatorConfig
        from core.command_executor import Command

        local = MagicMock()
        local.infer = AsyncMock(return_value="CLICK button")
        local.get_status = MagicMock(return_value={"model": "test", "backend": "ollama"})

        coord = HybridCoordinator(local=local, config=CoordinatorConfig())
        coord._cloud = MagicMock()
        coord._cloud.infer = AsyncMock(return_value="SCROLL down")

        cmd = Command(text="scroll down", action="SCROLL", source="voice")
        result = await coord._inference.run_cloud(cmd)

        assert result == "SCROLL down"

    @pytest.mark.asyncio
    async def test_cloud_timeout_constant_is_10s(self):
        """_CLOUD_TIMEOUT_S must be exactly 10 seconds."""
        from core.inference_runner import InferenceRunner
        assert InferenceRunner._CLOUD_TIMEOUT_S == 10.0


class TestFlareChangeCallback:
    """The flare-change callback fires on each transition so observers (e.g. the
    iPad Agent dashboard) can refresh."""

    def test_sync_callback_fires_with_state(self):
        gov, _ = _make_governor()
        seen = []
        gov.set_flare_change_callback(lambda active: seen.append(active))
        gov._notify_flare_change(True)
        gov._notify_flare_change(False)
        assert seen == [True, False]

    def test_no_callback_is_noop(self):
        gov, _ = _make_governor()
        gov._notify_flare_change(True)   # must not raise

    def test_callback_exception_is_swallowed(self):
        gov, _ = _make_governor()
        gov.set_flare_change_callback(lambda active: (_ for _ in ()).throw(RuntimeError("x")))
        gov._notify_flare_change(True)   # must not raise

    @pytest.mark.asyncio
    async def test_coroutine_callback_is_scheduled(self):
        gov, _ = _make_governor()
        seen = []

        async def _cb(active):
            seen.append(active)

        gov.set_flare_change_callback(_cb)
        gov._notify_flare_change(True)
        await asyncio.sleep(0)   # let the scheduled task run
        assert seen == [True]
