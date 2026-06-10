"""CircuitBreaker — latched failure breaker for inference backends (gap #4).

The coordinator already wraps each inference call in a per-call timeout, but a
backend that is genuinely down still costs the FULL timeout on EVERY request
(the call is attempted, hangs, times out). A latched breaker fixes that: after
`fail_threshold` consecutive failures it OPENS and subsequent calls fail fast
(no network attempt, no wait) until `cooldown_s` elapses; then it HALF-OPENS and
admits one probe — success closes it, failure re-opens it for another cooldown.

States:
    closed     — normal; calls allowed; failures counted toward the threshold
    open       — failing fast; calls rejected until cooldown elapses
    half_open   — one probe allowed; its outcome decides closed vs open

Usage:
    cb = CircuitBreaker(name="ollama", fail_threshold=3, cooldown_s=30.0)
    if not cb.allow():
        return fast_fallback()
    try:
        result = do_call()
        cb.record_success()
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
    ) -> None:
        self._name = name
        self._fail_threshold = max(1, fail_threshold)
        self._cooldown_s = cooldown_s
        self._now = time_fn
        self._state = "closed"
        self._consecutive_failures = 0
        self._opened_at: float = 0.0
        self._half_open_probe_inflight = False
        self._probe_started_at: float = 0.0

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
                return True
            return False
        self._half_open_probe_inflight = True
        self._probe_started_at = self._now()
        return True

    @property
    def state(self) -> str:
        return self._state

    # ── Outcome reporting ──────────────────────────────────────────────────────

    def record_success(self) -> None:
        if self._state != "closed":
            log.info("CircuitBreaker[%s]: success — closing", self._name)
        self._state = "closed"
        self._consecutive_failures = 0
        self._half_open_probe_inflight = False

    def record_failure(self) -> None:
        self._half_open_probe_inflight = False
        if self._state == "half_open":
            # Probe failed → straight back to open for another cooldown.
            self._open()
            return
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._fail_threshold:
            self._open()

    def _open(self) -> None:
        if self._state != "open":
            log.warning(
                "CircuitBreaker[%s]: OPEN after %d failure(s) — fast-failing for %.0fs",
                self._name, self._consecutive_failures, self._cooldown_s,
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
        }
