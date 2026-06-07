"""Tranche 3 robustness tests for the RAG / knowledge-base layer.

Covers:
  C1 — versioned migration shim (PRAGMA user_version gate, narrowed except)
  C2 — chunk sub-splitting (_split_oversized / _emit_chunks) replaces truncation
  C3 — file-watcher debounce (coalesce save bursts into one re-index)
  C4 — time-gated _available re-probe (CodebaseIndexer + SemanticMemory)
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from inference.codebase_indexer import (
    CodebaseIndexer,
    Chunk,
    _split_oversized,
    _emit_chunks,
    _MAX_CHUNK_CHARS,
)
from storage.semantic_memory import SemanticMemory


# ===========================================================================
# C2 — chunk sub-splitting
# ===========================================================================

class TestSplitOversized:
    def test_short_text_returned_unchanged(self):
        assert _split_oversized("hello world") == ["hello world"]

    def test_oversized_text_reconstructs(self):
        text = "\n".join(f"line {i} " + "x" * 50 for i in range(500))  # >> 4000 chars
        parts = _split_oversized(text)
        assert len(parts) >= 2
        for p in parts:
            assert len(p) <= _MAX_CHUNK_CHARS
        # Concatenation (re-joining on the stripped newlines) recovers the source
        assert "".join(parts).replace("\n", "") == text.replace("\n", "")

    def test_prefers_line_boundaries(self):
        # Each line is 100 chars; a split should land on a newline, never mid-line
        text = "\n".join("y" * 100 for _ in range(100))  # 100 lines → ~10100 chars
        parts = _split_oversized(text)
        assert len(parts) >= 2
        for p in parts[:-1]:
            # No partial line: every full piece is a whole number of 100-char lines
            assert all(len(line) == 100 for line in p.split("\n") if line)

    def test_no_newline_hard_cut(self):
        text = "z" * (_MAX_CHUNK_CHARS * 2 + 17)
        parts = _split_oversized(text)
        assert len(parts) == 3
        assert "".join(parts) == text


class TestEmitChunks:
    def test_single_chunk_no_suffix(self):
        chunks: list[Chunk] = []
        _emit_chunks(chunks, file="a.py", chunk_type="function", name="foo", text="body")
        assert len(chunks) == 1
        assert chunks[0].name == "foo"

    def test_oversized_emits_suffixed_subchunks(self):
        chunks: list[Chunk] = []
        big = "\n".join("w" * 100 for _ in range(100))  # ~10100 chars
        _emit_chunks(chunks, file="a.py", chunk_type="class", name="Big", text=big)
        assert len(chunks) >= 2
        n = len(chunks)
        for i, c in enumerate(chunks, 1):
            assert c.name == f"Big_({i}/{n})"
            assert c.chunk_type == "class"
            assert len(c.text) <= _MAX_CHUNK_CHARS


# ===========================================================================
# C4 — time-gated _available re-probe
# ===========================================================================

class TestSemanticMemoryReprobe:
    @pytest.mark.asyncio
    async def test_never_started_does_not_reprobe(self):
        mem = SemanticMemory(agent_db=None)
        mem.start = AsyncMock(return_value=False)
        assert not mem._started_once
        await mem._maybe_reprobe()
        mem.start.assert_not_called()

    @pytest.mark.asyncio
    async def test_started_unavailable_and_stale_reprobes(self):
        mem = SemanticMemory(agent_db=None)
        mem._started_once = True
        mem._available = False
        mem._last_probe_ts = time.monotonic() - 1000  # well past the interval
        mem.start = AsyncMock(return_value=False)
        await mem._maybe_reprobe()
        mem.start.assert_called_once()

    @pytest.mark.asyncio
    async def test_recent_probe_is_throttled(self):
        mem = SemanticMemory(agent_db=None)
        mem._started_once = True
        mem._available = False
        mem._last_probe_ts = time.monotonic()  # just probed
        mem.start = AsyncMock(return_value=False)
        await mem._maybe_reprobe()
        mem.start.assert_not_called()

    @pytest.mark.asyncio
    async def test_available_skips_reprobe(self):
        mem = SemanticMemory(agent_db=None)
        mem._started_once = True
        mem._available = True
        mem.start = AsyncMock(return_value=True)
        await mem._maybe_reprobe()
        mem.start.assert_not_called()


class TestCodebaseIndexerReprobe:
    @pytest.mark.asyncio
    async def test_never_started_does_not_reprobe(self):
        idx = CodebaseIndexer(project_root=".")
        idx.start = AsyncMock(return_value=False)
        await idx._maybe_reprobe()
        idx.start.assert_not_called()

    @pytest.mark.asyncio
    async def test_started_unavailable_and_stale_reprobes(self):
        idx = CodebaseIndexer(project_root=".")
        idx._started_once = True
        idx._available = False
        idx._last_probe_ts = time.monotonic() - 1000
        idx.start = AsyncMock(return_value=False)
        await idx._maybe_reprobe()
        idx.start.assert_called_once()


# ===========================================================================
# C3 — watcher debounce
# ===========================================================================

class TestWatcherDebounce:
    @pytest.mark.asyncio
    async def test_rapid_events_coalesce_to_single_reindex(self):
        idx = CodebaseIndexer(project_root=".")
        idx.WATCH_DEBOUNCE_S = 0.05  # instance override for a fast test
        idx._watch_loop = asyncio.get_running_loop()
        calls: list[Path] = []

        async def _fake_changed(path):
            calls.append(path)

        idx._on_file_changed = _fake_changed
        p = Path("module.py")

        # Five rapid "saves" of the same file within the debounce window
        for _ in range(5):
            idx._debounced_reindex(p)
        # Exactly one pending timer for the path (each call cancelled the prior)
        assert len(idx._debounce_timers) == 1

        await asyncio.sleep(0.15)
        assert len(calls) == 1  # coalesced into a single re-index
        assert idx._debounce_timers == {}

    @pytest.mark.asyncio
    async def test_distinct_paths_each_fire(self):
        idx = CodebaseIndexer(project_root=".")
        idx.WATCH_DEBOUNCE_S = 0.05
        idx._watch_loop = asyncio.get_running_loop()
        calls: list[str] = []

        async def _fake_changed(path):
            calls.append(str(path))

        idx._on_file_changed = _fake_changed
        idx._debounced_reindex(Path("a.py"))
        idx._debounced_reindex(Path("b.swift"))
        await asyncio.sleep(0.15)
        assert sorted(calls) == ["a.py", "b.swift"]

    @pytest.mark.asyncio
    async def test_non_source_file_ignored(self):
        idx = CodebaseIndexer(project_root=".")
        idx._watch_loop = asyncio.get_running_loop()
        idx._debounced_reindex(Path(".codebase_index_state.json"))
        assert idx._debounce_timers == {}  # no timer churn for non-source writes


# ===========================================================================
# C1 — versioned migration shim
# ===========================================================================

class TestMigrationShim:
    @pytest.mark.asyncio
    async def test_fresh_db_sets_user_version(self):
        from storage.db import AgentDB, _AGENT_DB_SCHEMA_VERSION
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            path = os.path.join(d, "agent.db")
            db = AgentDB()
            await db.open(path)
            try:
                cur = await db._conn.execute("PRAGMA user_version")
                assert (await cur.fetchone())[0] == _AGENT_DB_SCHEMA_VERSION
                # trace_id column is present on a fresh schema
                cur = await db._conn.execute("PRAGMA table_info(commands)")
                cols = {r[1] for r in await cur.fetchall()}
                assert "trace_id" in cols
            finally:
                await db.close()

    @pytest.mark.asyncio
    async def test_pre_migration_db_gets_column_added(self):
        import aiosqlite
        from storage.db import AgentDB, _AGENT_DB_SCHEMA_VERSION
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            path = os.path.join(d, "agent.db")
            # Simulate an old DB: a commands table WITHOUT trace_id, user_version 0
            conn = await aiosqlite.connect(path)
            await conn.execute(
                "CREATE TABLE commands (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "session_id INTEGER, ts REAL, source TEXT, text TEXT, action TEXT)"
            )
            await conn.execute("PRAGMA user_version = 0")
            await conn.commit()
            await conn.close()

            db = AgentDB()
            await db.open(path)
            try:
                cur = await db._conn.execute("PRAGMA table_info(commands)")
                cols = {r[1] for r in await cur.fetchall()}
                assert "trace_id" in cols  # migration ALTER added it
                cur = await db._conn.execute("PRAGMA user_version")
                assert (await cur.fetchone())[0] == _AGENT_DB_SCHEMA_VERSION
            finally:
                await db.close()

    @pytest.mark.asyncio
    async def test_reopen_is_noop(self):
        from storage.db import AgentDB, _AGENT_DB_SCHEMA_VERSION
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            path = os.path.join(d, "agent.db")
            db = AgentDB()
            await db.open(path)
            await db.close()
            # Re-open: _migrate must see version >= SCHEMA_VERSION and skip
            db2 = AgentDB()
            await db2.open(path)
            try:
                assert db2.available
                cur = await db2._conn.execute("PRAGMA user_version")
                assert (await cur.fetchone())[0] == _AGENT_DB_SCHEMA_VERSION
            finally:
                await db2.close()

    @pytest.mark.asyncio
    async def test_genuine_ddl_error_is_logged_not_swallowed(self, caplog, monkeypatch):
        import logging
        import storage.db as dbmod
        from storage.db import AgentDB
        # Inject a migration targeting a non-existent table — a real error that
        # must be logged (narrowed except), not silently passed.
        monkeypatch.setattr(
            dbmod, "_AGENT_DB_MIGRATIONS",
            (("no_such_table", "bogus", "TEXT"),),
        )
        monkeypatch.setattr(dbmod, "_AGENT_DB_SCHEMA_VERSION", 99)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            path = os.path.join(d, "agent.db")
            db = AgentDB()
            with caplog.at_level(logging.WARNING):
                await db.open(path)  # must NOT raise — degrade gracefully
            try:
                assert db.available  # core schema applied; startup survived
                assert "migration ALTER" in caplog.text.lower() or "migration" in caplog.text.lower()
            finally:
                await db.close()
