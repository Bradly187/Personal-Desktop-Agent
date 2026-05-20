"""db — central database layer for Personal Desktop Agent.

Two stores:
  AgentDB      — SQLite via aiosqlite; all operational pipeline writes.
                 Replaces trainer.db + routing_log.jsonl + gesture_calibration.json.
  AnalyticsDB  — DuckDB; benchmark storage + analytical queries over agent.db.
                 Replaces benchmark_results.json.
                 DuckDB can attach agent.db directly:
                   ATTACH 'agent.db' AS ops (TYPE SQLITE)

Usage:
    # Startup (main.py):
    agent_db = AgentDB()
    await agent_db.open(Path("agent.db"))
    session_id = await agent_db.insert_session(mode="normal", git_hash="abc123")

    # Shutdown:
    await agent_db.close_session(session_id)
    await agent_db.close()

    # Benchmark (benchmark_models.py, synchronous):
    analytics = AnalyticsDB()
    analytics.open(Path("analytics.duckdb"))
    analytics.attach_agent_db(Path("agent.db"))
    run_id = analytics.insert_benchmark_run(ts=time.time(), git_hash="abc123")
    analytics.close()
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import time
from pathlib import Path
from typing import TYPE_CHECKING, Optional

log = logging.getLogger(__name__)

try:
    import aiosqlite
    _AIOSQLITE_AVAILABLE = True
except ImportError:
    _AIOSQLITE_AVAILABLE = False
    log.warning("aiosqlite not installed — AgentDB disabled")

try:
    import duckdb
    _DUCKDB_AVAILABLE = True
except ImportError:
    _DUCKDB_AVAILABLE = False
    log.warning("duckdb not installed — AnalyticsDB disabled")

# ---------------------------------------------------------------------------
# MiniLM semantic encoder — lazy-loaded, optional
# ---------------------------------------------------------------------------

_ENCODER = None   # SentenceTransformer instance; None until first use
_ENCODER_FAILED = False  # set True if import/load fails so we don't retry


def _load_encoder_sync():
    """Load all-MiniLM-L6-v2 synchronously (called via asyncio.to_thread)."""
    from sentence_transformers import SentenceTransformer
    enc = SentenceTransformer("all-MiniLM-L6-v2")
    log.info("MiniLM loaded — semantic few-shot retrieval enabled (384-dim cosine)")
    return enc


async def _get_encoder() -> Optional[object]:
    """Return the cached encoder, loading it on first call (non-blocking)."""
    global _ENCODER, _ENCODER_FAILED
    if _ENCODER is not None:
        return _ENCODER
    if _ENCODER_FAILED:
        return None
    try:
        _ENCODER = await asyncio.to_thread(_load_encoder_sync)
        return _ENCODER
    except Exception as exc:
        _ENCODER_FAILED = True
        log.debug("MiniLM unavailable — falling back to Jaccard scoring: %s", exc)
        return None


def _encode_sync(text: str, encoder) -> bytes:
    """Encode text to normalised float32 bytes (384-dim)."""
    import numpy as np
    vec = encoder.encode(text, normalize_embeddings=True)
    return vec.astype(np.float32).tobytes()


def _cosine(a: bytes, b: bytes) -> float:
    """Cosine similarity between two normalised float32 BLOBs. Already unit-length → dot product."""
    import numpy as np
    va = np.frombuffer(a, dtype=np.float32)
    vb = np.frombuffer(b, dtype=np.float32)
    return float(np.dot(va, vb))

if TYPE_CHECKING:
    from command_executor import Command


# ---------------------------------------------------------------------------
# agent.db schema — all 12 tables
# ---------------------------------------------------------------------------

AGENT_DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    REAL    NOT NULL,
    ended_at      REAL,
    mode          TEXT    NOT NULL DEFAULT 'normal',
    git_hash      TEXT,
    agent_version TEXT,
    notes         TEXT
);

CREATE TABLE IF NOT EXISTS commands (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id         INTEGER NOT NULL REFERENCES sessions(id),
    ts                 REAL    NOT NULL,
    source             TEXT    NOT NULL,
    text               TEXT    NOT NULL,
    action             TEXT,
    params             TEXT,
    route              TEXT,
    gate_that_decided  TEXT,
    latency_ms         REAL,
    whisper_logprob    REAL,
    gesture_confidence REAL,
    gaze_x             REAL,
    gaze_y             REAL,
    success            INTEGER,
    error_msg          TEXT,
    corrected_to       TEXT
);
CREATE INDEX IF NOT EXISTS idx_commands_session ON commands(session_id);
CREATE INDEX IF NOT EXISTS idx_commands_ts      ON commands(ts);
CREATE INDEX IF NOT EXISTS idx_commands_source  ON commands(source);
CREATE INDEX IF NOT EXISTS idx_commands_action  ON commands(action);

CREATE TABLE IF NOT EXISTS inferences (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    command_id  INTEGER REFERENCES commands(id),
    ts          REAL    NOT NULL,
    model       TEXT    NOT NULL,
    domain      TEXT    NOT NULL,
    prompt_hash TEXT,
    response    TEXT,
    tokens_in   INTEGER,
    tokens_out  INTEGER,
    latency_ms  REAL    NOT NULL,
    backend     TEXT,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_inferences_command ON inferences(command_id);
CREATE INDEX IF NOT EXISTS idx_inferences_model   ON inferences(model);
CREATE INDEX IF NOT EXISTS idx_inferences_ts      ON inferences(ts);

CREATE TABLE IF NOT EXISTS agent_runs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    command_id       INTEGER REFERENCES commands(id),
    ts               REAL    NOT NULL,
    goal             TEXT    NOT NULL,
    domain           TEXT    NOT NULL,
    model_used       TEXT,
    step_count       INTEGER,
    success          INTEGER,
    total_latency_ms REAL,
    error            TEXT
);

CREATE TABLE IF NOT EXISTS agent_steps (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     INTEGER NOT NULL REFERENCES agent_runs(id),
    step_num   INTEGER NOT NULL,
    action     TEXT    NOT NULL,
    args       TEXT,
    body       TEXT,
    result     TEXT,
    success    INTEGER,
    latency_ms REAL
);
CREATE INDEX IF NOT EXISTS idx_steps_run ON agent_steps(run_id);

CREATE TABLE IF NOT EXISTS few_shot_examples (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    command_id  INTEGER REFERENCES commands(id),
    text        TEXT    NOT NULL,
    action      TEXT    NOT NULL,
    source      TEXT    NOT NULL,
    domain      TEXT    NOT NULL DEFAULT 'command',
    ts          REAL    NOT NULL,
    usage_count INTEGER NOT NULL DEFAULT 1,
    embedding   BLOB,
    UNIQUE(text, action)
);
CREATE INDEX IF NOT EXISTS idx_fse_ts     ON few_shot_examples(ts);
CREATE INDEX IF NOT EXISTS idx_fse_domain ON few_shot_examples(domain);

CREATE TABLE IF NOT EXISTS word_counts (
    word  TEXT    PRIMARY KEY,
    count INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS hotwords (
    word  TEXT PRIMARY KEY,
    added REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS gesture_samples (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    command_id    INTEGER REFERENCES commands(id),
    ts            REAL    NOT NULL,
    gesture       TEXT    NOT NULL,
    confidence    REAL    NOT NULL,
    lidar_depth_m REAL
);
CREATE INDEX IF NOT EXISTS idx_gs_gesture_ts ON gesture_samples(gesture, ts);

CREATE TABLE IF NOT EXISTS gesture_calibration (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ts               REAL    NOT NULL,
    gesture          TEXT    NOT NULL,
    confidence_floor REAL    NOT NULL,
    sample_count     INTEGER NOT NULL,
    p10              REAL
);
CREATE INDEX IF NOT EXISTS idx_gcon_gesture ON gesture_calibration(gesture, ts);

-- Velocity samples for motion-gesture calibration (Minority Report gestures).
-- velocity: normalised-coord/sec for swipes; m/s for push/pull.
-- pain_day: 1 if BehavioralTwinState.pain_day_active at time of capture.
CREATE TABLE IF NOT EXISTS gesture_velocity_samples (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL    NOT NULL,
    gesture    TEXT    NOT NULL,
    velocity   REAL    NOT NULL,
    pain_day   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_gvs_gesture_ts ON gesture_velocity_samples(gesture, ts);

-- Adapted velocity floors (minimum velocity to classify as intentional).
CREATE TABLE IF NOT EXISTS gesture_velocity_calibration (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ts               REAL    NOT NULL,
    gesture          TEXT    NOT NULL,
    velocity_floor   REAL    NOT NULL,
    sample_count     INTEGER NOT NULL,
    p10              REAL
);
CREATE INDEX IF NOT EXISTS idx_gvc_gesture ON gesture_velocity_calibration(gesture, ts);

CREATE TABLE IF NOT EXISTS sensor_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    command_id  INTEGER REFERENCES commands(id),
    ts          REAL    NOT NULL,
    event_type  TEXT    NOT NULL,
    x           REAL,
    y           REAL,
    confidence  REAL,
    value       TEXT,
    params      TEXT
);
CREATE INDEX IF NOT EXISTS idx_se_ts   ON sensor_events(ts);
CREATE INDEX IF NOT EXISTS idx_se_type ON sensor_events(event_type);

CREATE TABLE IF NOT EXISTS settings_versions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,
    component   TEXT    NOT NULL,
    key         TEXT    NOT NULL,
    old_value   TEXT,
    new_value   TEXT    NOT NULL,
    changed_by  TEXT
);
CREATE INDEX IF NOT EXISTS idx_sv_ts ON settings_versions(component, key, ts);

CREATE TABLE IF NOT EXISTS twin_session_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES sessions(id),
    ts          REAL    NOT NULL,
    cmd_text    TEXT    NOT NULL,
    action      TEXT    NOT NULL,
    source      TEXT    NOT NULL,
    seq         INTEGER NOT NULL  -- position within session (0-based)
);
CREATE INDEX IF NOT EXISTS idx_tsh_session ON twin_session_history(session_id, seq);

CREATE TABLE IF NOT EXISTS twin_pain_day_log (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id         INTEGER NOT NULL REFERENCES sessions(id),
    ts                 REAL    NOT NULL,
    pain_day_score     REAL    NOT NULL,
    pain_day_active    INTEGER NOT NULL,  -- 0 or 1
    fail_ratio         REAL,
    clarify_ratio      REAL,
    gesture_conf_delta REAL,
    cmd_rate_delta     REAL
);
CREATE INDEX IF NOT EXISTS idx_pdl_session ON twin_pain_day_log(session_id, ts);
"""


# ---------------------------------------------------------------------------
# Scoring helpers (shared with ContinuousTrainer)
# ---------------------------------------------------------------------------

def _tokens(text: str) -> set[str]:
    return set(text.lower().split())


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def _recency_weight(ts: float, now: float, half_life_days: float = 30.0) -> float:
    age_days = (now - ts) / 86400.0
    return math.exp(-age_days * math.log(2) / half_life_days)


def _fse_score(row: dict, query_tokens: set[str], now: float) -> float:
    overlap = _jaccard(query_tokens, _tokens(row["text"]))
    recency = _recency_weight(row["ts"], now)
    usage = math.log1p(row["usage_count"])
    return overlap * recency * usage


# ---------------------------------------------------------------------------
# AgentDB — SQLite operational store
# ---------------------------------------------------------------------------

class AgentDB:
    """Async SQLite wrapper for all operational pipeline writes.

    Always check `available` before calling methods — they are no-ops when
    aiosqlite is absent, returning safe default values (None / [] / 0.6).
    """

    def __init__(self) -> None:
        self._conn: Optional["aiosqlite.Connection"] = None
        self.available = False

    # ---------------------------------------------------------------------- #
    # Lifecycle
    # ---------------------------------------------------------------------- #

    async def open(self, path: Path | str) -> None:
        if not _AIOSQLITE_AVAILABLE:
            return
        self._conn = await aiosqlite.connect(Path(path))
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(AGENT_DB_SCHEMA)
        await self._conn.commit()
        self.available = True
        log.info("AgentDB opened: %s", path)

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None
        self.available = False

    # ---------------------------------------------------------------------- #
    # Sessions
    # ---------------------------------------------------------------------- #

    async def insert_session(
        self,
        mode: str = "normal",
        git_hash: Optional[str] = None,
        agent_version: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> int:
        """Insert a new session row and return its id."""
        if not self._conn:
            return -1
        cur = await self._conn.execute(
            "INSERT INTO sessions (started_at, mode, git_hash, agent_version, notes)"
            " VALUES (?, ?, ?, ?, ?)",
            (time.time(), mode, git_hash, agent_version, notes),
        )
        await self._conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    async def close_session(self, session_id: int) -> None:
        if not self._conn or session_id < 0:
            return
        await self._conn.execute(
            "UPDATE sessions SET ended_at = ? WHERE id = ?",
            (time.time(), session_id),
        )
        await self._conn.commit()

    # ---------------------------------------------------------------------- #
    # Commands
    # ---------------------------------------------------------------------- #

    async def insert_command(
        self,
        session_id: int,
        cmd: "Command",
        action: Optional[str],
        route: Optional[str],
        gate_that_decided: Optional[str],
        latency_ms: Optional[float],
        success: Optional[bool] = None,
        error_msg: Optional[str] = None,
    ) -> int:
        """Insert a command routing record and return its id."""
        if not self._conn:
            return -1
        gaze_x = gaze_y = None
        if cmd.gaze_coords:
            gaze_x, gaze_y = cmd.gaze_coords
        success_int = None if success is None else int(success)
        try:
            cur = await self._conn.execute(
                """INSERT INTO commands
                   (session_id, ts, source, text, action, params,
                    route, gate_that_decided, latency_ms,
                    whisper_logprob, gesture_confidence,
                    gaze_x, gaze_y, success, error_msg)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    session_id, time.time(), cmd.source, cmd.text,
                    action, json.dumps(cmd.params) if cmd.params else None,
                    route, gate_that_decided,
                    round(latency_ms, 1) if latency_ms is not None else None,
                    cmd.whisper_logprob, cmd.gesture_confidence,
                    gaze_x, gaze_y, success_int, error_msg,
                ),
            )
            await self._conn.commit()
            return cur.lastrowid  # type: ignore[return-value]
        except Exception as exc:
            log.warning("AgentDB.insert_command failed: %s", exc)
            return -1

    async def mark_command_corrected(self, command_id: int, corrected_to: str) -> None:
        if not self._conn or command_id < 0:
            return
        await self._conn.execute(
            "UPDATE commands SET corrected_to = ? WHERE id = ?",
            (corrected_to, command_id),
        )
        await self._conn.commit()

    # ---------------------------------------------------------------------- #
    # Inferences
    # ---------------------------------------------------------------------- #

    async def insert_inference(
        self,
        command_id: Optional[int],
        model: str,
        domain: str,
        prompt: Optional[str],
        response: Optional[str],
        tokens_in: Optional[int],
        tokens_out: Optional[int],
        latency_ms: float,
        backend: str = "ollama",
        error: Optional[str] = None,
    ) -> int:
        if not self._conn:
            return -1
        prompt_hash = (
            hashlib.sha256(prompt.encode()).hexdigest()[:16] if prompt else None
        )
        try:
            cur = await self._conn.execute(
                """INSERT INTO inferences
                   (command_id, ts, model, domain, prompt_hash, response,
                    tokens_in, tokens_out, latency_ms, backend, error)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    command_id if (command_id and command_id > 0) else None,
                    time.time(), model, domain, prompt_hash, response,
                    tokens_in, tokens_out, round(latency_ms, 1), backend, error,
                ),
            )
            await self._conn.commit()
            return cur.lastrowid  # type: ignore[return-value]
        except Exception as exc:
            log.warning("AgentDB.insert_inference failed: %s", exc)
            return -1

    # ---------------------------------------------------------------------- #
    # Agent runs / steps
    # ---------------------------------------------------------------------- #

    async def insert_agent_run(
        self,
        command_id: Optional[int],
        goal: str,
        domain: str,
        model_used: Optional[str],
        step_count: int,
        success: bool,
        total_latency_ms: float,
        error: Optional[str] = None,
    ) -> int:
        if not self._conn:
            return -1
        try:
            cur = await self._conn.execute(
                """INSERT INTO agent_runs
                   (command_id, ts, goal, domain, model_used,
                    step_count, success, total_latency_ms, error)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    command_id if (command_id and command_id > 0) else None,
                    time.time(), goal, domain, model_used,
                    step_count, int(success), round(total_latency_ms, 1), error,
                ),
            )
            await self._conn.commit()
            return cur.lastrowid  # type: ignore[return-value]
        except Exception as exc:
            log.warning("AgentDB.insert_agent_run failed: %s", exc)
            return -1

    async def insert_agent_step(
        self,
        run_id: int,
        step_num: int,
        action: str,
        args: Optional[str],
        body: Optional[str],
        result: Optional[str],
        success: Optional[bool],
        latency_ms: float,
    ) -> None:
        if not self._conn or run_id < 0:
            return
        try:
            await self._conn.execute(
                """INSERT INTO agent_steps
                   (run_id, step_num, action, args, body, result, success, latency_ms)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    run_id, step_num, action, args or None, body or None,
                    result or None,
                    None if success is None else int(success),
                    round(latency_ms, 1),
                ),
            )
            await self._conn.commit()
        except Exception as exc:
            log.warning("AgentDB.insert_agent_step failed: %s", exc)

    # ---------------------------------------------------------------------- #
    # Few-shot examples
    # ---------------------------------------------------------------------- #

    async def upsert_few_shot_example(
        self,
        cmd: "Command",
        action_str: str,
        domain: str = "command",
        command_id: Optional[int] = None,
    ) -> None:
        if not self._conn:
            return
        now = time.time()
        try:
            await self._conn.execute(
                """INSERT INTO few_shot_examples
                   (command_id, text, action, source, domain, ts, usage_count)
                   VALUES (?, ?, ?, ?, ?, ?, 1)
                   ON CONFLICT(text, action) DO UPDATE
                     SET usage_count = usage_count + 1, ts = excluded.ts""",
                (
                    command_id if (command_id and command_id > 0) else None,
                    cmd.text, action_str, cmd.source, domain, now,
                ),
            )
            # Word count tracking for hotword promotion
            for word in _tokens(cmd.text):
                if len(word) >= 3:
                    await self._conn.execute(
                        """INSERT INTO word_counts (word, count) VALUES (?, 1)
                           ON CONFLICT(word) DO UPDATE SET count = count + 1""",
                        (word,),
                    )
            await self._conn.commit()
        except Exception as exc:
            log.warning("AgentDB.upsert_few_shot_example failed: %s", exc)
            return

        # Compute and store embedding if MiniLM is available and row has none yet
        try:
            encoder = await _get_encoder()
            if encoder is None:
                return
            emb = await asyncio.to_thread(_encode_sync, cmd.text, encoder)
            await self._conn.execute(
                """UPDATE few_shot_examples SET embedding = ?
                   WHERE text = ? AND action = ? AND embedding IS NULL""",
                (emb, cmd.text, action_str),
            )
            await self._conn.commit()
        except Exception as exc:
            log.debug("Embedding update failed (non-fatal): %s", exc)

    async def get_few_shot_examples(
        self,
        cmd: "Command",
        n: int = 5,
        domain: str = "command",
    ) -> list[dict]:
        """Return up to n examples ranked by (cosine | Jaccard) × recency × usage.

        Uses cosine similarity when both the query and the stored row have
        embeddings; falls back to Jaccard word-overlap otherwise. Mixed rows
        (some with embeddings, some without) are handled per-row.
        """
        if not self._conn:
            return []
        try:
            now = time.time()
            query_tokens = _tokens(cmd.text)

            # Try to get a query embedding — non-blocking, falls back gracefully
            encoder = await _get_encoder()
            query_emb: Optional[bytes] = None
            if encoder is not None:
                try:
                    query_emb = await asyncio.to_thread(_encode_sync, cmd.text, encoder)
                except Exception:
                    pass

            async with self._conn.execute(
                """SELECT text, action, ts, usage_count, embedding
                   FROM few_shot_examples
                   WHERE domain = ?
                   ORDER BY ts DESC LIMIT 1000""",
                (domain,),
            ) as cur:
                rows = [dict(r) for r in await cur.fetchall()]

            def _score(row: dict) -> float:
                recency = _recency_weight(row["ts"], now)
                usage   = math.log1p(row["usage_count"])
                if query_emb is not None and row.get("embedding"):
                    sim = _cosine(query_emb, row["embedding"])
                else:
                    sim = _jaccard(query_tokens, _tokens(row["text"]))
                return sim * recency * usage

            scored = sorted(rows, key=_score, reverse=True)
            return [
                {"command_text": r["text"], "action_text": r["action"]}
                for r in scored[:n]
                if _score(r) > 0.0
            ]
        except Exception as exc:
            log.warning("AgentDB.get_few_shot_examples failed: %s", exc)
            return []

    # ---------------------------------------------------------------------- #
    # Hotwords
    # ---------------------------------------------------------------------- #

    async def get_hotwords(self) -> list[str]:
        if not self._conn:
            return []
        try:
            async with self._conn.execute("SELECT word FROM hotwords") as cur:
                return [r["word"] for r in await cur.fetchall()]
        except Exception as exc:
            log.warning("AgentDB.get_hotwords failed: %s", exc)
            return []

    async def promote_hotwords(self, threshold: int = 3) -> None:
        if not self._conn:
            return
        try:
            async with self._conn.execute(
                "SELECT word FROM word_counts WHERE count >= ?", (threshold,)
            ) as cur:
                candidates = [r["word"] for r in await cur.fetchall()]
            for word in candidates:
                await self._conn.execute(
                    "INSERT OR IGNORE INTO hotwords (word, added) VALUES (?, ?)",
                    (word, time.time()),
                )
            if candidates:
                await self._conn.commit()
        except Exception as exc:
            log.warning("AgentDB.promote_hotwords failed: %s", exc)

    # ---------------------------------------------------------------------- #
    # Gesture samples & calibration
    # ---------------------------------------------------------------------- #

    async def record_gesture_sample(
        self,
        gesture: str,
        confidence: float,
        lidar_depth_m: Optional[float] = None,
        command_id: Optional[int] = None,
    ) -> None:
        if not self._conn:
            return
        try:
            await self._conn.execute(
                """INSERT INTO gesture_samples
                   (command_id, ts, gesture, confidence, lidar_depth_m)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    command_id if (command_id and command_id > 0) else None,
                    time.time(), gesture, confidence, lidar_depth_m,
                ),
            )
            await self._conn.commit()
        except Exception as exc:
            log.warning("AgentDB.record_gesture_sample failed: %s", exc)

    async def get_recent_gesture_samples(
        self, gesture: str, limit: int = 500
    ) -> list[float]:
        if not self._conn:
            return []
        try:
            async with self._conn.execute(
                """SELECT confidence FROM gesture_samples
                   WHERE gesture = ?
                   ORDER BY ts DESC LIMIT ?""",
                (gesture, limit),
            ) as cur:
                return [r["confidence"] for r in await cur.fetchall()]
        except Exception as exc:
            log.warning("AgentDB.get_recent_gesture_samples failed: %s", exc)
            return []

    async def update_gesture_calibration(
        self,
        gesture: str,
        confidence_floor: float,
        sample_count: int,
        p10: float,
    ) -> None:
        if not self._conn:
            return
        try:
            await self._conn.execute(
                """INSERT INTO gesture_calibration
                   (ts, gesture, confidence_floor, sample_count, p10)
                   VALUES (?, ?, ?, ?, ?)""",
                (time.time(), gesture, confidence_floor, sample_count, p10),
            )
            await self._conn.commit()
        except Exception as exc:
            log.warning("AgentDB.update_gesture_calibration failed: %s", exc)

    async def get_gesture_floor(self, gesture: str) -> float:
        if not self._conn:
            return 0.60
        try:
            async with self._conn.execute(
                """SELECT confidence_floor FROM gesture_calibration
                   WHERE gesture = ?
                   ORDER BY ts DESC LIMIT 1""",
                (gesture,),
            ) as cur:
                row = await cur.fetchone()
                return row["confidence_floor"] if row else 0.60
        except Exception as exc:
            log.warning("AgentDB.get_gesture_floor failed: %s", exc)
            return 0.60

    # Gesture velocity calibration (Minority Report motion gestures)

    async def record_gesture_velocity(
        self, gesture: str, velocity: float, pain_day: bool = False
    ) -> None:
        if not self._conn:
            return
        try:
            await self._conn.execute(
                "INSERT INTO gesture_velocity_samples (ts, gesture, velocity, pain_day)"
                " VALUES (?, ?, ?, ?)",
                (time.time(), gesture, velocity, int(pain_day)),
            )
            await self._conn.commit()
        except Exception as exc:
            log.warning("AgentDB.record_gesture_velocity failed: %s", exc)

    async def get_recent_gesture_velocities(
        self, gesture: str, limit: int = 500
    ) -> list[float]:
        if not self._conn:
            return []
        try:
            async with self._conn.execute(
                "SELECT velocity FROM gesture_velocity_samples"
                " WHERE gesture = ? ORDER BY ts DESC LIMIT ?",
                (gesture, limit),
            ) as cur:
                return [r["velocity"] for r in await cur.fetchall()]
        except Exception as exc:
            log.warning("AgentDB.get_recent_gesture_velocities failed: %s", exc)
            return []

    async def update_gesture_velocity_calibration(
        self, gesture: str, velocity_floor: float, sample_count: int, p10: float
    ) -> None:
        if not self._conn:
            return
        try:
            await self._conn.execute(
                "INSERT INTO gesture_velocity_calibration"
                " (ts, gesture, velocity_floor, sample_count, p10)"
                " VALUES (?, ?, ?, ?, ?)",
                (time.time(), gesture, velocity_floor, sample_count, p10),
            )
            await self._conn.commit()
        except Exception as exc:
            log.warning("AgentDB.update_gesture_velocity_calibration failed: %s", exc)

    async def get_gesture_velocity_floor(
        self, gesture: str, default: float = 1.2
    ) -> float:
        if not self._conn:
            return default
        try:
            async with self._conn.execute(
                "SELECT velocity_floor FROM gesture_velocity_calibration"
                " WHERE gesture = ? ORDER BY ts DESC LIMIT 1",
                (gesture,),
            ) as cur:
                row = await cur.fetchone()
                return row["velocity_floor"] if row else default
        except Exception as exc:
            log.warning("AgentDB.get_gesture_velocity_floor failed: %s", exc)
            return default

    # ---------------------------------------------------------------------- #
    # Adaptation queries (used by ContinuousTrainer)
    # ---------------------------------------------------------------------- #

    async def get_recent_routing_stats(self, limit: int = 1000) -> list[dict]:
        """Return recent command rows for gate threshold adaptation."""
        if not self._conn:
            return []
        try:
            async with self._conn.execute(
                """SELECT route, action FROM commands
                   ORDER BY ts DESC LIMIT ?""",
                (limit,),
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]
        except Exception as exc:
            log.warning("AgentDB.get_recent_routing_stats failed: %s", exc)
            return []

    # ---------------------------------------------------------------------- #
    # Sensor events
    # ---------------------------------------------------------------------- #

    async def insert_sensor_event(
        self,
        event_type: str,
        x: Optional[float] = None,
        y: Optional[float] = None,
        confidence: Optional[float] = None,
        value: Optional[str] = None,
        params: Optional[dict] = None,
        command_id: Optional[int] = None,
    ) -> None:
        if not self._conn:
            return
        try:
            await self._conn.execute(
                """INSERT INTO sensor_events
                   (command_id, ts, event_type, x, y, confidence, value, params)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    command_id if (command_id and command_id > 0) else None,
                    time.time(), event_type, x, y, confidence, value,
                    json.dumps(params) if params else None,
                ),
            )
            await self._conn.commit()
        except Exception as exc:
            log.warning("AgentDB.insert_sensor_event failed: %s", exc)

    # ---------------------------------------------------------------------- #
    # Behavioral twin queries
    # ---------------------------------------------------------------------- #

    async def get_recent_successful_commands(self, limit: int = 500) -> list[dict]:
        """Return the N most recent successful commands across all sessions."""
        if not self._conn:
            return []
        try:
            async with self._conn.execute(
                """SELECT text, action, source, ts, session_id
                   FROM commands
                   WHERE success = 1
                   ORDER BY ts DESC LIMIT ?""",
                (limit,),
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]
        except Exception as exc:
            log.warning("AgentDB.get_recent_successful_commands failed: %s", exc)
            return []

    async def get_session_commands(self, session_id: int, limit: int = 20) -> list[dict]:
        """Return the last N commands from a specific session."""
        if not self._conn:
            return []
        try:
            async with self._conn.execute(
                """SELECT text, action, source, ts, session_id
                   FROM commands
                   WHERE session_id = ?
                   ORDER BY ts DESC LIMIT ?""",
                (session_id, limit),
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]
        except Exception as exc:
            log.warning("AgentDB.get_session_commands failed: %s", exc)
            return []

    async def get_most_recent_session_id(
        self, exclude_session_id: Optional[int] = None
    ) -> Optional[int]:
        """Return the id of the most recent session (optionally excluding current)."""
        if not self._conn:
            return None
        try:
            if exclude_session_id is not None:
                async with self._conn.execute(
                    """SELECT id FROM sessions
                       WHERE id != ?
                       ORDER BY started_at DESC LIMIT 1""",
                    (exclude_session_id,),
                ) as cur:
                    row = await cur.fetchone()
            else:
                async with self._conn.execute(
                    """SELECT id FROM sessions
                       ORDER BY started_at DESC LIMIT 1"""
                ) as cur:
                    row = await cur.fetchone()
            return row["id"] if row else None
        except Exception as exc:
            log.warning("AgentDB.get_most_recent_session_id failed: %s", exc)
            return None

    async def get_command_stats_last_n_days(self, days: int = 30) -> list[dict]:
        """Return commands from the last N days for time-of-day distribution."""
        if not self._conn:
            return []
        cutoff = time.time() - days * 86400
        try:
            async with self._conn.execute(
                """SELECT ts, action, source, success
                   FROM commands
                   WHERE ts >= ?
                   ORDER BY ts DESC""",
                (cutoff,),
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]
        except Exception as exc:
            log.warning("AgentDB.get_command_stats_last_n_days failed: %s", exc)
            return []

    async def get_source_stats_last_n_days(self, days: int = 7) -> list[dict]:
        """Return per-source success/total counts for the last N days."""
        if not self._conn:
            return []
        cutoff = time.time() - days * 86400
        try:
            async with self._conn.execute(
                """SELECT source,
                          SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) AS success_count,
                          COUNT(*) AS total_count
                   FROM commands
                   WHERE ts >= ?
                   GROUP BY source""",
                (cutoff,),
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]
        except Exception as exc:
            log.warning("AgentDB.get_source_stats_last_n_days failed: %s", exc)
            return []

    async def write_session_history(
        self, session_id: int, history: list[dict]
    ) -> None:
        """Persist SessionHistory to twin_session_history table at session close."""
        if not self._conn:
            return
        try:
            await self._conn.execute(
                "DELETE FROM twin_session_history WHERE session_id = ?",
                (session_id,),
            )
            for seq, item in enumerate(history):
                await self._conn.execute(
                    """INSERT INTO twin_session_history
                       (session_id, ts, cmd_text, action, source, seq)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        session_id,
                        item["ts"],
                        item["cmd_text"],
                        item["action"],
                        item["source"],
                        seq,
                    ),
                )
            await self._conn.commit()
        except Exception as exc:
            log.warning("AgentDB.write_session_history failed: %s", exc)

    async def read_session_history(
        self, session_id: int, limit: int = 20
    ) -> list[dict]:
        """Read SessionHistory for a given session."""
        if not self._conn:
            return []
        try:
            async with self._conn.execute(
                """SELECT ts, cmd_text, action, source, seq
                   FROM twin_session_history
                   WHERE session_id = ?
                   ORDER BY seq ASC LIMIT ?""",
                (session_id, limit),
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]
        except Exception as exc:
            log.warning("AgentDB.read_session_history failed: %s", exc)
            return []

    async def log_pain_day(
        self,
        session_id: int,
        score: float,
        active: bool,
        fail_ratio: float,
        clarify_ratio: float,
        gesture_conf_delta: float,
        cmd_rate_delta: float,
    ) -> None:
        """Append a pain_day_log row."""
        if not self._conn:
            return
        try:
            await self._conn.execute(
                """INSERT INTO twin_pain_day_log
                   (session_id, ts, pain_day_score, pain_day_active,
                    fail_ratio, clarify_ratio, gesture_conf_delta, cmd_rate_delta)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id, time.time(), score, 1 if active else 0,
                    fail_ratio, clarify_ratio, gesture_conf_delta, cmd_rate_delta,
                ),
            )
            await self._conn.commit()
        except Exception as exc:
            log.warning("AgentDB.log_pain_day failed: %s", exc)

    async def get_preference_model_snapshot(self) -> Optional[str]:
        """Return the most recent preference_model JSON from settings_versions."""
        if not self._conn:
            return None
        try:
            async with self._conn.execute(
                """SELECT new_value FROM settings_versions
                   WHERE component = 'preference_model' AND key = 'snapshot'
                   ORDER BY ts DESC LIMIT 1""",
            ) as cur:
                row = await cur.fetchone()
                return row["new_value"] if row else None
        except Exception as exc:
            log.warning("AgentDB.get_preference_model_snapshot failed: %s", exc)
            return None

    # ---------------------------------------------------------------------- #
    # Settings change log
    # ---------------------------------------------------------------------- #

    async def log_settings_change(
        self,
        component: str,
        key: str,
        old_value,
        new_value,
        changed_by: str = "user",
    ) -> None:
        if not self._conn:
            return
        try:
            await self._conn.execute(
                """INSERT INTO settings_versions
                   (ts, component, key, old_value, new_value, changed_by)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    time.time(), component, key,
                    json.dumps(old_value) if old_value is not None else None,
                    json.dumps(new_value),
                    changed_by,
                ),
            )
            await self._conn.commit()
        except Exception as exc:
            log.warning("AgentDB.log_settings_change failed: %s", exc)


# ---------------------------------------------------------------------------
# AnalyticsDB — DuckDB analytical store
# ---------------------------------------------------------------------------

_ANALYTICS_SCHEMA = """
CREATE SEQUENCE IF NOT EXISTS seq_benchmark_runs    START 1 INCREMENT 1;
CREATE SEQUENCE IF NOT EXISTS seq_benchmark_results START 1 INCREMENT 1;
CREATE SEQUENCE IF NOT EXISTS seq_benchmark_prompts START 1 INCREMENT 1;

CREATE TABLE IF NOT EXISTS benchmark_runs (
    id       BIGINT PRIMARY KEY,
    ts       DOUBLE  NOT NULL,
    git_hash VARCHAR,
    mode     VARCHAR DEFAULT 'standard',
    notes    VARCHAR
);

CREATE TABLE IF NOT EXISTS benchmark_results (
    id             BIGINT PRIMARY KEY,
    run_id         BIGINT  NOT NULL,
    model          VARCHAR NOT NULL,
    accuracy_pct   DOUBLE,
    correct        INTEGER,
    total          INTEGER,
    p50_ms         DOUBLE,
    p95_ms         DOUBLE,
    vram_before_gb DOUBLE,
    vram_after_gb  DOUBLE,
    vram_delta_gb  DOUBLE,
    error          VARCHAR
);

CREATE TABLE IF NOT EXISTS benchmark_prompts (
    id        BIGINT PRIMARY KEY,
    result_id BIGINT  NOT NULL,
    prompt    VARCHAR NOT NULL,
    expected  VARCHAR NOT NULL,
    got       VARCHAR,
    correct   BOOLEAN,
    p50_ms    DOUBLE,
    p95_ms    DOUBLE
);
"""


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
        new_id = int(row[0])
        self._conn.execute(
            """INSERT INTO benchmark_prompts
               (id, result_id, prompt, expected, got, correct, p50_ms, p95_ms)
               VALUES (?,?,?,?,?,?,?,?)""",
            [new_id, result_id, prompt, expected, got, correct, p50_ms, p95_ms],
        )
