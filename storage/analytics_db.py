from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

try:
    import duckdb
    _DUCKDB_AVAILABLE = True
except ImportError:
    _DUCKDB_AVAILABLE = False

from storage.schema.analytics import _ANALYTICS_SCHEMA

log = logging.getLogger(__name__)

class AnalyticsDB:
    """Synchronous DuckDB wrapper for benchmark storage and analytical queries.

    Attach agent.db to run complex queries across both stores:
        analytics.attach_agent_db(Path("agent.db"))
        analytics.query("SELECT gate_that_decided, COUNT(*) FROM ops.commands GROUP BY 1")
    """

    def __init__(self) -> None:
        self._conn: Optional["duckdb.DuckDBPyConnection"] = None
        self.available = False

    def open(self, path: Path | str) -> None:
        if not _DUCKDB_AVAILABLE:
            return
        self._conn = duckdb.connect(str(path))
        self._conn.execute(_ANALYTICS_SCHEMA)
        self.available = True
        log.info("AnalyticsDB opened: %s", path)

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None
        self.available = False

    def attach_agent_db(self, agent_db_path: Path | str) -> None:
        """Attach agent.db so you can query ops.commands, ops.inferences, etc."""
        if not self._conn:
            return
        try:
            self._conn.execute(
                f"ATTACH '{agent_db_path}' AS ops (TYPE SQLITE)"
            )
            log.info("AnalyticsDB: attached agent.db as 'ops'")
        except Exception as exc:
            # Already attached or sqlite extension unavailable
            log.debug("AnalyticsDB.attach_agent_db: %s", exc)

    def query(self, sql: str, params: Optional[list] = None):
        """Run an ad-hoc analytical query and return results."""
        if not self._conn:
            return None
        return self._conn.execute(sql, params or [])

    # ---------------------------------------------------------------------- #
    # Benchmark writes
    # ---------------------------------------------------------------------- #

    def insert_benchmark_run(
        self,
        ts: float,
        git_hash: Optional[str] = None,
        mode: str = "standard",
        notes: Optional[str] = None,
    ) -> int:
        if not self._conn:
            return -1
        row = self._conn.execute("SELECT nextval('seq_benchmark_runs')").fetchone()
        assert row is not None
        new_id = int(row[0])
        self._conn.execute(
            "INSERT INTO benchmark_runs (id, ts, git_hash, mode, notes) VALUES (?,?,?,?,?)",
            [new_id, ts, git_hash, mode, notes],
        )
        return new_id

    def insert_benchmark_result(
        self,
        run_id: int,
        model: str,
        accuracy_pct: Optional[float],
        correct: Optional[int],
        total: Optional[int],
        p50_ms: Optional[float],
        p95_ms: Optional[float],
        vram_before_gb: Optional[float],
        vram_after_gb: Optional[float],
        vram_delta_gb: Optional[float],
        error: Optional[str] = None,
    ) -> int:
        if not self._conn:
            return -1
        row = self._conn.execute("SELECT nextval('seq_benchmark_results')").fetchone()
        assert row is not None
        new_id = int(row[0])
        self._conn.execute(
            """INSERT INTO benchmark_results
               (id, run_id, model, accuracy_pct, correct, total,
                p50_ms, p95_ms, vram_before_gb, vram_after_gb, vram_delta_gb, error)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                new_id, run_id, model, accuracy_pct, correct, total,
                p50_ms, p95_ms, vram_before_gb, vram_after_gb, vram_delta_gb, error,
            ],
        )
        return new_id

    def insert_benchmark_prompt(
        self,
        result_id: int,
        prompt: str,
        expected: str,
        got: Optional[str],
        correct: bool,
        p50_ms: Optional[float],
        p95_ms: Optional[float],
    ) -> None:
        if not self._conn:
            return
        row = self._conn.execute("SELECT nextval('seq_benchmark_prompts')").fetchone()
        assert row is not None
        new_id = int(row[0])
        self._conn.execute(
            """INSERT INTO benchmark_prompts
               (id, result_id, prompt, expected, got, correct, p50_ms, p95_ms)
               VALUES (?,?,?,?,?,?,?,?)""",
            [new_id, result_id, prompt, expected, got, correct, p50_ms, p95_ms],
        )
