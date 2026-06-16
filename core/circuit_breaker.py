"""CircuitBreaker — latched failure breaker for inference backends (gap #4).

The coordinator already wraps each inference call in a per-call timeout, but a
backend that is genuinely down still costs the FULL timeout on EVERY request
(the call is attempted, hangs, times out). A latched breaker fixes that: after
`fail_threshold` consecutive failures it OPENS and subsequent calls fail fast
(no network attempt, no wait) until `cooldown_s` elapses; then it HALF-OPENS and
admits one probe — success closes it, failure re-opens it for another cooldown.

A backend can also degrade WITHOUT hard-failing: it stops erroring but every call
crawls in just under the timeout. Pure failure-counting never trips on that — each
slow call still "succeeds", so the breaker stays closed and the user keeps paying
near-timeout latency forever. With `slow_call_s` set, a successful-but-slow call
(latency ≥ `slow_call_s`) counts toward opening too: `slow_threshold` consecutive
slow calls open the breaker, and a slow half-open probe re-opens rather than
closing. A single fast clean call resets both counters.

States:
    closed     — normal; calls allowed; failures counted toward the threshold
    open       — failing fast; calls rejected until cooldown elapses
    half_open   — one probe allowed; its outcome decides closed vs open

Usage:
    cb = CircuitBreaker(name="ollama", fail_threshold=3, cooldown_s=30.0,
                        slow_call_s=12.0)
    if not cb.allow():
        return fast_fallback()
    t0 = monotonic()
    try:
        result = do_call()
        cb.record_success(latency_s=monotonic() - t0)
        return result
    except Exception:
        cb.record_failure()
        raise

The clock is injectable (`time_fn`) for deterministic tests; it defaults to
time.monotonic (never the wall clock — immune to NTP steps).
"""

from __future__ import annotations

import logging
import time
from typing import Callable

log = logging.getLogger(__name__)


class CircuitBreaker:
    def __init__(
        self,
        name: str = "",
        fail_threshold: int = 3,
        cooldown_s: float = 30.0,
        time_fn: Callable[[], float] = time.monotonic,
        slow_call_s: float | None = None,
        slow_threshold: int | None = None,
    ) -> None:
        self._name = name
        self._fail_threshold = max(1, fail_threshold)
        self._cooldown_s = cooldown_s
        self._now = time_fn
        # Timeout-awareness (#4 tail): latency at/above which a *successful* call
        # is "slow"; `slow_threshold` consecutive slow calls open the breaker.
        # None disables slow tracking (pure failure-counting, the old behavior).
        self._slow_call_s = slow_call_s
        self._slow_threshold = max(1, slow_threshold) if slow_threshold else self._fail_threshold
        self._consecutive_slow = 0
        self._state = "closed"
        self._consecutive_failures = 0
        self._opened_at: float = 0.0
        self._half_open_probe_inflight = False
        self._probe_started_at: float = 0.0
        # Monotonic id incremented on every probe admission. A caller captures
        # the generation when admitted (via `probe_gen`) and passes it back to
        # record_success/failure; an outcome whose generation is no longer
        # current (a lost probe that finally returns after a fresh probe was
        # admitted) is ignored, so two probes' outcomes can't be conflated (#16).
        self._probe_gen = 0

    # ── Query ────────────────────────────────────────────────────────────────

    def allow(self) -> bool:
        """True if a call should proceed now. Side-effect: an open breaker whose
        cooldown has elapsed transitions to half-open and admits ONE probe."""
        if self._state == "closed":
            return True
        if self._state == "open":
            if self._now() - self._opened_at >= self._cooldown_s:
                self._state = "half_open"
                self._half_open_probe_inflight = True
                self._probe_started_at = self._now()
                self._probe_gen += 1
                log.info("CircuitBreaker[%s]: half-open — admitting one probe", self._name)
                return True
            return False
        # half_open: admit exactly one probe; reject concurrent callers meanwhile.
        # Self-heal: if a probe was admitted but never reported its outcome (the
        # caller was cancelled before record_success/record_failure), the flag
        # would wedge the breaker shut forever. After cooldown_s with no outcome,
        # treat the probe as lost and admit a fresh one.
        if self._half_open_probe_inflight:
            if self._now() - self._probe_started_at >= self._cooldown_s:
                log.warning("CircuitBreaker[%s]: half-open probe lost (no outcome "
                            "in %.0fs) — admitting a fresh probe", self._name,
                            self._cooldown_s)
                self._probe_started_at = self._now()
                self._probe_gen += 1   # supersede the lost probe's outcome
                return True
            return False
        self._half_open_probe_inflight = True
        self._probe_started_at = self._now()
        self._probe_gen += 1
        return True

    @property
    def state(self) -> str:
        return self._state

    @property
    def probe_gen(self) -> int:
        """Generation of the most recently admitted probe — capture right after a
        successful allow() and pass to record_success/failure (#16)."""
        return self._probe_gen

    # ── Outcome reporting ──────────────────────────────────────────────────────

    def record_success(self, gen: int | None = None,
                        latency_s: float | None = None) -> None:
        if gen is not None and gen != self._probe_gen:
            return   # outcome from a superseded probe (#16) — ignore
        # Slow-but-successful: the backend is degrading, not down. Count it toward
        # opening so a hang-then-barely-respond loop stops costing near-timeout
        # latency on every call. A slow half-open probe is NOT a clean recovery.
        if (self._slow_call_s is not None and latency_s is not None
                and latency_s >= self._slow_call_s):
            self._half_open_probe_inflight = False
            self._consecutive_slow += 1
            if self._state == "half_open":
                self._open(reason=f"slow probe ({latency_s:.1f}s)")
                return
            if self._consecutive_slow >= self._slow_threshold:
                self._open(reason=f"{self._consecutive_slow} slow call(s) "
                                  f">= {self._slow_call_s:.1f}s")
            return   # slow but tolerated — stay in current state (closed)
        # Fast, clean success → fully close and reset both counters.
        if self._state != "closed":
            log.info("CircuitBreaker[%s]: success — closing", self._name)
        self._state = "closed"
        self._consecutive_failures = 0
        self._consecutive_slow = 0
        self._half_open_probe_inflight = False

    def record_failure(self, gen: int | None = None) -> None:
        if gen is not None and gen != self._probe_gen:
            return   # outcome from a superseded probe (#16) — ignore
        self._half_open_probe_inflight = False
        if self._state == "half_open":
            # Probe failed → straight back to open for another cooldown.
            self._open(reason="probe failed")
            return
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._fail_threshold:
            self._open(reason=f"{self._consecutive_failures} failure(s)")

    def _open(self, reason: str = "") -> None:
        if self._state != "open":
            log.warning(
                "CircuitBreaker[%s]: OPEN after %s — fast-failing for %.0fs",
                self._name, reason or "threshold reached", self._cooldown_s,
            )
        self._state = "open"
        self._opened_at = self._now()

    def get_status(self) -> dict:
        return {
            "name": self._name,
            "state": self._state,
            "consecutive_failures": self._consecutive_failures,
            "fail_threshold": self._fail_threshold,
            "cooldown_s": self._cooldown_s,
            "consecutive_slow": self._consecutive_slow,
            "slow_call_s": self._slow_call_s,
            "slow_threshold": self._slow_threshold,
        }
