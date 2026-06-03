"""ClusterHealthMonitor — liveness tracking for laptop service-node endpoints.

Polls each configured laptop service every `interval` seconds and exposes a
zero-cost, synchronous `is_healthy(service)` that routing code can call at
decision time (no await, safe inside the hot path).

Services tracked (only those present in ClusterConfig are polled):
    "laptop_ollama"  → GET <ollama_url>/api/tags     (Ollama has no /health)
    "whisper"        → GET <whisper_url>/health
    "indexer"        → GET <indexer_url>/health

Design:
  - One background asyncio task; each probe runs in a thread so a hung socket
    never blocks the event loop.
  - First failure logs WARNING; recovery logs INFO. Steady state is silent.
  - Unknown / not-yet-checked services report False (fail safe → route local).
"""

from __future__ import annotations

import asyncio
import logging
import time
import urllib.request
from typing import Dict, Optional, Set, Tuple

from core.cluster_config import ClusterConfig

log = logging.getLogger(__name__)


class ClusterHealthMonitor:
    def __init__(
        self,
        config: ClusterConfig,
        interval: float = 10.0,
        timeout: float = 2.0,
        fail_threshold: int = 2,
        recover_threshold: int = 1,
    ) -> None:
        self._config = config
        self._interval = interval
        self._timeout = timeout
        # Debounce: require N consecutive probes that disagree with the current
        # state before flipping it, so one transient blip can't evict the laptop
        # (fail) or a single lucky probe can't prematurely re-enable it (recover).
        self._fail_threshold = max(1, fail_threshold)
        self._recover_threshold = max(1, recover_threshold)
        self._streak: Dict[str, int] = {}        # consecutive probes disagreeing with state
        self._health: Dict[str, bool] = {}
        self._task: Optional[asyncio.Task] = None
        # NOTE: named _evloop, NOT _loop — _loop is the poll-loop coroutine method
        # below; an attribute named self._loop would shadow it and break start().
        self._evloop: Optional[asyncio.AbstractEventLoop] = None
        self._recheck_pending: Set[str] = set()   # dedupe in-flight on-demand re-probes

        # service name → health-check URL
        self._endpoints: Dict[str, str] = {}
        if config.laptop_ollama_url:
            self._endpoints["laptop_ollama"] = config.laptop_ollama_url + "/api/tags"
        if config.laptop_whisper_url:
            self._endpoints["whisper"] = config.laptop_whisper_url + "/health"
        if config.laptop_indexer_url:
            self._endpoints["indexer"] = config.laptop_indexer_url + "/health"

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        """Run one immediate check, then poll in the background."""
        if not self._endpoints:
            log.info("ClusterHealthMonitor: no endpoints configured — not starting")
            return
        if self._task is not None and not self._task.done():
            log.warning("ClusterHealthMonitor.start() called while already running — ignored")
            return
        self._evloop = asyncio.get_running_loop()  # for thread-safe mark_suspect()
        await self._check_all()
        self._task = asyncio.create_task(self._loop(), name="cluster-health")
        log.info(
            "ClusterHealthMonitor: started (every %.0fs) — %s",
            self._interval, ", ".join(self._endpoints),
        )

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._interval)
                await self._check_all()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # never let the monitor die
                log.debug("ClusterHealthMonitor: loop error %s", exc)

    # ------------------------------------------------------------------ #
    # Probing
    # ------------------------------------------------------------------ #

    async def _check_all(self) -> None:
        for svc, url in self._endpoints.items():
            ok, detail, dt_ms = await asyncio.to_thread(self._ping, url)
            self._apply_probe(svc, url, ok, detail, dt_ms)

    def _ping(self, url: str) -> Tuple[bool, str, float]:
        """Probe `url`. Returns (ok, detail, latency_ms). `detail` is the HTTP
        status or the exception class name (M4: distinguishes timeout vs refused
        vs 5xx instead of collapsing every failure to one boolean)."""
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(url, timeout=self._timeout) as resp:
                code = int(getattr(resp, "status", 200))
                return (200 <= code < 400), f"HTTP {code}", (time.monotonic() - t0) * 1000.0
        except Exception as exc:
            return False, type(exc).__name__, (time.monotonic() - t0) * 1000.0

    def _apply_probe(self, svc: str, url: str, ok: bool, detail: str, dt_ms: float) -> None:
        """Fold one probe result into health state with debounce (H1).

        A probe that agrees with the current state resets the streak. A probe
        that disagrees accumulates a streak; the state flips only once the streak
        reaches fail_threshold (down) / recover_threshold (up).
        """
        cur = self._health.get(svc, False)
        if ok == cur:
            self._streak[svc] = 0
            return
        self._streak[svc] = self._streak.get(svc, 0) + 1
        need = self._recover_threshold if ok else self._fail_threshold
        if self._streak[svc] >= need:
            self._health[svc] = ok
            self._streak[svc] = 0
            if ok:
                log.info("ClusterHealthMonitor: %s is UP (%s, %.0fms)", svc, url, dt_ms)
            else:
                log.warning(
                    "ClusterHealthMonitor: %s is DOWN (%s, %s) after %d miss(es) "
                    "— routing falls back to desktop", svc, url, detail, need,
                )
        else:
            log.debug(
                "ClusterHealthMonitor: %s probe %s (%s, %.0fms) streak %d/%d",
                svc, "ok" if ok else "fail", detail, dt_ms, self._streak[svc], need,
            )

    # ------------------------------------------------------------------ #
    # On-demand re-probe (H2) — called by consumers on a live remote failure
    # ------------------------------------------------------------------ #

    def mark_suspect(self, service: str) -> None:
        """Trigger an immediate re-probe of `service` instead of waiting up to
        `interval` for the next poll. Thread-safe: consumers (e.g. WhisperStream)
        call this from worker threads, so the coroutine is scheduled onto the
        monitor's event loop. Deduped while a re-probe is already in flight."""
        if service not in self._endpoints or self._evloop is None:
            return
        if service in self._recheck_pending:
            return
        self._recheck_pending.add(service)
        try:
            asyncio.run_coroutine_threadsafe(self._recheck(service), self._evloop)
        except Exception:
            self._recheck_pending.discard(service)

    async def _recheck(self, service: str) -> None:
        try:
            url = self._endpoints.get(service)
            if url:
                ok, detail, dt_ms = await asyncio.to_thread(self._ping, url)
                self._apply_probe(service, url, ok, detail, dt_ms)
        finally:
            self._recheck_pending.discard(service)

    # ------------------------------------------------------------------ #
    # Query (sync, hot-path safe)
    # ------------------------------------------------------------------ #

    def is_healthy(self, service: str) -> bool:
        """Return last-known health for a service. Unknown → False (route local)."""
        return self._health.get(service, False)

    def status(self) -> Dict[str, bool]:
        """Snapshot of all tracked services (for the startup table / diagnostics)."""
        return dict(self._health)
