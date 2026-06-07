"""ResourceGovernor — pain-aware resource control kernel primitive.

Elevates PainDayEngine from a BehavioralTwinState-internal concern to a
first-class kernel primitive that directly controls hardware-side resources.

On flare (pain_day_score >= 0.6):
    1. Tells FusionEngine to apply pain-day sensor thresholds immediately
    2. Pauses CodebaseIndexer (no new index jobs; in-progress job completes)
    3. Raises WhisperStream VAD thread priority (Windows SetThreadPriority)
    4. Reduces Ollama VRAM keepalive for qwen3-vl:30b to 0 (evicts ~18 GB)

On recovery (pain_day_score < 0.4, hysteresis):
    All four actions are reversed.  stop() also calls restore unconditionally.

Design constraints:
    - Polling interval: 5 s (not on the 60 Hz hot path).
    - get_pain_day_score() is a zero-copy attribute read via MemoryManager
      (no await, no DB hit, safe to call frequently).
    - All OS-level calls run in asyncio.to_thread or a daemon thread so the
      event loop is never blocked.
    - All actions are idempotent and recoverable on crash.
    - The 0.6 / 0.4 thresholds mirror BehavioralTwinState's own hysteresis
      constants to avoid conflicting state transitions.

Wire-up:
    governor = ResourceGovernor(memory=memory)
    governor.set_fusion_engine(fusion)
    governor.set_whisper_stream(whisper)
    governor.set_indexer(indexer)        # optional
    await governor.start()
    shutdown.register(governor)
    # ... pipeline runs ...
    await governor.stop()
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from core.fusion_engine import FusionEngine
    from sensors.whisper_stream import WhisperStream
    from inference.codebase_indexer import CodebaseIndexer
    from storage.memory_manager import MemoryManager

log = logging.getLogger(__name__)

# Mirror BehavioralTwinState hysteresis thresholds exactly
_ACTIVATE_THRESHOLD = 0.6
_DEACTIVATE_THRESHOLD = 0.4

# Windows THREAD_SET_INFORMATION | THREAD_QUERY_INFORMATION
_THREAD_ACCESS = 0x0060
_THREAD_PRIORITY_ABOVE_NORMAL = 1
_THREAD_PRIORITY_NORMAL = 0

# Fallback heavy-model list used only when no ModelRouter is wired (preserves a
# sane eviction target). The live set normally comes from
# ModelRouter.heavy_model_names() so it can never go stale.
_DEFAULT_HEAVY_MODELS: tuple[str, ...] = (
    "qwen3-coder:30b", "qwen3-vl:30b",
)


class ResourceGovernor:
    """Translates pain-day state into OS + inference resource changes.

    Polls MemoryManager.get_pain_day_score() every POLL_INTERVAL_S seconds.
    Responds to score changes by adjusting FusionEngine, WhisperStream,
    CodebaseIndexer, and Ollama keepalive.
    """

    POLL_INTERVAL_S = 5.0

    def __init__(self, memory: "MemoryManager") -> None:
        self._memory = memory
        self._fusion: Optional["FusionEngine"] = None
        self._whisper: Optional["WhisperStream"] = None
        self._indexer: Optional["CodebaseIndexer"] = None
        self._router = None   # ModelRouter — set via set_model_router()
        self._flare_active: bool = False
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._bg_tasks: set = set()  # keeps fire-and-forget create_task refs alive

    # ── Wiring ────────────────────────────────────────────────────────────────

    def set_fusion_engine(self, fusion: "FusionEngine") -> None:
        self._fusion = fusion

    def set_whisper_stream(self, whisper: "WhisperStream") -> None:
        self._whisper = whisper

    def set_indexer(self, indexer: "CodebaseIndexer") -> None:
        self._indexer = indexer

    def set_model_router(self, router) -> None:
        """Wire the ModelRouter so flare-time eviction targets the models the
        router can actually load (not a hardcoded name) and can sleep the vLLM
        specialist pool when active."""
        self._router = router

    def _heavy_models(self) -> list[str]:
        """The heavy specialist model names to evict on a flare."""
        if self._router is not None:
            try:
                names = self._router.heavy_model_names()
                if names:
                    return names
            except Exception as exc:
                log.debug("ResourceGovernor._heavy_models: router query failed: %s", exc)
        return list(_DEFAULT_HEAVY_MODELS)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._running:
            log.warning("ResourceGovernor.start() called while already running — ignored")
            return
        self._running = True
        self._task = asyncio.create_task(
            self._poll_loop(), name="resource_governor"
        )
        log.info(
            "ResourceGovernor started (poll_interval=%.1fs, "
            "activate>=%.1f, deactivate<%.1f)",
            self.POLL_INTERVAL_S, _ACTIVATE_THRESHOLD, _DEACTIVATE_THRESHOLD,
        )

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        # Always restore defaults on shutdown regardless of current flare state.
        # Run in a thread — _restore_resources_sync() makes blocking urllib calls.
        await asyncio.to_thread(self._restore_resources_sync)
        log.info("ResourceGovernor stopped (resources restored)")

    # ── SVT fast-path ────────────────────────────────────────────────────────

    def notify_pain_day_change(self, score: float) -> None:
        """Called synchronously by BehavioralTwinState.set_manual_pain_day().

        Fires _on_flare_start / _on_flare_end immediately via create_task so
        VRAM is released in <100ms on an SVT attack instead of waiting up to
        POLL_INTERVAL_S (5s) for the next poll cycle.
        """
        if not self._running:
            return
        if not self._flare_active and score >= _ACTIVATE_THRESHOLD:
            t = asyncio.create_task(
                self._on_flare_start(score), name="governor_svt_flare_start"
            )
            self._bg_tasks.add(t)
            t.add_done_callback(self._bg_tasks.discard)
        elif self._flare_active and score < _DEACTIVATE_THRESHOLD:
            t = asyncio.create_task(
                self._on_flare_end(score), name="governor_svt_flare_end"
            )
            self._bg_tasks.add(t)
            t.add_done_callback(self._bg_tasks.discard)

    # ── Poll loop ─────────────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self.POLL_INTERVAL_S)
                score = self._memory.get_pain_day_score()  # zero-copy, no await

                if not self._flare_active and score >= _ACTIVATE_THRESHOLD:
                    await self._on_flare_start(score)
                elif self._flare_active and score < _DEACTIVATE_THRESHOLD:
                    await self._on_flare_end(score)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.warning("ResourceGovernor._poll_loop error: %s", exc)

    # ── Flare response ────────────────────────────────────────────────────────

    async def _on_flare_start(self, score: float) -> None:
        self._flare_active = True
        log.info(
            "ResourceGovernor: FLARE START (score=%.2f) — restricting resources",
            score,
        )
        # 1. FusionEngine pain-day sensor thresholds
        if self._fusion is not None:
            self._fusion.apply_pain_day(True)

        # 2. Pause CodebaseIndexer
        if self._indexer is not None:
            try:
                self._indexer.pause()
            except Exception as exc:
                log.debug("ResourceGovernor: indexer.pause() failed: %s", exc)

        # 3. Raise WhisperStream VAD thread priority (Windows only, non-blocking)
        await asyncio.to_thread(self._raise_whisper_priority)

        # 4. Evict heavy specialist models from VRAM.
        #    Ollama path: keep_alive=0 for each router-derived heavy model.
        #    vLLM path: tear down any resident specialist via the pool.
        await asyncio.to_thread(self._evict_heavy_models)
        if self._router is not None:
            try:
                await self._router.sleep_specialists()
            except Exception as exc:
                log.debug("ResourceGovernor: sleep_specialists failed: %s", exc)

    async def _on_flare_end(self, score: float) -> None:
        self._flare_active = False
        log.info(
            "ResourceGovernor: FLARE END (score=%.2f) — restoring resources",
            score,
        )
        await asyncio.to_thread(self._restore_resources_sync)

    # ── Resource actions (blocking; run in asyncio.to_thread) ─────────────────

    def _restore_resources_sync(self) -> None:
        """Restore all resources to defaults. Always idempotent."""
        if self._fusion is not None:
            try:
                self._fusion.apply_pain_day(False)
            except Exception:
                pass
        if self._indexer is not None:
            try:
                self._indexer.resume()
            except Exception:
                pass
        self._restore_whisper_priority()
        self._restore_heavy_models()

    def _raise_whisper_priority(self) -> None:
        """Raise the WhisperStream VAD thread priority on Windows."""
        if sys.platform != "win32":
            return
        try:
            import ctypes
            thread: Optional[threading.Thread] = getattr(
                self._whisper, "_vad_thread", None
            )
            if thread is None or not thread.is_alive():
                return
            handle = ctypes.windll.kernel32.OpenThread(
                _THREAD_ACCESS, False, thread.ident
            )
            if handle:
                try:
                    ctypes.windll.kernel32.SetThreadPriority(
                        handle, _THREAD_PRIORITY_ABOVE_NORMAL
                    )
                    log.info(
                        "ResourceGovernor: WhisperStream VAD thread priority "
                        "raised to ABOVE_NORMAL"
                    )
                finally:
                    ctypes.windll.kernel32.CloseHandle(handle)
        except Exception as exc:
            log.debug("ResourceGovernor._raise_whisper_priority failed: %s", exc)

    def _restore_whisper_priority(self) -> None:
        """Restore the WhisperStream VAD thread to normal priority."""
        if sys.platform != "win32":
            return
        try:
            import ctypes
            thread: Optional[threading.Thread] = getattr(
                self._whisper, "_vad_thread", None
            )
            if thread is None or not thread.is_alive():
                return
            handle = ctypes.windll.kernel32.OpenThread(
                _THREAD_ACCESS, False, thread.ident
            )
            if handle:
                try:
                    ctypes.windll.kernel32.SetThreadPriority(
                        handle, _THREAD_PRIORITY_NORMAL
                    )
                finally:
                    ctypes.windll.kernel32.CloseHandle(handle)
        except Exception as exc:
            log.debug("ResourceGovernor._restore_whisper_priority failed: %s", exc)

    def _post_keepalive(self, model: str, keep_alive: str) -> None:
        """POST a single keep_alive directive to Ollama for `model`."""
        import urllib.request, json as _json
        body = _json.dumps({"model": model, "keep_alive": keep_alive}).encode()
        req = urllib.request.Request(
            "http://localhost:11434/api/generate",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=5)

    def _evict_heavy_models(self) -> None:
        """Set keep_alive=0 for every heavy specialist so Ollama evicts it.

        Targets the router-derived heavy set (falls back to _DEFAULT_HEAVY_MODELS
        when no router is wired), so it can never go stale against the lineup.
        Per-model failures are non-fatal (a model that isn't loaded is a no-op).
        """
        models = self._heavy_models()
        for model in models:
            try:
                self._post_keepalive(model, "0")
            except Exception as exc:
                log.debug("ResourceGovernor: evict %s failed: %s", model, exc)
        log.info("ResourceGovernor: heavy-model VRAM eviction requested (flare): %s", models)

    def _restore_heavy_models(self) -> None:
        """Restore keep_alive=5m for every heavy specialist (Ollama default)."""
        models = self._heavy_models()
        for model in models:
            try:
                self._post_keepalive(model, "5m")
            except Exception as exc:
                log.debug("ResourceGovernor: restore %s failed: %s", model, exc)

    # ── Status ────────────────────────────────────────────────────────────────

    def get_status(self) -> dict:
        return {
            "flare_active": self._flare_active,
            "pain_day_score": round(self._memory.get_pain_day_score(), 3),
            "running": self._running,
        }
