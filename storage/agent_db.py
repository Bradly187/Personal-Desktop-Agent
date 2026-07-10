from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Optional

try:
    import aiosqlite
    _AIOSQLITE_AVAILABLE = True
except ImportError:
    _AIOSQLITE_AVAILABLE = False

from storage.repositories.commands_repo import CommandsRepo
from storage.repositories.events_repo import EventsRepo
from storage.repositories.gestures_repo import GesturesRepo
from storage.repositories.goals_repo import GoalsRepo
from storage.repositories.graph_repo import GraphRepo
from storage.repositories.inferences_repo import InferencesRepo
from storage.repositories.logs_repo import LogsRepo
from storage.repositories.memory_repo import MemoryRepo
from storage.repositories.misc_repo import MiscRepo
from storage.repositories.profile_repo import ProfileRepo
from storage.repositories.routing_repo import RoutingRepo
from storage.repositories.runs_repo import RunsRepo
from storage.repositories.sagas_repo import SagasRepo
from storage.repositories.sessions_repo import SessionsRepo
from storage.repositories.skills_repo import SkillsRepo
from storage.repositories.telemetry_repo import TelemetryRepo
from storage.repositories.voice_repo import VoiceRepo
from storage.repositories.workflows_repo import WorkflowsRepo

from storage.schema.agent import AGENT_DB_SCHEMA, _AGENT_DB_SCHEMA_VERSION, _AGENT_DB_MIGRATIONS, _DEFERRED_INDEXES

log = logging.getLogger(__name__)

class AgentDB:
    """Async SQLite wrapper for all operational pipeline writes.

    Always check `available` before calling methods — they are no-ops when
    aiosqlite is absent, returning safe default values (None / [] / 0.6).
    """


    # ---------------------------------------------------------------------- #
    # Lifecycle
    # ---------------------------------------------------------------------- #

    def __init__(self):
        self._conn = None
        self.available = False
        self.commands = CommandsRepo(None)
        self.events = EventsRepo(None)
        self.gestures = GesturesRepo(None)
        self.goals = GoalsRepo(None)
        self.graph = GraphRepo(None)
        self.inferences = InferencesRepo(None)
        self.logs = LogsRepo(None)
        self.memory = MemoryRepo(None)
        self.misc = MiscRepo(None)
        self.profile = ProfileRepo(None)
        self.routing = RoutingRepo(None)
        self.runs = RunsRepo(None)
        self.sagas = SagasRepo(None)
        self.sessions = SessionsRepo(None)
        self.skills = SkillsRepo(None)
        self.telemetry = TelemetryRepo(None)
        self.voice = VoiceRepo(None)
        self.workflows = WorkflowsRepo(None)

    @property
    def path(self) -> Optional[str]:
        """Filesystem path of the open agent.db (None until open()). Lets read-only
        consumers — e.g. the dashboard's replay/trends/cost endpoints — point the
        stdlib-sqlite reader at the same file."""
        return getattr(self, "_path", None)

    async def open(self, path: Path | str) -> None:
        if not _AIOSQLITE_AVAILABLE:
            return
        self._path = str(Path(path))
        self._conn = await aiosqlite.connect(Path(path))
        self.commands = CommandsRepo(self._conn)
        self.events = EventsRepo(self._conn)
        self.gestures = GesturesRepo(self._conn)
        self.goals = GoalsRepo(self._conn)
        self.graph = GraphRepo(self._conn)
        self.inferences = InferencesRepo(self._conn)
        self.logs = LogsRepo(self._conn)
        self.memory = MemoryRepo(self._conn)
        self.misc = MiscRepo(self._conn)
        self.profile = ProfileRepo(self._conn)
        self.routing = RoutingRepo(self._conn)
        self.runs = RunsRepo(self._conn)
        self.sagas = SagasRepo(self._conn)
        self.sessions = SessionsRepo(self._conn)
        self.skills = SkillsRepo(self._conn)
        self.telemetry = TelemetryRepo(self._conn)
        self.voice = VoiceRepo(self._conn)
        self.workflows = WorkflowsRepo(self._conn)
        self._conn.row_factory = aiosqlite.Row
        # WAL mode: concurrent readers don't block writers; no "database is locked" under load.
        # busy_timeout: wait up to 5 s before raising an error (handles burst contention).
        # synchronous=NORMAL: safe with WAL; skips fsync on every write for ~3× throughput.
        await self._conn.executescript(
            "PRAGMA journal_mode=WAL;"
            "PRAGMA busy_timeout=5000;"
            "PRAGMA synchronous=NORMAL;"
        )
        await self._conn.executescript(AGENT_DB_SCHEMA)
        # Versioned, additive column migrations (degrade-gracefully — a failure
        # here logs and continues as long as the core schema applied).
        try:
            await self._migrate()
        except Exception as exc:
            log.warning("AgentDB migration error (continuing): %s", exc)
        # Indexes on migrated columns must be built only after _migrate() has
        # added those columns (see _DEFERRED_INDEXES) — otherwise a pre-migration
        # DB fails the index build during executescript above.
        for _idx_ddl in _DEFERRED_INDEXES:
            try:
                await self._conn.execute(_idx_ddl)
            except Exception as exc:
                log.warning("AgentDB deferred index error (continuing): %s", exc)
        try:
            await self._seed_config_tables()
        except Exception as exc:
            log.warning("AgentDB config seed error (continuing): %s", exc)
        await self._conn.commit()
        self.available = True
        log.info("AgentDB opened: %s", path)

    async def _migrate(self) -> None:
        """Apply additive column migrations, gated by PRAGMA user_version so the
        batch runs at most once per DB.

        CREATE TABLE IF NOT EXISTS cannot add columns to a pre-existing table, so
        each (table, column, ddl) is ALTERed in. Unlike the previous
        ``except Exception: pass``, the except is narrowed to the already-exists
        case — a genuine DDL error is logged instead of being silently swallowed.
        """
        cur = await self._conn.execute("PRAGMA user_version")
        row = await cur.fetchone()
        version = row[0] if row else 0
        if version >= _AGENT_DB_SCHEMA_VERSION:
            return  # already migrated — skip the ALTER probing entirely
        all_ok = True
        for table, column, ddl in _AGENT_DB_MIGRATIONS:
            try:
                await self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"
                )
            except Exception as exc:
                if "duplicate column name" not in str(exc).lower():
                    all_ok = False     # a genuine DDL failure — do NOT finalize
                    log.warning(
                        "AgentDB migration ALTER %s.%s failed: %s", table, column, exc
                    )
                # else: column already present (fresh/already-migrated DB) — fine
        # Only advance user_version when the whole batch applied (#8). Bumping it
        # after a genuine failure would mark the schema "migrated" and the broken
        # column would never retry; leaving it unbumped retries next boot.
        if all_ok:
            # PRAGMA user_version does not accept a bound parameter
            await self._conn.execute(
                f"PRAGMA user_version = {_AGENT_DB_SCHEMA_VERSION}")
            log.info("AgentDB schema migrated to version %d", _AGENT_DB_SCHEMA_VERSION)
        else:
            log.warning(
                "AgentDB migration incomplete — user_version left at %d, retry next boot",
                version)

    async def _seed_config_tables(self) -> None:
        """INSERT OR IGNORE default rows into the three config tables.

        Using the current wall-clock time as updated_at; callers may override
        individual rows via direct UPDATE without re-running this method.
        """
        now = time.time()
        await self._conn.executemany(
            "INSERT OR IGNORE INTO tool_timeout_config (tool_name, timeout_ms, max_retries, updated_at)"
            " VALUES (?, ?, ?, ?)",
            [
                ("mouse_click",     5_000,  1, now),
                ("keyboard_type",  10_000,  0, now),
                ("run_terminal",   30_000,  0, now),
                ("write_file",     15_000,  1, now),
                ("vision_grounder", 8_000,  1, now),
                ("screenshot",      5_000,  1, now),
                ("ui_automation",   3_000,  1, now),
            ],
        )
        await self._conn.executemany(
            "INSERT OR IGNORE INTO tool_cache_config (tool_name, ttl_s, max_entries, updated_at)"
            " VALUES (?, ?, ?, ?)",
            [
                ("vision_grounder", 2.0, 200, now),
                ("ui_automation",   1.0, 200, now),
                ("target_cache",    1.5, 500, now),
            ],
        )
        await self._conn.executemany(
            "INSERT OR IGNORE INTO rate_limit_config (resource, max_rps, burst_capacity, updated_at)"
            " VALUES (?, ?, ?, ?)",
            [
                ("cloud_api",        2.0, 5, now),
                ("ollama",           4.0, 8, now),
                ("vision_grounder",  1.0, 3, now),
            ],
        )

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None
        self.available = False

    async def _prune_with_retry(
        self,
        sql: str,
        params: tuple,
        *,
        label: str,
        checkpoint: bool = False,
        attempts: int = 4,
    ) -> int:
        """Run a pruning DELETE, retrying briefly on a locked database.

        ``PRAGMA busy_timeout`` only auto-retries ``SQLITE_BUSY``. A
        ``"database table is locked"`` (``SQLITE_LOCKED``) — observed at startup
        when a just-killed previous process still holds the WAL lock, or when a
        checkpoint collides with a concurrent reader — is NOT retried by SQLite
        itself, so the prune silently skipped and ``sensor_telemetry`` grew
        unbounded. Back off and retry here. Stays non-fatal: returns 0 if every
        attempt loses the race.
        """
        if not self._conn:
            return 0
        delay = 0.2
        for attempt in range(1, attempts + 1):
            try:
                async with self._conn.execute(sql, params) as cur:
                    deleted = cur.rowcount or 0
                if checkpoint:
                    await self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                await self._conn.commit()
                if deleted:
                    log.info("AgentDB: pruned %d %s", deleted, label)
                return deleted
            except Exception as exc:
                if "lock" in str(exc).lower() and attempt < attempts:
                    log.debug(
                        "AgentDB.prune %s locked (attempt %d/%d) — retrying in %.1fs",
                        label, attempt, attempts, delay,
                    )
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue
                log.warning("AgentDB.prune %s failed: %s", label, exc)
                return 0
        return 0

# ---------------------------------------------------------------------------
# AnalyticsDB — DuckDB analytical store
# ---------------------------------------------------------------------------
