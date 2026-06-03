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
import urllib.request
from typing import Dict, Optional

from core.cluster_config import ClusterConfig

log = logging.getLogger(__name__)


class ClusterHealthMonitor:
    def __init__(
        self,
        config: ClusterConfig,
        interval: float = 10.0,
        timeout: float = 2.0,
    ) -> None:
        self._config = config
        self._interval = interval
        self._timeout = timeout
        self._health: Dict[str, bool] = {}
        self._task: Optional[asyncio.Task] = None

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
            ok = await asyncio.to_thread(self._ping, url)
            prev = self._health.get(svc)
            if ok != prev:
                if ok:
                    log.info("ClusterHealthMonitor: %s is UP (%s)", svc, url)
                else:
                    # WARNING only after we'd previously seen it up, or on first miss
                    log.warning("ClusterHealthMonitor: %s is DOWN (%s) — routing falls back to desktop", svc, url)
            self._health[svc] = ok

    def _ping(self, url: str) -> bool:
        try:
            with urllib.request.urlopen(url, timeout=self._timeout) as resp:
                return 200 <= getattr(resp, "status", 200) < 400
        except Exception:
            return False

    # ------------------------------------------------------------------ #
    # Query (sync, hot-path safe)
    # ------------------------------------------------------------------ #

    def is_healthy(self, service: str) -> bool:
        """Return last-known health for a service. Unknown → False (route local)."""
        return self._health.get(service, False)

    def status(self) -> Dict[str, bool]:
        """Snapshot of all tracked services (for the startup table / diagnostics)."""
        return dict(self._health)
