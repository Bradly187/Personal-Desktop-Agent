"""PR 0 (R-2 foundation) — episodic_memory table + MemoryManager façade.

Covers insert/query/filter/touch/prune in AgentDB and the recall_episodic /
write_memory_note façade. Embeddings are optional (MiniLM may be absent); recall
falls back to Jaccard, so these tests pass with or without sentence-transformers.
"""
import os
import tempfile

import pytest

from storage.db import AgentDB
from storage.memory_manager import MemoryManager


async def _open_db():
    d = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    db = AgentDB()
    await db.open(os.path.join(d.name, "agent.db"))
    return db, d


async def test_insert_and_query_episodic():
    db, d = await _open_db()
    try:
        rid = await db.memory.insert_episodic_memory(
            "recovery", "fix the failing parser test",
            "Ran pytest, the parser test failed on a None guard; added the guard and it passed.",
            domain="code",
        )
        assert rid and rid > 0
        hits = await db.memory.query_episodic_memory("parser test failing", n=5)
        assert hits, "expected to recall the note via word overlap"
        assert hits[0]["goal"] == "fix the failing parser test"
        assert hits[0]["kind"] == "recovery"
        assert hits[0]["score"] > 0.0
    finally:
        await db.close()
        d.cleanup()


async def test_query_filters_kind_domain_painday():
    db, d = await _open_db()
    try:
        await db.memory.insert_episodic_memory(
            "recovery", "deploy goal", "deployed the service after a retry",
            domain="code", pain_day_active=True, pain_day_score=0.7,
        )
        await db.memory.insert_episodic_memory(
            "note", "deploy goal note", "a plain note about deploying",
            domain="general", pain_day_active=False,
        )
        only_recovery = await db.memory.query_episodic_memory("deploy", n=10, kind="recovery")
        assert all(h["kind"] == "recovery" for h in only_recovery)
        assert only_recovery
        # domain filter
        only_code = await db.memory.query_episodic_memory("deploy", n=10, domain="code")
        assert all(h["domain"] == "code" for h in only_code)
        # pain-day filter
        flare = await db.memory.query_episodic_memory("deploy", n=10, pain_day=True)
        assert flare and all(h["pain_day_active"] for h in flare)
    finally:
        await db.close()
        d.cleanup()


async def test_touch_bumps_usage():
    db, d = await _open_db()
    try:
        rid = await db.memory.insert_episodic_memory("note", "g", "alpha beta gamma delta")
        await db.memory.touch_episodic_memory(rid)
        await db.memory.touch_episodic_memory(rid)
        async with db._conn.execute(
            "SELECT usage_count, last_recalled_ts FROM episodic_memory WHERE id=?", (rid,)
        ) as cur:
            row = await cur.fetchone()
        assert row[0] == 2
        assert row[1] is not None
    finally:
        await db.close()
        d.cleanup()


async def test_prune_keeps_cap():
    db, d = await _open_db()
    try:
        for i in range(10):
            await db.memory.insert_episodic_memory("note", f"goal {i}", f"summary token{i} shared")
        deleted = await db.runs.prune_episodic_memory(cap=4)
        assert deleted == 6
        async with db._conn.execute("SELECT COUNT(*) FROM episodic_memory") as cur:
            assert (await cur.fetchone())[0] == 4
    finally:
        await db.close()
        d.cleanup()


async def test_memory_manager_recall_and_write():
    db, d = await _open_db()
    try:
        mm = MemoryManager(agent_db=db)  # twin_state=None → pain-day defaults
        rid = await mm.write_memory_note(
            kind="recovery", goal="restart the indexer",
            summary="indexer was wedged; restarting the supervised loop recovered it",
            domain="ops",
        )
        assert rid and rid > 0
        hits = await mm.recall_episodic("indexer wedged restart", n=3)
        assert hits and hits[0]["goal"] == "restart the indexer"
        # recall touched the row (usage_count incremented)
        async with db._conn.execute(
            "SELECT usage_count FROM episodic_memory WHERE id=?", (rid,)
        ) as cur:
            assert (await cur.fetchone())[0] >= 1
    finally:
        await db.close()
        d.cleanup()


async def test_recall_empty_store_is_noop():
    db, d = await _open_db()
    try:
        mm = MemoryManager(agent_db=db)
        assert await mm.recall_episodic("anything at all") == []
    finally:
        await db.close()
        d.cleanup()
