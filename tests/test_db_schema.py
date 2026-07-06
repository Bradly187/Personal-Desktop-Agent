"""Schema-integrity tripwires for storage/db.py — the DB schema source of truth.

storage/db.py is authoritative for the agent.db schema, table count, and
PRAGMA user_version (AGENTS.md #1). It is exercised indirectly by dozens of
suites but had no dedicated test pinning the schema contract itself.

This file has two layers:

* **Pure tripwires** (no aiosqlite) — parse AGENT_DB_SCHEMA and assert the table
  count and version constants. These run in CI regardless of whether the async
  sqlite driver is installed, so an accidental schema change always trips a gate.
* **Live-DB checks** (skip if aiosqlite absent) — open a fresh temp DB and assert
  the materialised schema matches the declared one, that re-open is idempotent,
  and that core writes round-trip / degrade safely when unopened.

If you intentionally add or drop a table, update `_EXPECTED_TABLE_COUNT` here AND
the authoritative count in CLAUDE.md's schema fact (that's the point of the gate).
"""

import re

import pytest

from storage.db import AGENT_DB_SCHEMA, _AGENT_DB_SCHEMA_VERSION

# Authoritative as of 2026-07-01: 49 tables at user_version 9
# (v9 = sensor_telemetry.trace_id, sensor→command trace correlation).
_EXPECTED_TABLE_COUNT = 51
_EXPECTED_USER_VERSION = 9

# Paren-anchored so a stray "CREATE TABLE IF NOT EXISTS is a …" in a comment is
# NOT counted (the name must be immediately followed by the column list).
_TABLE_RE = re.compile(r"CREATE TABLE IF NOT EXISTS\s+(\w+)\s*\(")


def _schema_table_names() -> set[str]:
    return set(_TABLE_RE.findall(AGENT_DB_SCHEMA))


# --------------------------------------------------------------------------- #
# Pure tripwires — always run (no aiosqlite needed)
# --------------------------------------------------------------------------- #
def test_schema_declares_expected_table_count():
    names = _schema_table_names()
    assert len(names) == _EXPECTED_TABLE_COUNT, (
        f"AGENT_DB_SCHEMA declares {len(names)} tables, expected "
        f"{_EXPECTED_TABLE_COUNT}. If this is an intentional schema change, "
        "update _EXPECTED_TABLE_COUNT here and the schema fact in CLAUDE.md "
        "(AGENTS.md #1)."
    )


def test_schema_table_names_are_unique():
    names = _TABLE_RE.findall(AGENT_DB_SCHEMA)
    assert len(names) == len(set(names)), "duplicate CREATE TABLE in AGENT_DB_SCHEMA"


def test_schema_version_constant_matches_expected():
    assert _AGENT_DB_SCHEMA_VERSION == _EXPECTED_USER_VERSION


# --------------------------------------------------------------------------- #
# Live-DB checks — require the async sqlite driver
# --------------------------------------------------------------------------- #
aiosqlite = pytest.importorskip("aiosqlite")

from storage.db import AgentDB  # noqa: E402  (after importorskip)

# asyncio_mode = auto (pytest.ini) runs the async tests below — no module mark
# needed, and a module mark would wrongly tag the sync tripwires above.


async def _open_temp(tmp_path, name="agent.db"):
    db = AgentDB()
    await db.open(tmp_path / name)
    return db


async def _live_table_names(db) -> set[str]:
    cur = await db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )
    return {r[0] for r in await cur.fetchall()}


async def _user_version(db) -> int:
    cur = await db._conn.execute("PRAGMA user_version")
    return (await cur.fetchone())[0]


async def test_fresh_db_opens_at_expected_version(tmp_path):
    db = await _open_temp(tmp_path)
    try:
        assert db.available is True
        assert await _user_version(db) == _EXPECTED_USER_VERSION
    finally:
        await db.close()


async def test_live_tables_match_declared_schema(tmp_path):
    db = await _open_temp(tmp_path)
    try:
        live = await _live_table_names(db)
        assert live == _schema_table_names()
        assert len(live) == _EXPECTED_TABLE_COUNT
    finally:
        await db.close()


async def test_reopen_is_idempotent(tmp_path):
    db = await _open_temp(tmp_path)
    first_tables = await _live_table_names(db)
    await db.close()

    # Re-opening the same file must not migrate again or alter the schema.
    db2 = AgentDB()
    await db2.open(tmp_path / "agent.db")
    try:
        assert await _user_version(db2) == _EXPECTED_USER_VERSION
        assert await _live_table_names(db2) == first_tables
    finally:
        await db2.close()


async def test_insert_session_roundtrips(tmp_path):
    db = await _open_temp(tmp_path)
    try:
        sid = await db.sessions.insert_session(mode="test", git_hash="deadbeef")
        assert sid > 0
        cur = await db._conn.execute(
            "SELECT mode, git_hash FROM sessions WHERE id = ?", (sid,)
        )
        row = await cur.fetchone()
        assert row is not None
        assert row[0] == "test"
        assert row[1] == "deadbeef"
    finally:
        await db.close()


async def test_methods_degrade_safely_when_not_opened():
    # A never-opened AgentDB must not crash — writes no-op with a sentinel.
    db = AgentDB()
    assert db.available is False
    assert await db.sessions.insert_session(mode="x") == -1
