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

    # Hard liveness cap on how long a throttled call will wait before failing
    # open (admitting it) rather than starving. The per-call cloud timeout
    # bounds the real wait below this; this is just a backstop.
    _MAX_WAIT_S = 15.0

    def __init__(self, db: "AgentDB") -> None:
        self._db = db
        self._buckets: dict[str, _TokenBucket] = {}
        # Per-resource init locks: prevents two concurrent first-callers for the
        # same resource from both entering the DB fetch (double-init race, #9).
        # asyncio.Lock is safe here because _get_bucket is only called from the
        # event loop (never from a thread). The lock dict itself is written
        # without an await between check and set, so no race on dict mutation.
        self._init_locks: dict[str, asyncio.Lock] = {}

    async def _get_bucket(self, resource: str) -> _TokenBucket:
        if resource not in self._buckets:
            # Create the per-resource lock if needed. No await between the check
            # and the assignment, so this dict write is race-free in the event loop.
            if resource not in self._init_locks:
                self._init_locks[resource] = asyncio.Lock()
            async with self._init_locks[resource]:
                # Double-check: another coroutine may have populated the bucket
                # while we were waiting for the lock.
                if resource not in self._buckets:
                    max_rps, burst = await self._db.logs.get_rate_limit_config(resource)
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
                await self._db.events.insert_rate_limit_event(
                    resource, command_id=command_id, wait_ms=0.0, was_dropped=True
                )
                return False
            log.debug("RateLimiter: throttling %s for %.0f ms", resource, wait_s * 1000)
            await self._db.events.insert_rate_limit_event(
                resource, command_id=command_id, wait_ms=wait_s * 1000, was_dropped=False
            )
            # Wait, then ACTUALLY consume the token. The first consume() above
            # returned a wait time WITHOUT taking a token; without re-consuming
            # here every throttled request would be admitted token-free, so the
            # effective rate would exceed max_rps under sustained load. Loop
            # until a token is genuinely consumed, bounded by a wall-clock
            # deadline (#17). A fixed retry count let losers of a contention race
            # fall through and admit token-free; a deadline keeps enforcing the
            # rate (re-waiting for the next token) up to the liveness cap.
            deadline = time.monotonic() + self._MAX_WAIT_S
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    break
                await asyncio.sleep(min(wait_s, remaining))
                wait_s = bucket.consume()
                if wait_s <= 0.0:
                    return True
            # Hard cap exceeded — fail-open to avoid starving the call (the call's
            # own timeout normally fires first under a saturated bucket).
            log.warning("RateLimiter: %s exceeded %.0fs wait cap — admitting (fail-open)",
                        resource, self._MAX_WAIT_S)
            return True
        except Exception as exc:
            log.warning("RateLimiter.check failed (fail-open): %s", exc)
            return True  # fail-open

    def invalidate(self, resource: str) -> None:
        """Drop the cached bucket so config is re-read from DB on next check."""
        self._buckets.pop(resource, None)
