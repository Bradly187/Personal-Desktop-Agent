"""AgentDB.open() must migrate a legacy DB before building migrated-column indexes.

Regression for the startup-blocking bug: AGENT_DB_SCHEMA built
`idx_goalq_sched ON goal_queue(execute_at)` inside executescript(), but on a DB
whose goal_queue predates the execute_at migration column, CREATE TABLE IF NOT
EXISTS is a no-op so the column was absent — the index build raised
`sqlite3.OperationalError: no such column: execute_at` before _migrate() could
add it. The fix defers that index (see _DEFERRED_INDEXES) until after _migrate().
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from storage.db import AgentDB, _AGENT_DB_SCHEMA_VERSION

# goal_queue exactly as it existed before the N+2 proactivity migration columns
# (execute_at / recurrence / source_trigger) and the Sprint-O claim-lease columns.
_LEGACY_GOAL_QUEUE = """
CREATE TABLE goal_queue (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL    NOT NULL,
    goal            TEXT    NOT NULL,
    domain          TEXT    NOT NULL DEFAULT 'plan',
    status          TEXT    NOT NULL DEFAULT 'queued',
    idempotency_key TEXT    UNIQUE,
    attempts        INTEGER NOT NULL DEFAULT 0,
    max_attempts    INTEGER NOT NULL DEFAULT 3,
    last_error      TEXT,
    run_id          INTEGER
);
"""


def _make_legacy_db(path: str) -> None:
    c = sqlite3.connect(path)
    try:
        c.executescript(_LEGACY_GOAL_QUEUE)
        c.execute("PRAGMA user_version = 5")
        c.commit()
    finally:
        c.close()


async def test_open_migrates_legacy_goal_queue_without_crashing(tmp_path):
    p = tmp_path / "legacy_agent.db"
    _make_legacy_db(str(p))

    # Pre-state mirrors the real broken DB: no execute_at, user_version 5.
    c = sqlite3.connect(str(p))
    assert "execute_at" not in [r[1] for r in c.execute("PRAGMA table_info(goal_queue)")]
    assert c.execute("PRAGMA user_version").fetchone()[0] == 5
    c.close()

    db = AgentDB()
    await db.open(p)        # must NOT raise "no such column: execute_at"
    await db.close()

    c = sqlite3.connect(str(p))
    cols = [r[1] for r in c.execute("PRAGMA table_info(goal_queue)")]
    idxs = [r[1] for r in c.execute("PRAGMA index_list(goal_queue)")]
    ver = c.execute("PRAGMA user_version").fetchone()[0]
    c.close()

    # _migrate added every pending column and finalized the version …
    for col in ("execute_at", "recurrence", "source_trigger", "owner_pid", "claimed_at"):
        assert col in cols, f"{col} not migrated in"
    assert ver == _AGENT_DB_SCHEMA_VERSION
    # … and the deferred index built AFTER the column existed.
    assert "idx_goalq_sched" in idxs


async def test_open_fresh_db_builds_deferred_index(tmp_path):
    """A brand-new DB (goal_queue created with execute_at) must also get the index."""
    p = tmp_path / "fresh_agent.db"
    db = AgentDB()
    await db.open(p)
    await db.close()

    c = sqlite3.connect(str(p))
    cols = [r[1] for r in c.execute("PRAGMA table_info(goal_queue)")]
    idxs = [r[1] for r in c.execute("PRAGMA index_list(goal_queue)")]
    c.close()

    assert "execute_at" in cols
    assert "idx_goalq_sched" in idxs
