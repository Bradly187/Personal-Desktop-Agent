"""Supervisor — liveness watchdog + restart policy for long-lived subsystems.

Closes orchestration gap #2: nothing previously detected or recovered a dead
background loop (scheduler worker, resource-governor poll loop, ...). Each such
subsystem owns an asyncio.Task that, by convention, should run for the whole
session. If that task dies unexpectedly — an uncaught path, an errant cancel,
a wedged await — the subsystem silently stops doing its job with no signal.

The Supervisor polls a liveness predicate per registered subsystem and, when one
is found dead *while it is still supposed to be running*, restarts it under a
bounded policy:

  - At most `max_restarts` restarts within `window_s`. Exceeding that latches the
    subsystem FAILED — we stop restarting (a crash-loop is worse than a clean
    stop) and log an ERROR + bump a metric for the operator to see.
  - A subsystem whose own `enabled()` returns False (e.g. its `_running` flag is
    False because shutdown set it) is never restarted — the Supervisor must not
    fight a deliberate teardown.

This is an Erlang-style one-for-one supervisor scoped to the few critical loops,
deliberately small: it does NOT manage start-up ordering (main.py does) and it
does NOT restart on a single transient — only on death of the supervised task.

Wire-up (main.py):
    supervisor = Supervisor()
    supervisor.supervise(SupervisedSpec(
        name="scheduler",
        is_alive=scheduler.is_healthy,
        restart=scheduler.restart,
        enabled=lambda: scheduler._running,
    ))
    supervisor.supervise(SupervisedSpec(
        name="resource_governor",
        is_alive=governor.is_healthy,
        restart=governor.restart,
        enabled=lambda: governor._running,
    ))
    await supervisor.start()
    supervisor.set_metrics(m)
    shutdown.register(supervisor)   # registered LAST → stopped FIRST (reversed order)
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

log = logging.getLogger(__name__)


@dataclass
class SupervisedSpec:
    """One supervised subsystem.

    name:     stable identifier (used in logs + metrics).
    is_alive: cheap, non-blocking predicate — True when the subsystem's
              background task is running and healthy.
    restart:  async callable that brings the subsystem back. MUST be idempotent
              and MUST NOT raise on an already-healthy subsystem (it may no-op).
    enabled:  True while the subsystem is *supposed* to be running. When False
              (e.g. shutdown set its _running flag), the Supervisor leaves it
              alone. Defaults to always-enabled.
    last_heartbeat: optional — returns the monotonic timestamp of the loop's last
              iteration. Lets the Supervisor catch a loop that is ALIVE BUT STUCK
              (wedged on a hung await / livelocked) — `is_alive()` alone only
              catches a crashed/exited task. Only meaningful for PERIODIC loops; an
              event-driven loop that legitimately blocks idle (scheduler worker,
              event_rule_engine) must leave this None or it would false-trip.
    stale_after_s: optional — paired with last_heartbeat. If `now - last_heartbeat()`
              exceeds this, the loop is treated as dead and restarted. Size it well
              above the loop's natural period + its slowest legitimate iteration.
    """
    name: str
    is_alive: Callable[[], bool]
    restart: Callable[[], Awaitable[None]]
    enabled: Callable[[], bool] = field(default=lambda: True)
    last_heartbeat: Optional[Callable[[], float]] = None
    stale_after_s: Optional[float] = None


@dataclass
class _SupervisedState:
    spec: SupervisedSpec
    restart_times: list = field(default_factory=list)  # monotonic timestamps
    total_restarts: int = 0
    failed: bool = False           # latched after exceeding the restart budget
    disabled_logged: bool = False  # E21 — one-shot log on enabled()→False flip


class Supervisor:
    """One-for-one liveness supervisor with a bounded restart policy."""

    def __init__(
        self,
        interval_s: float = 2.0,
        max_restarts: int = 5,
        window_s: float = 60.0,
        max_total_restarts: int = 20,
        on_failed: Optional[Callable[[str], object]] = None,
    ) -> None:
        self._interval_s = interval_s
        self._max_restarts = max_restarts
        self._window_s = window_s
        # Absolute lifetime ceiling: the sliding window alone lets a subsystem
        # that re-dies just under the window edge crash-loop indefinitely. A
        # total-restart cap latches FAILED once a persistent slow loop has racked
        # up this many restarts over the whole session (#22).
        self._max_total_restarts = max_total_restarts
        self._subsystems: dict[str, _SupervisedState] = {}
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._metrics = None
        # Escalation hook (gap E): invoked ONCE per subsystem when it latches
        # FAILED. May be sync or async. Used to notify the user (TTS/iPad) and
        # trigger degraded-mode fallback — detection alone is not enough for an
        # unattended user. A raising handler never destabilises the loop.
        self._on_failed = on_failed

    # ── Registration / wiring ────────────────────────────────────────────────

    def supervise(self, spec: SupervisedSpec) -> None:
        """Register a subsystem to be watched. Call before or after start()."""
        self._subsystems[spec.name] = _SupervisedState(spec=spec)
        log.info("Supervisor: now supervising %r", spec.name)

    def set_metrics(self, metrics) -> None:
        self._metrics = metrics

    def set_on_failed(self, handler: Optional[Callable[[str], object]]) -> None:
        """Register the escalation handler invoked once when a subsystem latches FAILED."""
        self._on_failed = handler

    async def _invoke_failed(self, name: str) -> None:
        """Fire the escalation handler (sync or async) once, defensively."""
        if self._on_failed is None:
            return
        try:
            result = self._on_failed(name)
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:
            log.error("Supervisor: on_failed(%r) handler raised: %s", name, exc)

    def _bump(self, name: str, value: int) -> None:
        if self._metrics is None:
            return
        try:
            self._metrics.set(f"supervisor_restarts_total::{name}", value)
        except Exception:
            pass

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._running:
            log.warning("Supervisor.start() called while already running — ignored")
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="supervisor")
        log.info(
            "Supervisor started (interval=%.1fs, budget=%d restarts / %.0fs)",
            self._interval_s, self._max_restarts, self._window_s,
        )

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("Supervisor stopped")

    # ── Core loop ────────────────────────────────────────────────────────────

    async def _loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self._interval_s)
                await self._check_once()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # the supervisor itself must never die
                log.warning("Supervisor._loop error: %s", exc)

    async def _check_once(self) -> None:
        """One pass over all supervised subsystems. Extracted for testability."""
        now = time.monotonic()
        for state in self._subsystems.values():
            spec = state.spec
            if state.failed:
                continue
            # Respect deliberate teardown: don't supervise a disabled subsystem.
            try:
                if not spec.enabled():
                    # E21: surface the transition once. A subsystem silently going
                    # disabled (its _running flag cleared by something other than a
                    # clean shutdown) would otherwise mask a crash — the supervisor
                    # just stops watching it with no trace.
                    if not state.disabled_logged:
                        log.warning(
                            "Supervisor: %r is no longer enabled — pausing "
                            "supervision (no restarts until it re-enables)",
                            spec.name,
                        )
                        state.disabled_logged = True
                    continue
                state.disabled_logged = False  # re-enabled → re-arm the one-shot
                alive = spec.is_alive()
                stale = self._is_stale(spec, now)
                if alive and not stale:
                    continue
            except Exception as exc:
                log.warning("Supervisor: %r is_alive/enabled raised: %s", spec.name, exc)
                continue

            # Subsystem is enabled but its task is dead OR alive-but-wedged
            # (heartbeat stale) → restart under budget.
            # Prune restart timestamps outside the sliding window first.
            state.restart_times = [t for t in state.restart_times if now - t < self._window_s]
            if (len(state.restart_times) >= self._max_restarts
                    or state.total_restarts >= self._max_total_restarts):
                state.failed = True
                log.error(
                    "Supervisor: %r exceeded restart budget (%d in %.0fs, %d total) — "
                    "LATCHED FAILED, no further restarts",
                    spec.name, self._max_restarts, self._window_s, state.total_restarts,
                )
                self._bump(f"{spec.name}::failed", 1)
                # Escalate (gap E): notify the user + degrade. Fires exactly once
                # — a FAILED subsystem is skipped on every subsequent pass above.
                await self._invoke_failed(spec.name)
                continue

            state.restart_times.append(now)
            state.total_restarts += 1
            reason = "stale (heartbeat stuck)" if (alive and stale) else "down"
            log.warning(
                "Supervisor: %r is %s — restarting (attempt %d in window, %d total)",
                spec.name, reason, len(state.restart_times), state.total_restarts,
            )
            try:
                await spec.restart()
                self._bump(spec.name, state.total_restarts)
            except Exception as exc:
                log.error("Supervisor: restart of %r raised: %s", spec.name, exc)

    def _is_stale(self, spec: SupervisedSpec, now: float) -> bool:
        """True when a periodic loop has gone too long without a heartbeat.

        Catches the alive-but-wedged case `is_alive()` misses. No-op for specs
        without a heartbeat (event-driven loops that idle legitimately).
        """
        if spec.last_heartbeat is None or spec.stale_after_s is None:
            return False
        try:
            hb = spec.last_heartbeat()
        except Exception:
            return False
        # 0.0 = never beaten (pre-start); start() seeds a real monotonic value, so
        # a 0 here only happens before launch — don't false-trip on it.
        if not hb:
            return False
        return (now - hb) > spec.stale_after_s

    # ── Status ───────────────────────────────────────────────────────────────

    def get_status(self) -> dict:
        now = time.monotonic()
        return {
            "running": self._running,
            "supervised": {
                name: {
                    "total_restarts": st.total_restarts,
                    "failed": st.failed,
                    "alive": _safe_call(st.spec.is_alive),
                    "enabled": _safe_call(st.spec.enabled),
                    "heartbeat_age_s": _heartbeat_age(st.spec, now),
                }
                for name, st in self._subsystems.items()
            },
        }


def _heartbeat_age(spec: SupervisedSpec, now: float) -> Optional[float]:
    """Seconds since the spec's last heartbeat, or None if it has none / unseeded."""
    if spec.last_heartbeat is None:
        return None
    try:
        hb = spec.last_heartbeat()
    except Exception:
        return None
    return round(now - hb, 1) if hb else None


def _safe_call(fn: Callable[[], bool]) -> Optional[bool]:
    try:
        return bool(fn())
    except Exception:
        return None
