"""core/rate_limiter.py — In-process token-bucket rate limiter backed by rate_limit_config.

Each resource has its own TokenBucket loaded from AgentDB on first use. Breaches are
logged to rate_limit_events for observability. The limiter is fail-open: if the DB is
unavailable or the resource is not configured, calls proceed without delay.

Usage::

    limiter = RateLimiter(agent_db)
    await limiter.check("cloud_api", command_id=cmd.id)  # waits if over rate
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from storage.db import AgentDB

log = logging.getLogger(__name__)


class _TokenBucket:
    """Token-bucket with a monotonic clock. Thread-safe via asyncio event loop."""

    def __init__(self, max_rps: float, burst_capacity: int) -> None:
        self.max_rps = max(max_rps, 0.01)
        self.burst_capacity = max(burst_capacity, 1)
        self._tokens: float = float(burst_capacity)
        self._last_refill: float = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self.burst_capacity, self._tokens + elapsed * self.max_rps)
        self._last_refill = now

    def consume(self) -> float:
        """Try to consume one token. Returns wait_s (0.0 if token available, >0 if must wait)."""
        self._refill()
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return 0.0
        # Calculate how long until the next token is available.
        return (1.0 - self._tokens) / self.max_rps


class RateLimiter:
    """Resource-scoped rate limiter. Loads config from AgentDB; fail-open if unavailable."""

    def __init__(self, db: "AgentDB") -> None:
        self._db = db
        self._buckets: dict[str, _TokenBucket] = {}

    async def _get_bucket(self, resource: str) -> _TokenBucket:
        if resource not in self._buckets:
            max_rps, burst = await self._db.get_rate_limit_config(resource)
            self._buckets[resource] = _TokenBucket(max_rps, burst)
        return self._buckets[resource]

    async def check(
        self,
        resource: str,
        *,
        command_id: Optional[int] = None,
        drop_on_limit: bool = False,
    ) -> bool:
        """Enforce rate limit for resource.

        If a token is available, consumes it and returns True immediately.
        If `drop_on_limit` is True, returns False without waiting (caller should CLARIFY).
        Otherwise awaits until a token is available, logs the wait, and returns True.
        """
        try:
            bucket = await self._get_bucket(resource)
            wait_s = bucket.consume()
            if wait_s <= 0.0:
                return True
            if drop_on_limit:
                log.info("RateLimiter: dropping %s request (over rate)", resource)
                await self._db.insert_rate_limit_event(
                    resource, command_id=command_id, wait_ms=0.0, was_dropped=True
                )
                return False
            log.debug("RateLimiter: throttling %s for %.0f ms", resource, wait_s * 1000)
            await self._db.insert_rate_limit_event(
                resource, command_id=command_id, wait_ms=wait_s * 1000, was_dropped=False
            )
            await asyncio.sleep(wait_s)
            return True
        except Exception as exc:
            log.warning("RateLimiter.check failed (fail-open): %s", exc)
            return True  # fail-open

    def invalidate(self, resource: str) -> None:
        """Drop the cached bucket so config is re-read from DB on next check."""
        self._buckets.pop(resource, None)
