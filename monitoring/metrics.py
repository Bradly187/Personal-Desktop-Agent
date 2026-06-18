"""monitoring.metrics.py — In-process metrics for the Personal Desktop Agent.

Provides a thread-safe Metrics singleton that all pipeline components
write to.  Exposes:
  - get_snapshot()  → dict  (for dashboard polling and /metrics endpoint)
  - aiohttp_route() → aiohttp.web.Response  (JSON HTTP handler)

Usage:
    # In main.py startup:
    from monitoring.metrics import get_metrics
    m = get_metrics()
    fusion_engine.set_metrics(m)
    hybrid_coordinator.set_metrics(m)
    whisper_stream.set_metrics(m)

    # In any pipeline component:
    from monitoring.metrics import get_metrics
    get_metrics().inc("commands_total")
    get_metrics().set("pain_day_score", 0.7)
    get_metrics().observe("latency_ms", 312.5)

Metrics catalogue:
  Counters (monotonically increasing integers):
    commands_total          — all commands entering the pipeline
    commands_success        — commands that executed without error
    commands_failed         — commands that returned an error
    commands_clarify        — CLARIFY actions emitted
    cloud_escalations       — commands routed to the cloud (Anthropic API)
    gate0_blocks            — Gate 0 privacy blocks
    gate1_discards          — Gate 1 confidence discards
    gate2_cloud             — Gate 2 complexity → cloud
    gate3_cloud             — Gate 3 VRAM → cloud
    gate4_cloud             — Gate 4 latency → cloud
    corrections             — user corrections recorded
    whisper_hallucinations  — transcripts filtered by hallucination check
    gesture_rejections      — gestures below velocity/confidence floor

  Gauges (current value, can go up or down):
    pain_day_score          — PainDayEngine score [0.0, 1.0]
    whisper_logprob_ema     — EMA of recent Whisper avg_logprob
    gesture_conf_ema        — EMA of recent gesture confidence
    vram_free_gb            — free VRAM GB (updated every 60s)
    latency_ema_ms          — EMA of recent end-to-end latency
    gate1_threshold         — current Gate 1 whisper_logprob_min
    cloud_rate_1m           — cloud escalation rate over last 60 commands
    success_rate_1m         — success rate over last 60 commands

  Histograms (latency distributions):
    latency_ms              — end-to-end per-command latency
    whisper_latency_ms      — Whisper transcription latency
    llm_latency_ms          — local LLM inference latency
    cloud_latency_ms        — cloud (Anthropic API) round-trip latency

  Source counters (per-source command counts):
    source_counts           — {voice: N, gesture: N, touch: N, ...}
    domain_counts           — {command: N, code: N, math: N, ...}
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

# EMA smoothing factor — ~10-command rolling average
_EMA_ALPHA = 0.1

# Histogram bucket boundaries for latency_ms
_LATENCY_BUCKETS = [50, 100, 200, 400, 600, 1000, 2000, 5000, float("inf")]

# Rolling window size for rate metrics
_WINDOW = 60


# ---------------------------------------------------------------------------
# Histogram
# ---------------------------------------------------------------------------

class Histogram:
    """Simple fixed-bucket histogram with sum and count."""

    def __init__(self, buckets: list[float]) -> None:
        self._buckets = sorted(buckets)
        self._counts = [0] * len(self._buckets)
        self._sum = 0.0
        self._count = 0

    def observe(self, value: float) -> None:
        self._sum += value
        self._count += 1
        for i, bound in enumerate(self._buckets):
            if value <= bound:
                self._counts[i] += 1
                break

    @property
    def mean(self) -> Optional[float]:
        return self._sum / self._count if self._count else None

    def percentile(self, p: float) -> Optional[float]:
        """Approximate percentile from bucket boundaries."""
        if self._count == 0:
            return None
        target = self._count * p / 100.0
        cumulative = 0
        prev_bound = 0.0
        for bound, count in zip(self._buckets, self._counts):
            cumulative += count
            if cumulative >= target:
                return bound
        return self._buckets[-2] if len(self._buckets) > 1 else None

    def to_dict(self) -> dict:
        return {
            "count": self._count,
            "sum": self._sum,
            "mean": self.mean,
            "p50": self.percentile(50),
            "p95": self.percentile(95),
            "p99": self.percentile(99),
            "buckets": dict(zip(
                [str(b) for b in self._buckets],
                self._counts,
            )),
        }


# ---------------------------------------------------------------------------
# RollingWindow — for rate metrics
# ---------------------------------------------------------------------------

class RollingWindow:
    """Fixed-size deque tracking recent boolean outcomes for rate calculation."""

    def __init__(self, maxlen: int = _WINDOW) -> None:
        self._buf: deque[int] = deque(maxlen=maxlen)

    def push(self, value: int) -> None:
        self._buf.append(value)

    @property
    def rate(self) -> Optional[float]:
        if not self._buf:
            return None
        return sum(self._buf) / len(self._buf)


# ---------------------------------------------------------------------------
# Metrics — the main singleton
# ---------------------------------------------------------------------------

class Metrics:
    """Thread-safe in-process metrics store.

    All write operations acquire a lock so they are safe to call from
    any thread (Whisper runs in a thread pool, everything else is async).
    get_snapshot() also locks so snapshots are internally consistent.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._start_ts = time.time()

        # ── Counters ──────────────────────────────────────────────────────
        self._counters: dict[str, int] = {
            "commands_total": 0,
            "commands_success": 0,
            "commands_failed": 0,
            "commands_clarify": 0,
            "cloud_escalations": 0,
            "gate0_blocks": 0,
            "gate1_discards": 0,
            "gate2_cloud": 0,
            "gate3_cloud": 0,
            "gate4_cloud": 0,
            "corrections": 0,
            "whisper_hallucinations": 0,
            "gesture_rejections": 0,
            # FusionEngine circuit breaker (commands shed during acoustic storms/latency spikes)
            "route_task_breaker_sheds": 0,
            # Ollama hang-timeout (FINDING 4): incremented when DA_OLLAMA_TIMEOUT_S fires
            "ollama_hang_detected": 0,
        }

        # ── Gauges ────────────────────────────────────────────────────────
        self._gauges: dict[str, Optional[float]] = {
            "pain_day_score": 0.0,
            "whisper_logprob_ema": None,
            "gesture_conf_ema": None,
            "vram_free_gb": None,
            "latency_ema_ms": None,
            "gate1_threshold": -1.0,
            "cloud_rate_1m": None,
            "success_rate_1m": None,
            # Scheduler visibility (AccessibilityScheduler.set_metrics)
            "scheduler_queue_depth": 0.0,
            "scheduler_dev_inflight": 0.0,
            # FusionEngine circuit breaker (commands shed during acoustic storms)
            "coordinator_route_tasks_inflight": 0.0,
        }

        # ── Histograms ────────────────────────────────────────────────────
        self._histograms: dict[str, Histogram] = {
            "latency_ms":        Histogram(_LATENCY_BUCKETS),
            "whisper_latency_ms": Histogram(_LATENCY_BUCKETS),
            "llm_latency_ms":    Histogram(_LATENCY_BUCKETS),
            "cloud_latency_ms":  Histogram(_LATENCY_BUCKETS),
        }

        # ── Per-source / per-domain breakdown ─────────────────────────────
        self._source_counts: dict[str, int] = {}
        self._domain_counts: dict[str, int] = {}

        # ── Rolling windows for rate metrics ──────────────────────────────
        self._cloud_window  = RollingWindow(_WINDOW)
        self._success_window = RollingWindow(_WINDOW)

        # ── Recent commands feed (last 20, for dashboard) ─────────────────
        self._recent_commands: deque[dict] = deque(maxlen=20)

        # ── VRAM poll task ────────────────────────────────────────────────
        self._vram_task: Optional[asyncio.Task] = None

    # ---------------------------------------------------------------------- #
    # Write API
    # ---------------------------------------------------------------------- #

    def inc(self, name: str, by: int = 1) -> None:
        """Increment a counter by `by`."""
        with self._lock:
            if name in self._counters:
                self._counters[name] += by
            else:
                log.debug("metrics.inc: unknown counter %r", name)

    def set(self, name: str, value: float) -> None:
        """Set a gauge to `value`."""
        with self._lock:
            self._gauges[name] = value

    def observe(self, name: str, value: float) -> None:
        """Record an observation in a histogram."""
        with self._lock:
            if name in self._histograms:
                self._histograms[name].observe(value)
            else:
                log.debug("metrics.observe: unknown histogram %r", name)

    def ema_update(self, gauge_name: str, value: float, alpha: float = _EMA_ALPHA) -> None:
        """Update an EMA gauge with a new observation."""
        with self._lock:
            prev = self._gauges.get(gauge_name)
            if prev is None:
                self._gauges[gauge_name] = value
            else:
                self._gauges[gauge_name] = alpha * value + (1.0 - alpha) * prev

    # ── Convenience helpers called by pipeline components ─────────────────

    def record_command_routed(self, source: str) -> None:
        """Call when a Command enters _emit() in FusionEngine."""
        with self._lock:
            self._counters["commands_total"] += 1
            self._source_counts[source] = self._source_counts.get(source, 0) + 1

    def record_command_outcome(
        self,
        success: bool,
        action: str,
        latency_ms: float,
        route: str,
        domain: Optional[str] = None,
        gate: Optional[str] = None,
        whisper_logprob: Optional[float] = None,
        gesture_conf: Optional[float] = None,
    ) -> None:
        """Call after HybridCoordinator finishes executing a command."""
        with self._lock:
            if success:
                self._counters["commands_success"] += 1
                self._success_window.push(1)
            else:
                self._counters["commands_failed"] += 1
                self._success_window.push(0)

            if action == "CLARIFY":
                self._counters["commands_clarify"] += 1

            is_cloud = route == "cloud"
            if is_cloud:
                self._counters["cloud_escalations"] += 1
                self._cloud_window.push(1)
                self._histograms["cloud_latency_ms"].observe(latency_ms)
            else:
                self._cloud_window.push(0)

            # Gate counters
            if gate:
                gate_map = {
                    "gate0_privacy": "gate0_blocks",
                    "gate1_confidence": "gate1_discards",
                    "discard": "gate1_discards",
                    "gate2_complexity": "gate2_cloud",
                    "gate3_vram": "gate3_cloud",
                    "gate4_latency": "gate4_cloud",
                }
                if gate in gate_map:
                    self._counters[gate_map[gate]] += 1

            self._histograms["latency_ms"].observe(latency_ms)

            # EMA updates
            prev_lat = self._gauges.get("latency_ema_ms")
            self._gauges["latency_ema_ms"] = (
                latency_ms if prev_lat is None
                else _EMA_ALPHA * latency_ms + (1 - _EMA_ALPHA) * prev_lat
            )

            if whisper_logprob is not None:
                prev = self._gauges.get("whisper_logprob_ema")
                self._gauges["whisper_logprob_ema"] = (
                    whisper_logprob if prev is None
                    else _EMA_ALPHA * whisper_logprob + (1 - _EMA_ALPHA) * prev
                )
            if gesture_conf is not None:
                prev = self._gauges.get("gesture_conf_ema")
                self._gauges["gesture_conf_ema"] = (
                    gesture_conf if prev is None
                    else _EMA_ALPHA * gesture_conf + (1 - _EMA_ALPHA) * prev
                )

            if domain:
                self._domain_counts[domain] = self._domain_counts.get(domain, 0) + 1

            # Rolling rate snapshots
            self._gauges["cloud_rate_1m"] = self._cloud_window.rate
            self._gauges["success_rate_1m"] = self._success_window.rate

            # Recent commands feed
            self._recent_commands.append({
                "ts": time.time(),
                "action": action,
                "route": route,
                "domain": domain,
                "latency_ms": round(latency_ms, 1),
                "success": success,
                "gate": gate,
            })

    def record_llm_latency(self, latency_ms: float, is_cloud: bool = False) -> None:
        """Call after each LLM inference completes."""
        with self._lock:
            key = "cloud_latency_ms" if is_cloud else "llm_latency_ms"
            self._histograms[key].observe(latency_ms)

    def record_whisper_latency(self, latency_ms: float) -> None:
        with self._lock:
            self._histograms["whisper_latency_ms"].observe(latency_ms)

    # ---------------------------------------------------------------------- #
    # Read API
    # ---------------------------------------------------------------------- #

    def get_snapshot(self) -> dict:
        """Return a JSON-serialisable snapshot of all metrics."""
        with self._lock:
            uptime = time.time() - self._start_ts
            return {
                "uptime_s": round(uptime, 1),
                "counters": dict(self._counters),
                "gauges": {k: (_round(v) if isinstance(v, float) else v)
                           for k, v in self._gauges.items()},
                "histograms": {k: h.to_dict() for k, h in self._histograms.items()},
                "source_counts": dict(self._source_counts),
                "domain_counts": dict(self._domain_counts),
                "recent_commands": list(self._recent_commands),
                "ts": time.time(),
            }

    # ---------------------------------------------------------------------- #
    # aiohttp route handler
    # ---------------------------------------------------------------------- #

    async def aiohttp_handler(self, request) -> "aiohttp.web.Response":
        """GET /metrics → JSON snapshot.  Wire into your aiohttp app."""
        try:
            from aiohttp import web
            snapshot = self.get_snapshot()
            return web.Response(
                content_type="application/json",
                text=json.dumps(snapshot, indent=2),
            )
        except Exception as exc:
            from aiohttp import web
            return web.Response(status=500, text=str(exc))

    # ---------------------------------------------------------------------- #
    # VRAM background poller
    # ---------------------------------------------------------------------- #

    async def start_vram_poller(self, interval_s: float = 60.0) -> None:
        """Background task: poll pynvml every `interval_s` seconds."""
        self._vram_task = asyncio.create_task(
            self._vram_poll_loop(interval_s), name="metrics_vram_poller"
        )

    async def _vram_poll_loop(self, interval_s: float) -> None:
        while True:
            try:
                import pynvml as nvml
                nvml.nvmlInit()
                h = nvml.nvmlDeviceGetHandleByIndex(0)
                info = nvml.nvmlDeviceGetMemoryInfo(h)
                nvml.nvmlShutdown()
                self.set("vram_free_gb", round(info.free / (1024 ** 3), 2))
            except Exception:
                pass
            await asyncio.sleep(interval_s)

    def stop_vram_poller(self) -> None:
        if self._vram_task:
            self._vram_task.cancel()
            self._vram_task = None


# ---------------------------------------------------------------------------
# Global singleton
# ---------------------------------------------------------------------------

_METRICS: Optional[Metrics] = None


def get_metrics() -> Metrics:
    """Return the process-wide Metrics singleton (created on first call)."""
    global _METRICS
    if _METRICS is None:
        _METRICS = Metrics()
    return _METRICS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _round(v: float, digits: int = 4) -> float:
    try:
        return round(v, digits)
    except Exception:
        return v
