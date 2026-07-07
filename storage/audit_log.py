"""audit_log — append-only security audit trail for the Personal Desktop Agent.

Records all significant agent actions: shell executions, API calls, file access,
MCP tool invocations, security events, approval decisions, and session lifecycle.

Design:
  - Append-only: no UPDATE or DELETE on audit_events (enforced by triggers).
  - WAL mode for concurrent reads during writes.
  - Separate from agent.db to isolate security-critical data.
  - Async interface consistent with AgentDB pattern.

Event types:
  shell_exec       — command executed via subprocess/pyautogui
  api_call         — outbound API request (Bedrock, Anthropic, etc.)
  file_access      — file read/write by the agent
  mcp_call         — MCP tool invocation (mouse, keyboard, screen, etc.)
  security_event   — content filter trigger, trust classifier flag, auth failure
  approval         — user approval/denial of a flagged action
  session_lifecycle — session start/stop/crash

Usage:
    audit = AuditLog()
    await audit.open(Path("audit.db"))
    await audit.log("mcp_call", tool="mouse_click", params={"x": 100, "y": 200})
    await audit.log("security_event", detail="PII detected in prompt", severity="warning")
    await audit.close()
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

try:
    import aiosqlite
    _AVAILABLE = True
except ImportError:
    _AVAILABLE = False
    log.warning("aiosqlite not installed — AuditLog disabled")


AUDIT_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS audit_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,
    event_type  TEXT    NOT NULL,
    severity    TEXT    NOT NULL DEFAULT 'info',
    actor       TEXT    NOT NULL DEFAULT 'agent',
    tool        TEXT,
    detail      TEXT,
    params      TEXT,
    outcome     TEXT,
    session_id  INTEGER,
    command_id  INTEGER,
    source_ip   TEXT,
    redacted    INTEGER NOT NULL DEFAULT 0,
    -- Tamper-evidence chain (M1): each row's row_hash = SHA-256(prev_hash + its
    -- own fields), and prev_hash = the previous row's row_hash. The triggers
    -- block UPDATE/DELETE, but a writer that drops the triggers could still
    -- delete/modify rows; the chain makes any such interior tampering DETECTABLE
    -- via verify_chain() (which triggers alone cannot provide).
    prev_hash   TEXT,
    row_hash    TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_events(ts);
CREATE INDEX IF NOT EXISTS idx_audit_type ON audit_events(event_type);
CREATE INDEX IF NOT EXISTS idx_audit_severity ON audit_events(severity);
CREATE INDEX IF NOT EXISTS idx_audit_session ON audit_events(session_id);

-- Append-only enforcement: block UPDATE and DELETE
CREATE TRIGGER IF NOT EXISTS audit_no_update
    BEFORE UPDATE ON audit_events
    BEGIN
        SELECT RAISE(ABORT, 'audit_events is append-only: UPDATE not allowed');
    END;

CREATE TRIGGER IF NOT EXISTS audit_no_delete
    BEFORE DELETE ON audit_events
    BEGIN
        SELECT RAISE(ABORT, 'audit_events is append-only: DELETE not allowed');
    END;
"""


# Hash-chain genesis (the prev_hash of the very first chained row).
_CHAIN_GENESIS = "0" * 64


def _compute_row_hash(
    prev_hash: str, *, ts, event_type, severity, actor, tool, detail,
    params_json, outcome, session_id, command_id, source_ip, redacted,
) -> str:
    """SHA-256 over prev_hash + this row's stored field values. The exact same
    values are used at insert time and recomputed by verify_chain(), so any
    change to a persisted field (or to prev_hash) breaks the recomputation."""
    canonical = json.dumps(
        [prev_hash, ts, event_type, severity, actor, tool, detail, params_json,
         outcome, session_id, command_id, source_ip, int(redacted)],
        ensure_ascii=False, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class AuditLog:
    """Append-only async audit log backed by SQLite WAL."""

    def __init__(self) -> None:
        self._conn: Optional["aiosqlite.Connection"] = None
        self.available = False
        self._session_id: Optional[int] = None
        # Running chain head (row_hash of the last appended row), loaded from the
        # DB on open() so the chain survives a restart. Serialise appends so the
        # read-modify-write of _last_hash across the insert await can't fork the
        # chain under concurrent log() callers.
        self._last_hash: str = _CHAIN_GENESIS
        self._write_lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def open(self, path: Path | str = "audit.db") -> None:
        if not _AVAILABLE:
            return
        self._conn = await aiosqlite.connect(Path(path))
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(AUDIT_SCHEMA)
        # Additive migration for a pre-chain audit.db (CREATE TABLE IF NOT EXISTS
        # can't add columns to an existing table). Legacy rows keep NULL hashes;
        # the chain begins at the first row appended after this.
        for col in ("prev_hash", "row_hash"):
            try:
                await self._conn.execute(
                    f"ALTER TABLE audit_events ADD COLUMN {col} TEXT")
            except Exception as exc:
                if "duplicate column name" not in str(exc).lower():
                    log.debug("AuditLog migration %s: %s", col, exc)
        await self._conn.commit()
        # Resume the chain from the last hashed row so a restart continues it.
        try:
            async with self._conn.execute(
                "SELECT row_hash FROM audit_events WHERE row_hash IS NOT NULL "
                "ORDER BY id DESC LIMIT 1"
            ) as cur:
                row = await cur.fetchone()
                if row and row[0]:
                    self._last_hash = row[0]
        except Exception as exc:
            log.debug("AuditLog: chain head load skipped: %s", exc)
        self.available = True
        log.info("AuditLog opened: %s (WAL mode, append-only, hash-chained)", path)

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None
        self.available = False

    def set_session_id(self, session_id: int) -> None:
        """Associate subsequent log entries with a session."""
        self._session_id = session_id

    # ------------------------------------------------------------------ #
    # Core logging
    # ------------------------------------------------------------------ #

    async def log(
        self,
        event_type: str,
        *,
        severity: str = "info",
        actor: str = "agent",
        tool: Optional[str] = None,
        detail: Optional[str] = None,
        params: Optional[dict] = None,
        outcome: Optional[str] = None,
        command_id: Optional[int] = None,
        source_ip: Optional[str] = None,
        redacted: bool = False,
    ) -> Optional[int]:
        """Append an audit event. Returns the row id or None if unavailable."""
        if not self._conn:
            return None
        params_json = json.dumps(params) if params else None
        cid = command_id if (command_id and command_id > 0) else None
        # Serialise the hash-chain read-modify-write: prev_hash must be the
        # immediately-preceding row's row_hash, so two concurrent appends can't
        # both chain off the same head and fork the chain.
        async with self._write_lock:
            ts = time.time()
            prev = self._last_hash
            row_hash = _compute_row_hash(
                prev, ts=ts, event_type=event_type, severity=severity, actor=actor,
                tool=tool, detail=detail, params_json=params_json, outcome=outcome,
                session_id=self._session_id, command_id=cid, source_ip=source_ip,
                redacted=redacted,
            )
            try:
                cur = await self._conn.execute(
                    """INSERT INTO audit_events
                       (ts, event_type, severity, actor, tool, detail, params,
                        outcome, session_id, command_id, source_ip, redacted,
                        prev_hash, row_hash)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        ts, event_type, severity, actor, tool, detail, params_json,
                        outcome, self._session_id, cid, source_ip, int(redacted),
                        prev, row_hash,
                    ),
                )
                await self._conn.commit()
                self._last_hash = row_hash
                return cur.lastrowid
            except Exception as exc:
                log.warning("AuditLog.log failed: %s", exc)
                return None

    # ------------------------------------------------------------------ #
    # Convenience methods
    # ------------------------------------------------------------------ #

    async def log_mcp_call(
        self, tool: str, params: Optional[dict] = None, outcome: Optional[str] = None
    ) -> Optional[int]:
        return await self.log(
            "mcp_call", tool=tool, params=params, outcome=outcome
        )

    async def log_api_call(
        self, service: str, detail: Optional[str] = None, redacted: bool = False
    ) -> Optional[int]:
        return await self.log(
            "api_call", tool=service, detail=detail, redacted=redacted
        )

    async def log_shell_exec(
        self, command: str, outcome: Optional[str] = None
    ) -> Optional[int]:
        return await self.log(
            "shell_exec", detail=command, outcome=outcome
        )

    async def log_file_access(
        self, path: str, mode: str = "read"
    ) -> Optional[int]:
        return await self.log(
            "file_access", detail=path, params={"mode": mode}
        )

    async def log_security_event(
        self,
        detail: str,
        severity: str = "warning",
        params: Optional[dict] = None,
    ) -> Optional[int]:
        return await self.log(
            "security_event", severity=severity, detail=detail, params=params
        )

    async def log_session_start(self, session_id: int) -> Optional[int]:
        self._session_id = session_id
        return await self.log(
            "session_lifecycle", detail="session_start",
            params={"session_id": session_id}
        )

    async def log_session_stop(self, reason: str = "normal") -> Optional[int]:
        return await self.log(
            "session_lifecycle", detail="session_stop",
            params={"reason": reason}
        )

    # ------------------------------------------------------------------ #
    # Query interface (read-only, for dashboards/debugging)
    # ------------------------------------------------------------------ #

    async def get_recent(self, limit: int = 100) -> list[dict]:
        """Return the most recent audit events."""
        if not self._conn:
            return []
        try:
            async with self._conn.execute(
                "SELECT * FROM audit_events ORDER BY ts DESC LIMIT ?",
                (limit,),
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]
        except Exception as exc:
            log.warning("AuditLog.get_recent failed: %s", exc)
            return []

    async def get_by_type(
        self, event_type: str, limit: int = 100
    ) -> list[dict]:
        """Return recent events of a specific type."""
        if not self._conn:
            return []
        try:
            async with self._conn.execute(
                "SELECT * FROM audit_events WHERE event_type = ? ORDER BY ts DESC LIMIT ?",
                (event_type, limit),
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]
        except Exception as exc:
            log.warning("AuditLog.get_by_type failed: %s", exc)
            return []

    async def get_recent_mcp_calls(self, n: int = 10) -> list[dict]:
        """Return the most recent `n` mcp_call events for history queries."""
        return await self.get_by_type("mcp_call", limit=n)

    async def get_security_events(
        self, severity: Optional[str] = None, limit: int = 50
    ) -> list[dict]:
        """Return security events, optionally filtered by severity."""
        if not self._conn:
            return []
        try:
            if severity:
                query = "SELECT * FROM audit_events WHERE event_type = 'security_event' AND severity = ? ORDER BY ts DESC LIMIT ?"
                params: typing.Any = (severity, limit)
            else:
                query = "SELECT * FROM audit_events WHERE event_type = 'security_event' ORDER BY ts DESC LIMIT ?"
                params: typing.Any = (limit,)
            async with self._conn.execute(query, params) as cur:
                return [dict(r) for r in await cur.fetchall()]
        except Exception as exc:
            log.warning("AuditLog.get_security_events failed: %s", exc)
            return []

    # ------------------------------------------------------------------ #
    # Tamper-evidence (M1)
    # ------------------------------------------------------------------ #

    def chain_head(self) -> str:
        """The current chain head (row_hash of the last appended row). An external
        monitor can checkpoint this to also detect tail truncation."""
        return self._last_hash

    async def verify_chain(self) -> dict:
        """Walk the hash chain in id order and report tamper evidence.

        Returns {ok, rows_checked, unchained, break_at}. Detects any
        modification, interior deletion, or reordering of CHAINED rows: a
        modified field fails the row_hash recomputation, and a deleted interior
        row breaks the prev_hash linkage. Legacy rows written before chaining was
        added (NULL row_hash) are counted as `unchained` and skipped.

        Limitation: tail truncation (deleting the most-recent rows) is only
        detectable against an external checkpoint of chain_head() — a self-
        contained walk can't know rows are missing from the end.
        """
        import typing
        result: dict[str, typing.Any] = {"ok": True, "rows_checked": 0, "unchained": 0, "break_at": None}
        if not self._conn:
            result["ok"] = False
            return result
        prev = _CHAIN_GENESIS
        started = False
        try:
            async with self._conn.execute(
                "SELECT id, ts, event_type, severity, actor, tool, detail, params, "
                "outcome, session_id, command_id, source_ip, redacted, "
                "prev_hash, row_hash FROM audit_events ORDER BY id"
            ) as cur:
                async for r in cur:
                    if r["row_hash"] is None:
                        result["unchained"] += 1
                        continue
                    expected = _compute_row_hash(
                        r["prev_hash"], ts=r["ts"], event_type=r["event_type"],
                        severity=r["severity"], actor=r["actor"], tool=r["tool"],
                        detail=r["detail"], params_json=r["params"],
                        outcome=r["outcome"], session_id=r["session_id"],
                        command_id=r["command_id"], source_ip=r["source_ip"],
                        redacted=r["redacted"],
                    )
                    # Field/prev_hash tampering breaks the recomputation; a
                    # deleted interior row breaks the prev→row_hash linkage.
                    if expected != r["row_hash"] or (started and r["prev_hash"] != prev):
                        result["ok"] = False
                        result["break_at"] = r["id"]
                        break
                    prev = r["row_hash"]
                    started = True
                    result["rows_checked"] += 1
        except Exception as exc:
            log.warning("AuditLog.verify_chain failed: %s", exc)
            result["ok"] = False
        return result

    async def count_by_type(self) -> dict[str, int]:
        """Return event counts grouped by type."""
        if not self._conn:
            return {}
        try:
            async with self._conn.execute(
                "SELECT event_type, COUNT(*) as cnt FROM audit_events GROUP BY event_type"
            ) as cur:
                return {r["event_type"]: r["cnt"] for r in await cur.fetchall()}
        except Exception as exc:
            log.warning("AuditLog.count_by_type failed: %s", exc)
            return {}
