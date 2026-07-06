"""Tests for core.rate_limiter — TokenBucket + RateLimiter."""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.rate_limiter import _TokenBucket, RateLimiter


# ---------------------------------------------------------------------------
# _TokenBucket unit tests (sync, clock-injectable via monkeypatching)
# ---------------------------------------------------------------------------

def test_bucket_consume_returns_zero_when_token_available():
    bucket = _TokenBucket(max_rps=10.0, burst_capacity=5)
    wait = bucket.consume()
    assert wait == 0.0
    assert bucket._tokens == pytest.approx(4.0, abs=0.01)


def test_bucket_consume_returns_wait_when_empty():
    bucket = _TokenBucket(max_rps=2.0, burst_capacity=1)
    bucket.consume()          # drains the one token
    wait = bucket.consume()
    assert wait > 0.0
    assert wait == pytest.approx(0.5, abs=0.1)  # 1 token at 2 rps = 0.5 s


def test_bucket_refills_over_time(monkeypatch):
    calls = iter([0.0, 1.0])  # monotonic: t=0 at __init__, t=1 when consume() calls _refill()
    monkeypatch.setattr(time, "monotonic", lambda: next(calls))

    bucket = _TokenBucket(max_rps=1.0, burst_capacity=1)
    bucket._tokens = 0.0
    bucket._last_refill = 0.0

    wait = bucket.consume()   # t=1 → refilled 1 token
    assert wait == 0.0        # token available after 1 s


def test_bucket_clamps_tokens_at_burst_capacity():
    bucket = _TokenBucket(max_rps=100.0, burst_capacity=3)
    # Force a large elapsed time to trigger refill beyond capacity
    bucket._last_refill -= 100.0
    bucket._refill()
    assert bucket._tokens == pytest.approx(3.0, abs=0.01)  # capped at burst


def test_bucket_min_rps_and_burst_floor():
    bucket = _TokenBucket(max_rps=0.0, burst_capacity=0)
    assert bucket.max_rps >= 0.01
    assert bucket.burst_capacity >= 1


# ---------------------------------------------------------------------------
# RateLimiter integration tests (with a real in-memory AgentDB)
# ---------------------------------------------------------------------------

async def _make_limiter(tmp_path):
    from storage.db import AgentDB
    db = AgentDB()
    await db.open(tmp_path / "rl.db")
    return RateLimiter(db), db


async def test_check_passes_when_token_available(tmp_path):
    limiter, db = await _make_limiter(tmp_path)
    result = await limiter.check("cloud_api")  # seeded at 2 rps, burst 5
    assert result is True
    await db.close()


async def test_check_drops_when_drop_on_limit_and_empty(tmp_path):
    limiter, db = await _make_limiter(tmp_path)
    # Drain the bucket manually
    bucket = await limiter._get_bucket("cloud_api")
    bucket._tokens = 0.0

    result = await limiter.check("cloud_api", drop_on_limit=True)
    assert result is False
    await db.close()


async def test_check_waits_when_not_drop_on_limit(tmp_path):
    limiter, db = await _make_limiter(tmp_path)
    # Drain the bucket to 0.1 s wait
    bucket = await limiter._get_bucket("ollama")  # seeded 4 rps, burst 8
    bucket._tokens = 0.0
    bucket.max_rps = 100.0  # make wait tiny so test is fast

    t0 = time.monotonic()
    result = await limiter.check("ollama")
    elapsed = time.monotonic() - t0

    assert result is True
    assert elapsed < 0.2  # waited (tiny) but returned True
    await db.close()


async def test_check_fails_open_when_db_unavailable(tmp_path):
    limiter, db = await _make_limiter(tmp_path)
    limiter._db.logs.get_rate_limit_config = AsyncMock(side_effect=RuntimeError("db gone"))
    limiter._buckets.clear()  # force config re-read

    result = await limiter.check("cloud_api")
    assert result is True  # fail-open
    await db.close()


async def test_invalidate_forces_config_reload(tmp_path):
    limiter, db = await _make_limiter(tmp_path)
    await limiter.check("cloud_api")  # loads bucket into cache
    assert "cloud_api" in limiter._buckets

    limiter.invalidate("cloud_api")
    assert "cloud_api" not in limiter._buckets

    # Next check re-loads from DB
    result = await limiter.check("cloud_api")
    assert result is True
    await db.close()


async def test_unknown_resource_fails_open(tmp_path):
    limiter, db = await _make_limiter(tmp_path)
    # "nonexistent_resource" has no config row — get_rate_limit_config returns defaults
    result = await limiter.check("nonexistent_resource")
    assert result is True
    await db.close()


async def test_breach_logged_to_rate_limit_events(tmp_path):
    limiter, db = await _make_limiter(tmp_path)
    bucket = await limiter._get_bucket("cloud_api")
    bucket._tokens = 0.0
    bucket.max_rps = 100.0  # tiny wait

    await limiter.check("cloud_api")  # throttled path

    async with db._conn.execute(
        "SELECT COUNT(*) FROM rate_limit_events WHERE resource = 'cloud_api'"
    ) as cur:
        n = (await cur.fetchone())[0]
    assert n == 1
    await db.close()


async def test_drop_logged_to_rate_limit_events(tmp_path):
    limiter, db = await _make_limiter(tmp_path)
    bucket = await limiter._get_bucket("cloud_api")
    bucket._tokens = 0.0

    await limiter.check("cloud_api", drop_on_limit=True)

    async with db._conn.execute(
        "SELECT was_dropped FROM rate_limit_events WHERE resource = 'cloud_api'"
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == 1  # was_dropped = True
    await db.close()


# ---------------------------------------------------------------------------
# Wait-path token consumption (audit fix 2026-06-09)
# ---------------------------------------------------------------------------

async def test_wait_path_reconsumes_token_after_sleep(tmp_path, monkeypatch):
    """The throttled path must actually take a token after waiting. Previously
    check() returned True without re-consuming, so every throttled request was
    admitted token-free and the effective rate exceeded max_rps."""
    limiter, db = await _make_limiter(tmp_path)
    bucket = await limiter._get_bucket("ollama")
    bucket._tokens = 0.0
    bucket.max_rps = 50.0       # wait ≈ 0.02 s; one token refills during the sleep

    calls = {"n": 0}
    real_consume = bucket.consume

    def _counting():
        calls["n"] += 1
        return real_consume()

    monkeypatch.setattr(bucket, "consume", _counting)

    result = await limiter.check("ollama")
    assert result is True
    # initial consume (empty → wait) + at least one re-consume after the sleep
    assert calls["n"] >= 2
    await db.close()


async def test_throttled_calls_consume_their_tokens(tmp_path):
    """Throttled checks each take a token — the bucket never free-accumulates,
    so the effective admission rate is bounded by max_rps."""
    limiter, db = await _make_limiter(tmp_path)
    bucket = await limiter._get_bucket("ollama")
    bucket._tokens = 0.0
    bucket.max_rps = 50.0       # fast but real waits

    for _ in range(3):
        assert await limiter.check("ollama") is True
    # After three throttled admissions the bucket holds well under a full token —
    # each admission consumed what refilled, no token-free passes.
    assert bucket._tokens < 1.0
    await db.close()


# ---------------------------------------------------------------------------
# Wiring: coordinator throttles cloud egress; limiter shared with CloudDevAgent
# ---------------------------------------------------------------------------

def test_run_cloud_calls_rate_limiter():
    from core.hybrid_coordinator import HybridCoordinator
    from core.command_executor import Command

    coord = HybridCoordinator()
    coord._content_filter = None

    class _Cloud:
        async def infer(self, cmd):
            return "CLICK ok"

    coord._cloud = _Cloud()
    checked = []

    class _RL:
        async def check(self, resource, **kw):
            checked.append(resource)
            return True

    coord._rate_limiter = _RL()
    cmd = Command(text="open browser", action="OPEN", source="voice")
    out = asyncio.run(coord._inference.run_cloud(cmd))
    assert out == "CLICK ok"
    assert checked == ["anthropic"]


def test_set_rate_limiter_propagates_to_cloud_dev_agent():
    from core.hybrid_coordinator import HybridCoordinator

    coord = HybridCoordinator()
    cda = MagicMock()
    coord._cloud_dev_agent = cda
    rl = object()
    coord.set_rate_limiter(rl)
    cda.set_rate_limiter.assert_called_once_with(rl)
    assert coord._rate_limiter is rl
