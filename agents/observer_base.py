"""agents/observer_base.py — ObserverAgent: reusable EventBus-subscriber substrate.

An ObserverAgent subscribes to one or more EventBus topic patterns and reacts to
matching events. It is the choreography counterpart to the central DevAgent
orchestration: new agents plug into the bus instead of the orchestration thread.

Lifecycle mirrors the canonical supervised loop (ResourceGovernor / EmailWatcher)
so the Supervisor (core/supervisor.py) can watch it:

    is_alive=agent.is_healthy   restart=agent.restart   enabled=lambda: agent._running

Subclasses implement:
    topics()        -> list[str]   SQL-LIKE patterns (e.g. ["command.%", "voice.drift"])
    on_event(evt)   -> awaitable   handle one matching event envelope

The base spawns one subscription task per pattern, drains the bus, swallows handler
exceptions (logged) so one bad event never kills the loop, and re-raises
CancelledError for structured shutdown.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod

log = logging.getLogger(__name__)


class ObserverAgent(ABC):
    def __init__(self, event_bus, name: str) -> None:
        self._bus = event_bus
        self._name = name
        self._tasks: list[asyncio.Task] = []
        self._running: bool = False

    # ── Subclass contract ─────────────────────────────────────────────────────

    @abstractmethod
    def topics(self) -> list[str]:
        """Return the SQL-LIKE topic patterns this observer subscribes to."""

    @abstractmethod
    async def on_event(self, evt: dict) -> None:
        """Handle one event envelope (keys: id, ts, topic, source, payload, ...)."""

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._running:
            return
        if self._bus is None:
            log.warning("%s: no event bus wired — observer inert", self._name)
            return
        self._running = True
        self._tasks = [
            asyncio.create_task(self._consume(p), name=f"{self._name}:{p}")
            for p in self.topics()
        ]
        log.info("%s started (topics=%s)", self._name, self.topics())

    async def _consume(self, pattern: str) -> None:
        consumer = f"{self._name}:{pattern}"
        try:
            async for evt in self._bus.subscribe(consumer, pattern):
                if not self._running:
                    break
                try:
                    await self.on_event(evt)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("%s.on_event error (continuing): %s", self._name, exc)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.warning("%s subscription loop error: %s", self._name, exc)

    async def stop(self) -> None:
        self._running = False
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        self._tasks = []

    # ── Supervision (Supervisor-compatible) ─────────────────────────────────────

    def is_healthy(self) -> bool:
        return (
            self._running
            and bool(self._tasks)
            and all(not t.done() for t in self._tasks)
        )

    async def restart(self) -> None:
        """Relaunch any dead subscription tasks; keep the live ones. Idempotent."""
        if not self._running:
            return
        patterns = self.topics()
        new_tasks: list[asyncio.Task] = []
        for i, p in enumerate(patterns):
            existing = self._tasks[i] if i < len(self._tasks) else None
            if existing is not None and not existing.done():
                new_tasks.append(existing)
            else:
                new_tasks.append(
                    asyncio.create_task(self._consume(p), name=f"{self._name}:{p}")
                )
        self._tasks = new_tasks
        log.warning("%s: subscription task(s) relaunched by supervisor", self._name)
