"""PR 0 (R-3 foundation) — self_evolution_candidates staging table.

Covers staged insert (idempotent on UNIQUE(kind,text,action_or_wrong)), status
filtering, and status transitions with eval_delta.
"""
import os
import tempfile

from storage.db import AgentDB


async def _open_db():
    d = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    db = AgentDB()
    await db.open(os.path.join(d.name, "agent.db"))
    return db, d


async def test_insert_is_idempotent():
    db, d = await _open_db()
    try:
        id1 = await db.insert_evolution_candidate(
            "counterexample", "open the parser", "OPEN parser.py",
            domain="command", reason="pipeline_failure",
            source_refs='{"escalation_ids":[1]}',
        )
        id2 = await db.insert_evolution_candidate(
            "counterexample", "open the parser", "OPEN parser.py", domain="command",
        )
        assert id1 and id1 == id2, "duplicate stage must return the same row id"
        async with db._conn.execute(
            "SELECT COUNT(*) FROM self_evolution_candidates"
        ) as cur:
            assert (await cur.fetchone())[0] == 1
    finally:
        await db.close()
        d.cleanup()


async def test_get_by_status_and_transition():
    db, d = await _open_db()
    try:
        cid = await db.insert_evolution_candidate(
            "example", "scroll down", "SCROLL down", domain="command",
        )
        proposed = await db.get_evolution_candidates(status="proposed")
        assert any(c["id"] == cid for c in proposed)
        assert proposed[0]["status"] == "proposed"

        await db.set_evolution_candidate_status(cid, "promoted", eval_delta=0.012)

        # no longer proposed
        assert all(c["id"] != cid for c in await db.get_evolution_candidates("proposed"))
        promoted = await db.get_evolution_candidates("promoted")
        row = next(c for c in promoted if c["id"] == cid)
        assert row["status"] == "promoted"
        assert abs(row["eval_delta"] - 0.012) < 1e-9
        assert row["decided_ts"] is not None
    finally:
        await db.close()
        d.cleanup()


async def test_reject_status():
    db, d = await _open_db()
    try:
        cid = await db.insert_evolution_candidate(
            "counterexample", "delete everything", "RUN_TERMINAL rm -rf",
            domain="command", reason="pipeline_failure",
        )
        await db.set_evolution_candidate_status(cid, "rejected")
        rejected = await db.get_evolution_candidates("rejected")
        assert any(c["id"] == cid for c in rejected)
    finally:
        await db.close()
        d.cleanup()
