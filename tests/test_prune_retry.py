"""F4 — startup prune retries on a locked database instead of silently skipping.

``PRAGMA busy_timeout`` only auto-retries ``SQLITE_BUSY``. A "database table is
locked" (``SQLITE_LOCKED``) — seen at startup when a just-killed previous
process still holds the WAL lock, observed live as
``AgentDB.telemetry.prune_sensor_telemetry failed: database table is locked`` — is NOT
retried by SQLite, so the prune skipped and ``sensor_telemetry`` grew unbounded
(the exact thing the prune exists to prevent). ``_prune_with_retry`` now backs
off and retries, staying non-fatal.

Run:
    python -m pytest tests/test_prune_retry.py -q
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import storage.db as db
from storage.db import AgentDB


class _Result:
    """Awaitable + async-context-manager cursor stand-in (like aiosqlite)."""

    def __init__(self, rowcount: int = 0):
        self.rowcount = rowcount

    def __await__(self):
        async def _c():
            return self
        return _c().__await__()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeConn:
    """Fails the DELETE `fail_times` times with SQLITE_LOCKED, then succeeds."""

    def __init__(self, fail_times: int, rowcount: int = 5):
        self.fail_times = fail_times
        self.rowcount = rowcount
        self.delete_calls = 0
        self.commits = 0
        self.checkpoints = 0

    def execute(self, sql, params=None):
        s = sql.strip().upper()
        if s.startswith("PRAGMA"):
            self.checkpoints += 1
            return _Result(0)
        # DELETE path
        self.delete_calls += 1
        if self.delete_calls <= self.fail_times:
            raise sqlite3.OperationalError("database table is locked")
        return _Result(self.rowcount)

    async def commit(self):
        self.commits += 1


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _instant(*_a, **_k):
        return None
    monkeypatch.setattr(db.asyncio, "sleep", _instant)


def _db(conn) -> AgentDB:
    d = AgentDB()
    d._conn = conn
    return d


async def test_prune_succeeds_after_transient_lock():
    conn = _FakeConn(fail_times=2)
    d = _db(conn)
    deleted = await d._prune_with_retry(
        "DELETE FROM sensor_telemetry WHERE ts < ?", (0,),
        label="sensor_telemetry rows (> 7 days)", checkpoint=True,
    )
    assert deleted == 5
    assert conn.delete_calls == 3          # 2 locked attempts + 1 success
    assert conn.commits == 1
    assert conn.checkpoints == 1           # checkpoint ran on the successful pass


async def test_prune_gives_up_after_attempts_non_fatal():
    conn = _FakeConn(fail_times=99)        # never recovers
    d = _db(conn)
    deleted = await d._prune_with_retry(
        "DELETE FROM sensor_telemetry WHERE ts < ?", (0,),
        label="sensor_telemetry rows (> 7 days)", attempts=4,
    )
    assert deleted == 0                     # non-fatal: returns 0, never raises
    assert conn.delete_calls == 4           # exactly `attempts` tries
    assert conn.commits == 0


async def test_prune_no_conn_returns_zero():
    d = _db(None)
    assert await d._prune_with_retry("DELETE FROM x WHERE ts < ?", (0,), label="x") == 0


async def test_non_lock_error_not_retried():
    class _BadConn(_FakeConn):
        def execute(self, sql, params=None):
            if sql.strip().upper().startswith("PRAGMA"):
                return _Result(0)
            self.delete_calls += 1
            raise sqlite3.OperationalError("no such table: sensor_telemetry")
    conn = _BadConn(fail_times=0)
    d = _db(conn)
    deleted = await d._prune_with_retry(
        "DELETE FROM sensor_telemetry WHERE ts < ?", (0,), label="x", attempts=4,
    )
    assert deleted == 0
    assert conn.delete_calls == 1           # a non-lock error fails fast (no retry)
