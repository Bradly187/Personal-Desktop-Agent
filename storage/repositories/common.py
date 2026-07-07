import asyncio
import logging
import os

log = logging.getLogger(__name__)

_GOAL_LEASE_TTL_S: float = 1800.0  # 30 minutes


class PruneRetryMixin:
    """Shared pruning DELETE with lock retry, for repos holding ``self._conn``.

    ``PRAGMA busy_timeout`` only auto-retries ``SQLITE_BUSY``. A
    ``"database table is locked"`` (``SQLITE_LOCKED``) — observed at startup
    when a just-killed previous process still holds the WAL lock, or when a
    checkpoint collides with a concurrent reader — is NOT retried by SQLite
    itself, so the prune silently skipped and the table grew unbounded. Back
    off and retry here. Stays non-fatal: returns 0 if every attempt loses the
    race.
    """
    import typing
    _conn: typing.Any

    async def _prune_with_retry(
        self,
        sql: str,
        params: tuple,
        *,
        label: str,
        checkpoint: bool = False,
        attempts: int = 4,
    ) -> int:
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

def _pid_alive(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        import psutil
        return bool(psutil.pid_exists(pid))
    except ImportError:
        pass
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    except Exception:
        return True
    return True
