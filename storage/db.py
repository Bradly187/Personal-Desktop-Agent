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
    from core.command_executor import Command


# ---------------------------------------------------------------------------
# agent.db schema — all 29 tables
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
    corrected_to       TEXT,
    trace_id           TEXT          -- cross-layer trace id (DA_TRACE); links to monitoring/trace.py spans
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
    error            TEXT,
    -- Lifecycle for crash recovery: 'running' while a plan executes, then a
    -- terminal status ('completed'/'failed'/'cancelled'). On startup any row
    -- still 'running' is reconciled to 'interrupted' (the process died mid-plan).
    status           TEXT NOT NULL DEFAULT 'completed'
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

-- Durable goal backlog (gap D): goals authorized for autonomous execution are
-- persisted here BEFORE they run, so a crash/shed never drops queued work. The
-- agent_runs/agent_steps ledger journals an *executing* plan; this is the
-- pre-execution *queue*. idempotency_key is UNIQUE so a re-enqueue (e.g. crash
-- recovery) can't create a duplicate. Lifecycle: queued → running → done/failed/
-- cancelled; a row left 'running' at startup is requeued (bounded by attempts).
CREATE TABLE IF NOT EXISTS goal_queue (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL    NOT NULL,
    goal            TEXT    NOT NULL,
    domain          TEXT    NOT NULL DEFAULT 'plan',
    status          TEXT    NOT NULL DEFAULT 'queued',
    idempotency_key TEXT    UNIQUE,
    attempts        INTEGER NOT NULL DEFAULT 0,
    max_attempts    INTEGER NOT NULL DEFAULT 3,
    last_error      TEXT,
    run_id          INTEGER REFERENCES agent_runs(id)
);
CREATE INDEX IF NOT EXISTS idx_goalq_status ON goal_queue(status, ts);

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

-- Counterexamples: (text, wrong_action) pairs captured from failures and user
-- corrections. Injected as "Do NOT produce" guidance in prompts so the local LLM
-- stops mapping a phrasing to an action the user has already rejected.
-- Source reason: "pipeline_failure" | "user_correction"
CREATE TABLE IF NOT EXISTS few_shot_counterexamples (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    command_id   INTEGER REFERENCES commands(id),
    text         TEXT    NOT NULL,
    wrong_action TEXT    NOT NULL,
    reason       TEXT,
    source       TEXT    NOT NULL,
    domain       TEXT    NOT NULL DEFAULT 'command',
    ts           REAL    NOT NULL,
    embedding    BLOB,
    usage_count  INTEGER NOT NULL DEFAULT 1,
    UNIQUE(text, wrong_action)
);
CREATE INDEX IF NOT EXISTS idx_fsce_ts     ON few_shot_counterexamples(ts);
CREATE INDEX IF NOT EXISTS idx_fsce_domain ON few_shot_counterexamples(domain);

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

-- ── Voice calibration (Sprint A) ─────────────────────────────────────────

-- Per-utterance acoustic measurements collected during calibration sessions
-- and passively during normal operation.
CREATE TABLE IF NOT EXISTS voice_calibration (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id     INTEGER REFERENCES sessions(id),
    ts             REAL    NOT NULL,
    phrase         TEXT,               -- phrase presented to user
    actual_text    TEXT,               -- what Whisper transcribed
    rms_amplitude  REAL,               -- RMS of voiced frames (0.0–1.0)
    freq_centroid  REAL,               -- spectral centroid Hz
    avg_logprob    REAL,               -- Whisper segment confidence
    duration_s     REAL,               -- speech duration
    is_flare_day   INTEGER DEFAULT 0   -- 1 when pain_day_active at capture time
);
CREATE INDEX IF NOT EXISTS idx_vc_session ON voice_calibration(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_vc_flare   ON voice_calibration(is_flare_day, ts);

-- Single-row per-user voice profile derived from calibration samples.
-- Updated in-place; history lives in voice_calibration.
CREATE TABLE IF NOT EXISTS voice_profile (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    updated_at          REAL    NOT NULL,
    baseline_rms        REAL,           -- median RMS on healthy days
    baseline_logprob    REAL,           -- median Whisper logprob on healthy days
    baseline_freq       REAL,           -- median frequency centroid Hz
    flare_rms_scale     REAL DEFAULT 0.5,  -- flare voice volume as fraction of baseline
    vad_threshold       REAL,           -- computed silence threshold for WhisperStream
    logprob_floor       REAL,           -- computed Gate 1 logprob floor
    sample_count        INTEGER DEFAULT 0
);

-- Phrase bank presented during voice onboarding.
CREATE TABLE IF NOT EXISTS voice_phrases (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    phrase    TEXT    NOT NULL,         -- what to display and say
    category  TEXT    NOT NULL,         -- verb / app / navigation / number
    phonetic  TEXT,                     -- expected Whisper output (may differ)
    active    INTEGER NOT NULL DEFAULT 1
);

-- Per-sensor range-of-motion measurements from assessment onboarding.
CREATE TABLE IF NOT EXISTS sensor_rom (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                REAL    NOT NULL,
    session_id        INTEGER REFERENCES sessions(id),
    sensor            TEXT    NOT NULL,   -- voice/gaze/tilt/head/gesture/sound
    direction         TEXT,               -- left/right/up/down/pinch/cluck/etc
    max_value         REAL,               -- maximum comfortable measurement
    comfortable_value REAL,               -- daily-use comfortable value
    unit              TEXT                -- degrees/rms/confidence/etc
);
CREATE INDEX IF NOT EXISTS idx_rom_sensor ON sensor_rom(sensor, ts);

-- User-defined flare degradation profile.
CREATE TABLE IF NOT EXISTS flare_profile (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    updated_at          REAL    NOT NULL,
    voice_degrades      INTEGER NOT NULL DEFAULT 1,
    gesture_degrades    INTEGER NOT NULL DEFAULT 0,
    gaze_degrades       INTEGER NOT NULL DEFAULT 0,
    tilt_degrades       INTEGER NOT NULL DEFAULT 0,
    sound_degrades      INTEGER NOT NULL DEFAULT 1,    -- mouth-sound cooldown relaxes on flare
    flare_vad_scale     REAL    NOT NULL DEFAULT 0.5,  -- voice volume fraction
    manual_pain_day     INTEGER NOT NULL DEFAULT 0,    -- user override flag
    notes               TEXT
);

-- Voice calibration sessions — one row per calibration run.
CREATE TABLE IF NOT EXISTS voice_calibration_sessions (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL    NOT NULL,
    condition TEXT    NOT NULL,  -- good_day | flare_day | allergy_day | svt_attack
    notes     TEXT
);

-- Per-phrase calibration results captured during a session.
CREATE TABLE IF NOT EXISTS voice_pronunciations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    INTEGER NOT NULL REFERENCES voice_calibration_sessions(id),
    ts            REAL    NOT NULL,
    expected      TEXT    NOT NULL,
    heard         TEXT    NOT NULL,
    logprob       REAL,
    duration_s    REAL,
    accepted      INTEGER NOT NULL DEFAULT 1  -- 1=used in profile, 0=rejected
);
CREATE INDEX IF NOT EXISTS idx_vp_session ON voice_pronunciations(session_id);
CREATE INDEX IF NOT EXISTS idx_vp_expected ON voice_pronunciations(expected);

-- Compiled voice profiles — one row per condition, updated after each session.
CREATE TABLE IF NOT EXISTS voice_profiles (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    condition        TEXT    NOT NULL UNIQUE,
    corrections_json TEXT    NOT NULL DEFAULT '{}',
    vad_threshold    REAL    NOT NULL DEFAULT 0.015,
    logprob_floor    REAL    NOT NULL DEFAULT -0.8,
    initial_prompt   TEXT,
    updated_at       REAL    NOT NULL
);

-- Lecture mode transcriptions: speech heard while lecture mode is active.
-- Captures lecture audio for later search and review via DevAgent.
CREATE TABLE IF NOT EXISTS ambient_transcripts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER REFERENCES sessions(id),
    ts          REAL    NOT NULL,
    text        TEXT    NOT NULL,
    logprob     REAL,
    duration_s  REAL
);
CREATE INDEX IF NOT EXISTS idx_at_session ON ambient_transcripts(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_at_ts      ON ambient_transcripts(ts);

CREATE TABLE IF NOT EXISTS ipad_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER REFERENCES sessions(id),
    ts          REAL    NOT NULL,
    level       TEXT    NOT NULL,   -- 'debug' | 'info' | 'warning' | 'error' | 'fault'
    subsystem   TEXT    NOT NULL,
    msg         TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ipad_logs_session ON ipad_logs(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_ipad_logs_level   ON ipad_logs(level, ts);

-- D5: Adaptation effectiveness log — records pre/post metrics for each
-- training adaptation so the trainer can detect and roll back bad changes.
CREATE TABLE IF NOT EXISTS adaptation_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL    NOT NULL,
    component     TEXT    NOT NULL,   -- 'gate1' | 'gesture_floor' | 'velocity_floor'
    metric_before REAL,               -- threshold value before this adaptation
    metric_after  REAL,               -- threshold value after this adaptation
    cloud_rate    REAL,               -- cloud escalation rate at time of adaptation
    failure_rate  REAL,               -- pipeline failure rate at time of adaptation
    rolled_back   INTEGER DEFAULT 0   -- 1 if this change was subsequently rolled back
);
CREATE INDEX IF NOT EXISTS idx_al_component ON adaptation_log(component, ts);

-- ── Continuous sensor telemetry — 1 Hz ambient snapshot (ML dataset) ──────
-- One row per second regardless of whether a command fired.
-- Provides continuous signal for fatigue detection, pain-day onset, ROM drift.
-- tilt_rx/ry: gyro velocity rad/s (velocity mode) or None if sensor inactive.
-- gaze_dx/dy/conf: last received gaze-delta values (relative movement, pixels).
-- head_pitch/yaw: ARKit head pose degrees (None if inactive).
-- cursor_x/y: actual screen cursor position in pixels at sample time.
-- pain_day_active: 1 when PainDayEngine threshold is exceeded or override set.
-- active_source: last sensor that drove a cursor move or command this second.
-- gesture_conf: confidence of last gesture event in this window (NULL if none).
-- rms_ambient: AcousticProfiler RMS of last voice utterance (NULL if silent).
CREATE TABLE IF NOT EXISTS sensor_telemetry (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id       INTEGER REFERENCES sessions(id),
    ts               REAL    NOT NULL,
    tilt_rx          REAL,
    tilt_ry          REAL,
    gaze_dx          REAL,
    gaze_dy          REAL,
    gaze_conf        REAL,
    head_pitch       REAL,
    head_yaw         REAL,
    cursor_x         INTEGER,
    cursor_y         INTEGER,
    pain_day_active  INTEGER NOT NULL DEFAULT 0,
    active_source    TEXT,
    gesture_conf     REAL,
    rms_ambient      REAL
);
CREATE INDEX IF NOT EXISTS idx_st_session ON sensor_telemetry(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_st_ts      ON sensor_telemetry(ts);

-- ── Session summaries — written at session close by SessionAnalyzer ────────
-- Aggregated KPIs over one agent run. Primary table for dashboard queries.
CREATE TABLE IF NOT EXISTS session_summaries (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id            INTEGER UNIQUE REFERENCES sessions(id),
    ts                    REAL    NOT NULL,
    duration_s            REAL,
    total_commands        INTEGER NOT NULL DEFAULT 0,
    success_rate          REAL,           -- fraction 0.0–1.0
    cloud_escalation_rate REAL,           -- fraction of commands routed to the cloud
    gate0_blocks          INTEGER DEFAULT 0,
    gate1_blocks          INTEGER DEFAULT 0,
    gate2_blocks          INTEGER DEFAULT 0,
    gate3_blocks          INTEGER DEFAULT 0,
    gate4_blocks          INTEGER DEFAULT 0,
    latency_p50_ms        REAL,
    latency_p95_ms        REAL,
    pain_day_pct          REAL,           -- fraction of session where pain_day_active=1
    corrections_count     INTEGER DEFAULT 0,
    avg_whisper_logprob   REAL,
    avg_gesture_conf      REAL,
    source_breakdown      TEXT,           -- JSON {voice:N, gesture:N, touch:N, ...}
    domain_breakdown      TEXT,           -- JSON {command:N, code:N, math:N, ...}
    top_actions           TEXT            -- JSON [[action, count], ...]
);

-- ── Event bus — append-only structured log for topic fan-out ─────────────────
-- Provides durable replay + in-process asyncio.Queue real-time delivery.
-- Consumers track their cursor via event_consumers.last_event_id.
-- Prune: 7 days (similar cadence to sensor_telemetry).
CREATE TABLE IF NOT EXISTS event_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,
    topic       TEXT    NOT NULL,     -- dotted: 'command.executed', 'gate.decided', 'step.failed'
    session_id  INTEGER REFERENCES sessions(id),
    command_id  INTEGER REFERENCES commands(id),
    trace_id    TEXT,                 -- correlates to commands.trace_id
    source      TEXT    NOT NULL,     -- emitting component name
    payload     TEXT    NOT NULL      -- JSON blob; schema per-topic documented in core/events.py
);
CREATE INDEX IF NOT EXISTS idx_event_log_topic_id ON event_log(topic, id);
CREATE INDEX IF NOT EXISTS idx_event_log_ts       ON event_log(ts);
CREATE INDEX IF NOT EXISTS idx_event_log_trace    ON event_log(trace_id) WHERE trace_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS event_consumers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    consumer_name   TEXT    NOT NULL UNIQUE,
    topic_pattern   TEXT    NOT NULL,           -- SQL LIKE pattern: 'command.%', 'gate.%'
    last_event_id   INTEGER NOT NULL DEFAULT 0, -- cursor; poll WHERE id > last_event_id
    updated_at      REAL    NOT NULL
);

-- ── Saga compensations — reverse actions for DevAgent plan steps ──────────────
-- One row per executed step that declares a compensation. Populated as steps run;
-- triggered in reverse order when MAX_REPLANS is exhausted or plan is cancelled.
-- Never pruned — forensic value is high; volume is very low (≤200 rows/day).
CREATE TABLE IF NOT EXISTS saga_compensations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                  REAL    NOT NULL,
    run_id              INTEGER NOT NULL REFERENCES agent_runs(id),
    step_id             INTEGER NOT NULL REFERENCES agent_steps(id),
    compensation_action TEXT    NOT NULL,  -- verb: 'DELETE_FILE', 'REVERT_TERMINAL'
    compensation_args   TEXT,             -- JSON; must be fully self-contained
    status              TEXT    NOT NULL DEFAULT 'pending',  -- pending/running/done/failed/skipped
    triggered_by        TEXT,             -- 'step_failure' | 'user_cancel' | 'max_replans'
    started_at          REAL,
    finished_at         REAL,
    error               TEXT
);
CREATE INDEX IF NOT EXISTS idx_saga_run  ON saga_compensations(run_id);
CREATE INDEX IF NOT EXISTS idx_saga_step ON saga_compensations(step_id);

-- ── Per-call tool execution log — idempotency + timeout tracking ─────────────
-- Records every MCP/desktop tool invocation. idempotency_key is
-- SHA-256(tool_name + canonical_args_json) for idempotent verbs; NULL otherwise.
-- The UNIQUE index on (idempotency_key) WHERE completed prevents re-running a
-- successful idempotent call (e.g. WRITE_FILE) if the coordinator retries.
-- Prune: 30 days (~6 000 rows; negligible).
CREATE TABLE IF NOT EXISTS tool_calls (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL    NOT NULL,
    command_id      INTEGER REFERENCES commands(id),
    run_id          INTEGER REFERENCES agent_runs(id),
    step_id         INTEGER REFERENCES agent_steps(id),
    tool_name       TEXT    NOT NULL,
    idempotency_key TEXT,
    args_json       TEXT,
    result_json     TEXT,
    success         INTEGER,
    latency_ms      REAL,
    timeout_ms      INTEGER,
    status          TEXT    NOT NULL DEFAULT 'completed'  -- running/completed/failed/timeout
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tool_calls_idem
    ON tool_calls(idempotency_key) WHERE idempotency_key IS NOT NULL AND status = 'completed';
CREATE INDEX IF NOT EXISTS idx_tool_calls_cmd  ON tool_calls(command_id);
CREATE INDEX IF NOT EXISTS idx_tool_calls_step ON tool_calls(step_id);

-- ── Config tables (rarely written; read at init) ─────────────────────────────
-- Populated via INSERT OR IGNORE in AgentDB.open(); never pruned.

CREATE TABLE IF NOT EXISTS tool_timeout_config (
    tool_name   TEXT    PRIMARY KEY,
    timeout_ms  INTEGER NOT NULL,
    max_retries INTEGER NOT NULL DEFAULT 0,
    updated_at  REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS tool_cache_config (
    tool_name   TEXT    PRIMARY KEY,
    ttl_s       REAL    NOT NULL,
    max_entries INTEGER NOT NULL DEFAULT 200,
    updated_at  REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS rate_limit_config (
    resource        TEXT    PRIMARY KEY,
    max_rps         REAL    NOT NULL,
    burst_capacity  INTEGER NOT NULL DEFAULT 1,
    updated_at      REAL    NOT NULL
);

-- ── Rate limit breach log — observability only ───────────────────────────────
-- Prune: 7 days.
CREATE TABLE IF NOT EXISTS rate_limit_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,
    resource    TEXT    NOT NULL,
    command_id  INTEGER REFERENCES commands(id),
    wait_ms     REAL    NOT NULL DEFAULT 0,
    was_dropped INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_rle_resource_ts ON rate_limit_events(resource, ts);
"""


# Additive column migrations (see AgentDB._migrate). Each entry adds a column to
# a table created before that column existed. Bump _AGENT_DB_SCHEMA_VERSION and
# append a row when introducing a new additive column; the batch is gated by
# PRAGMA user_version so it runs at most once per database file.
_AGENT_DB_SCHEMA_VERSION = 5
_AGENT_DB_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    # v1→2
    ("flare_profile", "sound_degrades", "INTEGER NOT NULL DEFAULT 1"),
    ("agent_runs", "status", "TEXT NOT NULL DEFAULT 'completed'"),
    ("commands", "trace_id", "TEXT"),
    ("adaptation_log", "domain", "TEXT"),       # gap H: per-domain SLO adaptation
    # v2→3
    ("ipad_logs",   "trace_id",           "TEXT"),          # iPad↔PC trace correlation
    ("agent_steps", "compensation_action", "TEXT"),          # saga: reverse verb
    ("agent_steps", "compensation_args",   "TEXT"),          # saga: reverse args JSON
    # v3→4
    ("inferences", "prompt", "TEXT"),           # fine-tuning: full prompt text
)


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
        for table, column, ddl in _AGENT_DB_MIGRATIONS:
            try:
                await self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"
                )
            except Exception as exc:
                if "duplicate column name" not in str(exc).lower():
                    log.warning(
                        "AgentDB migration ALTER %s.%s failed: %s", table, column, exc
                    )
                # else: column already present (fresh/already-migrated DB) — fine
        # PRAGMA user_version does not accept a bound parameter
        await self._conn.execute(f"PRAGMA user_version = {_AGENT_DB_SCHEMA_VERSION}")
        log.info("AgentDB schema migrated to version %d", _AGENT_DB_SCHEMA_VERSION)

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
    # iPad log forwarding
    # ---------------------------------------------------------------------- #

    async def log_ipad_events(
        self,
        session_id: int,
        entries: list,
        trace_id: Optional[str] = None,
    ) -> None:
        """Persist a batch of structured log entries forwarded from the iPad app.

        Each entry is a dict with keys: ts (float), level (str), subsystem (str), msg (str).
        trace_id correlates entries to the PC-side command that triggered them (iPad↔PC
        trace correlation gap). Silently no-ops if DB is unavailable or entries is empty.
        """
        if not self._conn or not entries:
            return
        rows = [
            (
                session_id,
                float(e.get("ts", time.time())),
                str(e.get("level", "info"))[:16],
                str(e.get("subsystem", "unknown"))[:64],
                str(e.get("msg", ""))[:2048],
                trace_id,
            )
            for e in entries
            if isinstance(e, dict)
        ]
        if not rows:
            return
        await self._conn.executemany(
            "INSERT INTO ipad_logs (session_id, ts, level, subsystem, msg, trace_id)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        await self._conn.commit()

    # ---------------------------------------------------------------------- #
    # Sensor telemetry (1 Hz ambient snapshots)
    # ---------------------------------------------------------------------- #

    async def insert_sensor_telemetry(
        self,
        session_id: int,
        ts: float,
        *,
        tilt_rx: Optional[float] = None,
        tilt_ry: Optional[float] = None,
        gaze_dx: Optional[float] = None,
        gaze_dy: Optional[float] = None,
        gaze_conf: Optional[float] = None,
        head_pitch: Optional[float] = None,
        head_yaw: Optional[float] = None,
        cursor_x: Optional[int] = None,
        cursor_y: Optional[int] = None,
        pain_day_active: bool = False,
        active_source: Optional[str] = None,
        gesture_conf: Optional[float] = None,
        rms_ambient: Optional[float] = None,
    ) -> None:
        """Write one 1-Hz sensor telemetry row. Non-fatal on any error."""
        if not self._conn:
            return
        try:
            await self._conn.execute(
                """INSERT INTO sensor_telemetry
                   (session_id, ts, tilt_rx, tilt_ry, gaze_dx, gaze_dy, gaze_conf,
                    head_pitch, head_yaw, cursor_x, cursor_y,
                    pain_day_active, active_source, gesture_conf, rms_ambient)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    session_id, ts,
                    tilt_rx, tilt_ry,
                    gaze_dx, gaze_dy, gaze_conf,
                    head_pitch, head_yaw,
                    cursor_x, cursor_y,
                    int(pain_day_active),
                    active_source,
                    gesture_conf, rms_ambient,
                ),
            )
            await self._conn.commit()
        except Exception as exc:
            log.debug("insert_sensor_telemetry failed (non-fatal): %s", exc)

    # ---------------------------------------------------------------------- #
    # Session summaries
    # ---------------------------------------------------------------------- #

    async def insert_session_summary(self, summary: dict) -> None:
        """Upsert a session summary row (keyed on session_id)."""
        if not self._conn:
            return
        try:
            await self._conn.execute(
                """INSERT OR REPLACE INTO session_summaries
                   (session_id, ts, duration_s, total_commands, success_rate,
                    cloud_escalation_rate, gate0_blocks, gate1_blocks,
                    gate2_blocks, gate3_blocks, gate4_blocks,
                    latency_p50_ms, latency_p95_ms, pain_day_pct,
                    corrections_count, avg_whisper_logprob, avg_gesture_conf,
                    source_breakdown, domain_breakdown, top_actions)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    summary.get("session_id"),
                    summary.get("ts", time.time()),
                    summary.get("duration_s"),
                    summary.get("total_commands", 0),
                    summary.get("success_rate"),
                    summary.get("cloud_escalation_rate"),
                    summary.get("gate0_blocks", 0),
                    summary.get("gate1_blocks", 0),
                    summary.get("gate2_blocks", 0),
                    summary.get("gate3_blocks", 0),
                    summary.get("gate4_blocks", 0),
                    summary.get("latency_p50_ms"),
                    summary.get("latency_p95_ms"),
                    summary.get("pain_day_pct"),
                    summary.get("corrections_count", 0),
                    summary.get("avg_whisper_logprob"),
                    summary.get("avg_gesture_conf"),
                    summary.get("source_breakdown"),
                    summary.get("domain_breakdown"),
                    summary.get("top_actions"),
                ),
            )
            await self._conn.commit()
        except Exception as exc:
            log.debug("insert_session_summary failed (non-fatal): %s", exc)

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
        trace_id: Optional[str] = None,
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
                    gaze_x, gaze_y, success, error_msg, trace_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    session_id, time.time(), cmd.source, cmd.text,
                    action, json.dumps(
                        {k: v for k, v in cmd.params.items()
                         if not isinstance(v, (bytes, bytearray))}
                    ) if cmd.params else None,
                    route, gate_that_decided,
                    round(latency_ms, 1) if latency_ms is not None else None,
                    cmd.whisper_logprob, cmd.gesture_confidence,
                    gaze_x, gaze_y, success_int, error_msg, trace_id,
                ),
            )
            await self._conn.commit()
            return cur.lastrowid  # type: ignore[return-value]
        except Exception as exc:
            log.warning("AgentDB.insert_command failed: %s", exc)
            return -1

    # ── Voice calibration (Sprint A) ──────────────────────────────────────

    async def insert_voice_calibration(
        self,
        session_id: int,
        phrase: str,
        actual_text: str,
        rms_amplitude: float,
        freq_centroid: float,
        avg_logprob: float,
        duration_s: float,
        is_flare_day: bool = False,
    ) -> None:
        if not self._conn:
            return
        try:
            await self._conn.execute(
                """INSERT INTO voice_calibration
                   (session_id, ts, phrase, actual_text, rms_amplitude,
                    freq_centroid, avg_logprob, duration_s, is_flare_day)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (session_id, time.time(), phrase, actual_text, rms_amplitude,
                 freq_centroid, avg_logprob, duration_s, int(is_flare_day)),
            )
            await self._conn.commit()
        except Exception as exc:
            log.warning("AgentDB.insert_voice_calibration failed: %s", exc)

    async def upsert_voice_profile(self, profile: dict) -> None:
        """Insert or replace the single-row voice profile."""
        if not self._conn:
            return
        try:
            existing = await (await self._conn.execute(
                "SELECT id FROM voice_profile ORDER BY id LIMIT 1"
            )).fetchone()
            if existing:
                await self._conn.execute(
                    """UPDATE voice_profile SET
                       updated_at=?, baseline_rms=?, baseline_logprob=?,
                       baseline_freq=?, flare_rms_scale=?, vad_threshold=?,
                       logprob_floor=?, sample_count=?
                       WHERE id=?""",
                    (time.time(), profile.get("baseline_rms"),
                     profile.get("baseline_logprob"), profile.get("baseline_freq"),
                     profile.get("flare_rms_scale", 0.5),
                     profile.get("vad_threshold"), profile.get("logprob_floor"),
                     profile.get("sample_count", 0), existing[0]),
                )
            else:
                await self._conn.execute(
                    """INSERT INTO voice_profile
                       (updated_at, baseline_rms, baseline_logprob, baseline_freq,
                        flare_rms_scale, vad_threshold, logprob_floor, sample_count)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (time.time(), profile.get("baseline_rms"),
                     profile.get("baseline_logprob"), profile.get("baseline_freq"),
                     profile.get("flare_rms_scale", 0.5),
                     profile.get("vad_threshold"), profile.get("logprob_floor"),
                     profile.get("sample_count", 0)),
                )
            await self._conn.commit()
        except Exception as exc:
            log.warning("AgentDB.upsert_voice_profile failed: %s", exc)

    async def get_voice_profile(self) -> dict | None:
        """Return the current voice profile row, or None if not yet calibrated."""
        if not self._conn:
            return None
        try:
            row = await (await self._conn.execute(
                """SELECT baseline_rms, baseline_logprob, baseline_freq,
                          flare_rms_scale, vad_threshold, logprob_floor, sample_count
                   FROM voice_profile ORDER BY id LIMIT 1"""
            )).fetchone()
            if not row:
                return None
            return {
                "baseline_rms": row[0], "baseline_logprob": row[1],
                "baseline_freq": row[2], "flare_rms_scale": row[3],
                "vad_threshold": row[4], "logprob_floor": row[5],
                "sample_count": row[6],
            }
        except Exception as exc:
            log.warning("AgentDB.get_voice_profile failed: %s", exc)
            return None

    async def get_voice_calibration_samples(
        self, is_flare_day: bool | None = None, limit: int = 200
    ) -> list[dict]:
        """Return recent voice calibration samples, optionally filtered by flare state."""
        if not self._conn:
            return []
        try:
            if is_flare_day is None:
                rows = await (await self._conn.execute(
                    """SELECT rms_amplitude, freq_centroid, avg_logprob, duration_s, is_flare_day
                       FROM voice_calibration ORDER BY ts DESC LIMIT ?""", (limit,)
                )).fetchall()
            else:
                rows = await (await self._conn.execute(
                    """SELECT rms_amplitude, freq_centroid, avg_logprob, duration_s, is_flare_day
                       FROM voice_calibration WHERE is_flare_day=?
                       ORDER BY ts DESC LIMIT ?""", (int(is_flare_day), limit)
                )).fetchall()
            return [
                {"rms": r[0], "freq": r[1], "logprob": r[2],
                 "duration_s": r[3], "flare": bool(r[4])}
                for r in rows if r[0] is not None
            ]
        except Exception as exc:
            log.warning("AgentDB.get_voice_calibration_samples failed: %s", exc)
            return []

    async def get_flare_profile(self) -> dict | None:
        if not self._conn:
            return None
        try:
            row = await (await self._conn.execute(
                """SELECT voice_degrades, gesture_degrades, gaze_degrades,
                          tilt_degrades, flare_vad_scale, manual_pain_day, notes,
                          sound_degrades
                   FROM flare_profile ORDER BY id DESC LIMIT 1"""
            )).fetchone()
            if not row:
                return None
            return {
                "voice_degrades": bool(row[0]), "gesture_degrades": bool(row[1]),
                "gaze_degrades": bool(row[2]), "tilt_degrades": bool(row[3]),
                "flare_vad_scale": row[4], "manual_pain_day": bool(row[5]),
                "notes": row[6], "sound_degrades": bool(row[7]),
            }
        except Exception as exc:
            log.warning("AgentDB.get_flare_profile failed: %s", exc)
            return None

    async def upsert_flare_profile(self, flags: dict) -> None:
        """Persist the user's flare degrade profile from the iPad FlareProfileSheet.

        Updates the most recent flare_profile row (preserving manual_pain_day),
        or inserts a new one. `flags` may contain any of: voice_degrades,
        gesture_degrades, gaze_degrades, tilt_degrades, sound_degrades,
        flare_vad_scale.
        """
        if not self._conn:
            return
        cols = ("voice_degrades", "gesture_degrades", "gaze_degrades",
                "tilt_degrades", "sound_degrades", "flare_vad_scale")
        try:
            existing = await (await self._conn.execute(
                "SELECT id FROM flare_profile ORDER BY id DESC LIMIT 1"
            )).fetchone()
            present = [(c, flags[c]) for c in cols if c in flags]
            if not present:
                return
            if existing:
                set_clause = ", ".join(f"{c}=?" for c, _ in present)
                params = [
                    int(v) if c != "flare_vad_scale" else float(v)
                    for c, v in present
                ]
                await self._conn.execute(
                    f"UPDATE flare_profile SET {set_clause}, updated_at=? WHERE id=?",
                    (*params, time.time(), existing[0]),
                )
            else:
                col_names = ", ".join(c for c, _ in present)
                placeholders = ", ".join("?" for _ in present)
                params = [
                    int(v) if c != "flare_vad_scale" else float(v)
                    for c, v in present
                ]
                await self._conn.execute(
                    f"INSERT INTO flare_profile (updated_at, {col_names}) "
                    f"VALUES (?, {placeholders})",
                    (time.time(), *params),
                )
            await self._conn.commit()
            log.info("AgentDB: flare_profile updated — %s", dict(present))
        except Exception as exc:
            log.warning("AgentDB.upsert_flare_profile failed: %s", exc)

    async def set_manual_pain_day(self, active: bool) -> None:
        """User override: force pain_day_active regardless of auto-detection."""
        if not self._conn:
            return
        try:
            existing = await (await self._conn.execute(
                "SELECT id FROM flare_profile ORDER BY id DESC LIMIT 1"
            )).fetchone()
            if existing:
                await self._conn.execute(
                    "UPDATE flare_profile SET manual_pain_day=?, updated_at=? WHERE id=?",
                    (int(active), time.time(), existing[0]),
                )
            else:
                await self._conn.execute(
                    """INSERT INTO flare_profile
                       (updated_at, manual_pain_day, flare_vad_scale)
                       VALUES (?,?,0.5)""",
                    (time.time(), int(active)),
                )
            await self._conn.commit()
            log.info("AgentDB: manual_pain_day set to %s", active)
        except Exception as exc:
            log.warning("AgentDB.set_manual_pain_day failed: %s", exc)

    async def search_lecture_notes(
        self,
        query: str,
        session_id: int | None = None,
        limit: int = 20,
    ) -> list[dict]:
        """Full-text search over ambient_transcripts (lecture mode captures).

        Uses SQLite LIKE for simple substring matching. Returns rows ordered
        by timestamp descending so most recent results come first.

        Args:
            query:      Search term — matched as %query% substring.
            session_id: Restrict to a specific session (None = all sessions).
            limit:      Max rows to return.
        """
        if not self._conn:
            return []
        try:
            like = f"%{query}%"
            if session_id is not None:
                rows = await (await self._conn.execute(
                    """SELECT ts, text, logprob, duration_s
                       FROM ambient_transcripts
                       WHERE session_id = ? AND text LIKE ?
                       ORDER BY ts DESC LIMIT ?""",
                    (session_id, like, limit),
                )).fetchall()
            else:
                rows = await (await self._conn.execute(
                    """SELECT ts, text, logprob, duration_s
                       FROM ambient_transcripts
                       WHERE text LIKE ?
                       ORDER BY ts DESC LIMIT ?""",
                    (like, limit),
                )).fetchall()
            return [
                {"ts": r[0], "text": r[1], "logprob": r[2], "duration_s": r[3]}
                for r in rows
            ]
        except Exception as exc:
            log.warning("AgentDB.search_lecture_notes failed: %s", exc)
            return []

    # ---------------------------------------------------------------------- #
    # Voice calibration
    # ---------------------------------------------------------------------- #

    async def start_calibration_session(self, condition: str, notes: str = "") -> int:
        if not self._conn:
            return -1
        cur = await self._conn.execute(
            "INSERT INTO voice_calibration_sessions (ts, condition, notes) VALUES (?,?,?)",
            (time.time(), condition, notes),
        )
        await self._conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    async def insert_pronunciation(
        self,
        session_id: int,
        expected: str,
        heard: str,
        logprob: float | None = None,
        duration_s: float | None = None,
    ) -> None:
        if not self._conn:
            return
        await self._conn.execute(
            """INSERT INTO voice_pronunciations
               (session_id, ts, expected, heard, logprob, duration_s)
               VALUES (?,?,?,?,?,?)""",
            (session_id, time.time(), expected, heard, logprob, duration_s),
        )
        await self._conn.commit()

    async def save_voice_profile(
        self,
        condition: str,
        corrections: dict,
        vad_threshold: float,
        logprob_floor: float,
        initial_prompt: str | None = None,
    ) -> None:
        if not self._conn:
            return
        await self._conn.execute(
            """INSERT INTO voice_profiles
               (condition, corrections_json, vad_threshold, logprob_floor,
                initial_prompt, updated_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(condition) DO UPDATE SET
                 corrections_json=excluded.corrections_json,
                 vad_threshold=excluded.vad_threshold,
                 logprob_floor=excluded.logprob_floor,
                 initial_prompt=excluded.initial_prompt,
                 updated_at=excluded.updated_at""",
            (condition, json.dumps(corrections), vad_threshold,
             logprob_floor, initial_prompt, time.time()),
        )
        await self._conn.commit()

    async def load_voice_profile(self, condition: str) -> dict | None:
        if not self._conn:
            return None
        cur = await self._conn.execute(
            "SELECT corrections_json, vad_threshold, logprob_floor, initial_prompt "
            "FROM voice_profiles WHERE condition=?",
            (condition,),
        )
        row = await cur.fetchone()
        if not row:
            return None
        return {
            "corrections": json.loads(row[0]),
            "vad_threshold": row[1],
            "logprob_floor": row[2],
            "initial_prompt": row[3],
        }

    async def get_all_pronunciations(self, condition: str) -> list[dict]:
        """Return all accepted pronunciations for a condition across all sessions."""
        if not self._conn:
            return []
        cur = await self._conn.execute(
            """SELECT vp.expected, vp.heard, vp.logprob, vp.duration_s
               FROM voice_pronunciations vp
               JOIN voice_calibration_sessions vcs ON vp.session_id = vcs.id
               WHERE vcs.condition=? AND vp.accepted=1
               ORDER BY vp.ts DESC""",
            (condition,),
        )
        rows = await cur.fetchall()
        return [{"expected": r[0], "heard": r[1], "logprob": r[2], "duration_s": r[3]}
                for r in rows]

    async def insert_ambient_transcript(
        self,
        session_id: int,
        text: str,
        logprob: float | None = None,
        duration_s: float | None = None,
    ) -> None:
        """Store a transcription that was heard but not routed as a command.

        Captures lecture audio, background conversation, etc. so it can be
        searched or reviewed later via DevAgent ("search my lecture notes").
        """
        if not self._conn:
            return
        try:
            await self._conn.execute(
                """INSERT INTO ambient_transcripts (session_id, ts, text, logprob, duration_s)
                   VALUES (?, ?, ?, ?, ?)""",
                (session_id, time.time(), text, logprob, duration_s),
            )
            await self._conn.commit()
        except Exception as exc:
            log.warning("AgentDB.insert_ambient_transcript failed: %s", exc)

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
                   (command_id, ts, model, domain, prompt_hash, prompt, response,
                    tokens_in, tokens_out, latency_ms, backend, error)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    command_id if (command_id and command_id > 0) else None,
                    time.time(), model, domain, prompt_hash, prompt, response,
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
        compensation_action: Optional[str] = None,
        compensation_args: Optional[str] = None,
    ) -> Optional[int]:
        """Insert a step record. Returns the new row id (for saga compensation wiring), or None."""
        if not self._conn or run_id < 0:
            return None
        try:
            async with self._conn.execute(
                """INSERT INTO agent_steps
                   (run_id, step_num, action, args, body, result, success, latency_ms,
                    compensation_action, compensation_args)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id, step_num, action, args or None, body or None,
                    result or None,
                    None if success is None else int(success),
                    round(latency_ms, 1),
                    compensation_action,
                    compensation_args,
                ),
            ) as cur:
                row_id = cur.lastrowid
            await self._conn.commit()
            return row_id
        except Exception as exc:
            log.warning("AgentDB.insert_agent_step failed: %s", exc)
            return None

    # ---------------------------------------------------------------------- #
    # Agent run lifecycle (durable, resumable plan ledger)
    # ---------------------------------------------------------------------- #

    async def start_agent_run(
        self,
        goal: str,
        domain: str = "plan",
        model_used: Optional[str] = None,
        command_id: Optional[int] = None,
    ) -> int:
        """Insert a run row with status='running' and return its id.

        Steps are appended via insert_agent_step as they complete; the run is
        finalised with update_agent_run. A row left 'running' (process crash) is
        reconciled to 'interrupted' on next startup by mark_interrupted_runs().
        """
        if not self._conn:
            return -1
        try:
            cur = await self._conn.execute(
                """INSERT INTO agent_runs
                   (command_id, ts, goal, domain, model_used, step_count,
                    success, total_latency_ms, error, status)
                   VALUES (?,?,?,?,?,0,NULL,NULL,NULL,'running')""",
                (
                    command_id if (command_id and command_id > 0) else None,
                    time.time(), goal, domain, model_used,
                ),
            )
            await self._conn.commit()
            return cur.lastrowid  # type: ignore[return-value]
        except Exception as exc:
            log.warning("AgentDB.start_agent_run failed: %s", exc)
            return -1

    async def update_agent_run(
        self,
        run_id: int,
        status: str,
        step_count: int,
        success: bool,
        total_latency_ms: float,
        error: Optional[str] = None,
    ) -> None:
        """Finalise a run with a terminal status ('completed'/'failed'/'cancelled')."""
        if not self._conn or run_id < 0:
            return
        try:
            await self._conn.execute(
                """UPDATE agent_runs
                   SET status=?, step_count=?, success=?, total_latency_ms=?, error=?
                   WHERE id=?""",
                (status, step_count, int(success), round(total_latency_ms, 1), error, run_id),
            )
            await self._conn.commit()
        except Exception as exc:
            log.warning("AgentDB.update_agent_run failed: %s", exc)

    async def mark_interrupted_runs(self) -> int:
        """Reconcile orphaned 'running' rows to 'interrupted'. Returns the count.

        Called once at startup: any run still 'running' means the process died
        mid-plan. Returns how many rows were reconciled so the caller can offer
        a resume.
        """
        if not self._conn:
            return 0
        try:
            cur = await self._conn.execute(
                "UPDATE agent_runs SET status='interrupted' WHERE status='running'"
            )
            await self._conn.commit()
            return cur.rowcount if cur.rowcount is not None else 0
        except Exception as exc:
            log.warning("AgentDB.mark_interrupted_runs failed: %s", exc)
            return 0

    async def get_interrupted_runs(self, limit: int = 10) -> list[dict]:
        """Return recent interrupted runs (most recent first) for resume offers."""
        if not self._conn:
            return []
        try:
            cur = await self._conn.execute(
                """SELECT id, goal, domain, model_used, step_count, ts
                   FROM agent_runs WHERE status='interrupted'
                   ORDER BY ts DESC LIMIT ?""",
                (limit,),
            )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]
        except Exception as exc:
            log.warning("AgentDB.get_interrupted_runs failed: %s", exc)
            return []

    # ---------------------------------------------------------------------- #
    # Durable goal backlog (gap D)
    # ---------------------------------------------------------------------- #

    async def enqueue_goal(
        self,
        goal: str,
        domain: str = "plan",
        idempotency_key: Optional[str] = None,
        max_attempts: int = 3,
    ) -> int:
        """Persist a goal to the durable backlog. Returns its row id, or -1.

        idempotency_key is UNIQUE: re-enqueuing the same key (e.g. crash recovery)
        is a no-op that returns the existing row id, so a goal can never be
        double-queued.
        """
        if not self._conn:
            return -1
        try:
            import time
            await self._conn.execute(
                """INSERT OR IGNORE INTO goal_queue
                   (ts, goal, domain, status, idempotency_key, max_attempts)
                   VALUES (?, ?, ?, 'queued', ?, ?)""",
                (time.time(), goal, domain, idempotency_key, max_attempts),
            )
            await self._conn.commit()
            if idempotency_key is not None:
                cur = await self._conn.execute(
                    "SELECT id FROM goal_queue WHERE idempotency_key=?",
                    (idempotency_key,),
                )
                row = await cur.fetchone()
                return int(row["id"]) if row else -1
            cur = await self._conn.execute("SELECT last_insert_rowid() AS id")
            row = await cur.fetchone()
            return int(row["id"]) if row else -1
        except Exception as exc:
            log.warning("AgentDB.enqueue_goal failed: %s", exc)
            return -1

    async def claim_next_goal(self) -> Optional[dict]:
        """Atomically claim the oldest queued goal → 'running'. Returns its dict
        (with attempts incremented), or None if the queue is empty.

        Single-consumer (the drainer), so SELECT-then-guarded-UPDATE is race-safe
        without RETURNING; the UPDATE's `status='queued'` guard is belt-and-braces.
        """
        if not self._conn:
            return None
        try:
            cur = await self._conn.execute(
                "SELECT * FROM goal_queue WHERE status='queued' ORDER BY ts LIMIT 1"
            )
            row = await cur.fetchone()
            if row is None:
                return None
            gid = int(row["id"])
            upd = await self._conn.execute(
                "UPDATE goal_queue SET status='running', attempts=attempts+1 "
                "WHERE id=? AND status='queued'",
                (gid,),
            )
            await self._conn.commit()
            if (upd.rowcount or 0) == 0:
                return None   # lost a race (shouldn't happen single-consumer)
            d = dict(row)
            d["status"] = "running"
            d["attempts"] = int(d.get("attempts", 0)) + 1
            return d
        except Exception as exc:
            log.warning("AgentDB.claim_next_goal failed: %s", exc)
            return None

    async def complete_goal(
        self,
        goal_id: int,
        status: str,
        error: Optional[str] = None,
        run_id: Optional[int] = None,
    ) -> None:
        """Mark a claimed goal terminal: 'done' / 'failed' / 'cancelled'."""
        if not self._conn:
            return
        try:
            await self._conn.execute(
                "UPDATE goal_queue SET status=?, last_error=?, run_id=COALESCE(?, run_id) "
                "WHERE id=?",
                (status, error, run_id, goal_id),
            )
            await self._conn.commit()
        except Exception as exc:
            log.warning("AgentDB.complete_goal failed: %s", exc)

    async def requeue_stale_running(self) -> int:
        """Startup recovery: a goal left 'running' means the process died mid-goal.

        Requeue it (attempts already counted the failed try) when under
        max_attempts; otherwise mark it 'failed' so a poison goal can't loop
        forever. Returns the number requeued.
        """
        if not self._conn:
            return 0
        try:
            await self._conn.execute(
                "UPDATE goal_queue SET status='failed', "
                "last_error='exceeded max_attempts after crash' "
                "WHERE status='running' AND attempts >= max_attempts"
            )
            cur = await self._conn.execute(
                "UPDATE goal_queue SET status='queued' "
                "WHERE status='running' AND attempts < max_attempts"
            )
            await self._conn.commit()
            return cur.rowcount if cur.rowcount is not None else 0
        except Exception as exc:
            log.warning("AgentDB.requeue_stale_running failed: %s", exc)
            return 0

    async def get_queued_goals(self, limit: int = 50) -> list[dict]:
        """Return queued goals (oldest first) for status queries / draining."""
        if not self._conn:
            return []
        try:
            cur = await self._conn.execute(
                "SELECT id, goal, domain, attempts, max_attempts, ts "
                "FROM goal_queue WHERE status='queued' ORDER BY ts LIMIT ?",
                (limit,),
            )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]
        except Exception as exc:
            log.warning("AgentDB.get_queued_goals failed: %s", exc)
            return []

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

    async def upsert_few_shot_counterexample(
        self,
        cmd: "Command",
        wrong_action: str,
        domain: str = "command",
        reason: str = "pipeline_failure",
        command_id: Optional[int] = None,
    ) -> None:
        if not self._conn:
            return
        now = time.time()
        try:
            await self._conn.execute(
                """INSERT INTO few_shot_counterexamples
                   (command_id, text, wrong_action, reason, source, domain, ts, usage_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                   ON CONFLICT(text, wrong_action) DO UPDATE
                     SET usage_count = usage_count + 1, ts = excluded.ts""",
                (
                    command_id if (command_id and command_id > 0) else None,
                    cmd.text, wrong_action, reason, cmd.source, domain, now,
                ),
            )
            await self._conn.commit()
        except Exception as exc:
            log.warning("AgentDB.upsert_few_shot_counterexample failed: %s", exc)
            return
        try:
            encoder = await _get_encoder()
            if encoder is None:
                return
            emb = await asyncio.to_thread(_encode_sync, cmd.text, encoder)
            await self._conn.execute(
                """UPDATE few_shot_counterexamples SET embedding = ?
                   WHERE text = ? AND wrong_action = ? AND embedding IS NULL""",
                (emb, cmd.text, wrong_action),
            )
            await self._conn.commit()
        except Exception as exc:
            log.debug("Counterexample embedding update failed (non-fatal): %s", exc)

    async def get_few_shot_counterexamples(
        self,
        cmd: "Command",
        n: int = 3,
        domain: str = "command",
    ) -> list[dict]:
        """Return up to n counterexamples ranked by (cosine | Jaccard) × recency × usage."""
        if not self._conn:
            return []
        try:
            now = time.time()
            query_tokens = _tokens(cmd.text)
            encoder = await _get_encoder()
            query_emb: Optional[bytes] = None
            if encoder is not None:
                try:
                    query_emb = await asyncio.to_thread(_encode_sync, cmd.text, encoder)
                except Exception:
                    pass
            async with self._conn.execute(
                """SELECT text, wrong_action, ts, usage_count, embedding
                   FROM few_shot_counterexamples
                   WHERE domain = ?
                   ORDER BY ts DESC LIMIT 500""",
                (domain,),
            ) as cur:
                rows = [dict(r) for r in await cur.fetchall()]

            def _cscore(row: dict) -> float:
                recency = _recency_weight(row["ts"], now)
                usage   = math.log1p(row["usage_count"])
                if query_emb is not None and row.get("embedding"):
                    sim = _cosine(query_emb, row["embedding"])
                else:
                    sim = _jaccard(query_tokens, _tokens(row["text"]))
                return sim * recency * usage

            scored = sorted(rows, key=_cscore, reverse=True)
            return [
                {"command_text": r["text"], "wrong_action": r["wrong_action"]}
                for r in scored[:n]
                if _cscore(r) > 0.0
            ]
        except Exception as exc:
            log.warning("AgentDB.get_few_shot_counterexamples failed: %s", exc)
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
        self, gesture: str, default: Optional[float] = None
    ) -> Optional[float]:
        """Latest calibrated velocity floor for `gesture`, or `default` when no
        calibration row exists. The default is None — NOT a numeric guess:
        swipe floors are ~1.2 normalized coords/s but push/pull floors are
        ~0.30 m/s (different units), so a shared numeric default poisons the
        other gesture class (audit 2026-06-09: the old 1.2 default made
        push/pull/snap gestures 4x harder on every uncalibrated startup,
        permanently, because below-threshold motions are never sampled)."""
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
    # Maintenance / pruning (called at startup or on a schedule)
    # ---------------------------------------------------------------------- #

    async def prune_sensor_telemetry(self, days: int = 7) -> int:
        """Delete sensor_telemetry rows older than `days`. Returns rows deleted.

        At 1 Hz write rate, 7 days = ~604,800 rows (~30–50 MB). Call at startup
        to keep the DB from growing unboundedly across long-uptime deployments.
        """
        if not self._conn:
            return 0
        cutoff = time.time() - days * 86400
        try:
            async with self._conn.execute(
                "DELETE FROM sensor_telemetry WHERE ts < ?", (cutoff,)
            ) as cur:
                deleted = cur.rowcount or 0
            await self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            await self._conn.commit()
            if deleted:
                log.info("AgentDB: pruned %d sensor_telemetry rows (> %d days)", deleted, days)
            return deleted
        except Exception as exc:
            log.warning("AgentDB.prune_sensor_telemetry failed: %s", exc)
            return 0

    async def prune_gesture_velocity_samples(self, days: int = 90) -> int:
        """Delete gesture_velocity_samples rows older than `days`. Returns rows deleted.

        At ~7,200/day, 90 days = ~648,000 rows. Retaining 90 days preserves enough
        signal for ContinuousTrainer's p10 velocity-floor calibration.
        """
        if not self._conn:
            return 0
        cutoff = time.time() - days * 86400
        try:
            async with self._conn.execute(
                "DELETE FROM gesture_velocity_samples WHERE ts < ?", (cutoff,)
            ) as cur:
                deleted = cur.rowcount or 0
            await self._conn.commit()
            if deleted:
                log.info(
                    "AgentDB: pruned %d gesture_velocity_samples rows (> %d days)", deleted, days
                )
            return deleted
        except Exception as exc:
            log.warning("AgentDB.prune_gesture_velocity_samples failed: %s", exc)
            return 0

    async def prune_ipad_logs(self, days: int = 60) -> int:
        """Delete ipad_logs rows older than `days`. Returns rows deleted."""
        if not self._conn:
            return 0
        cutoff = time.time() - days * 86400
        try:
            async with self._conn.execute(
                "DELETE FROM ipad_logs WHERE ts < ?", (cutoff,)
            ) as cur:
                deleted = cur.rowcount or 0
            await self._conn.commit()
            if deleted:
                log.info("AgentDB: pruned %d ipad_logs rows (> %d days)", deleted, days)
            return deleted
        except Exception as exc:
            log.warning("AgentDB.prune_ipad_logs failed: %s", exc)
            return 0

    async def prune_event_log(self, days: int = 7) -> int:
        """Delete event_log rows older than `days`. Returns rows deleted."""
        if not self._conn:
            return 0
        cutoff = time.time() - days * 86400
        try:
            async with self._conn.execute(
                "DELETE FROM event_log WHERE ts < ?", (cutoff,)
            ) as cur:
                deleted = cur.rowcount or 0
            await self._conn.commit()
            if deleted:
                log.info("AgentDB: pruned %d event_log rows (> %d days)", deleted, days)
            return deleted
        except Exception as exc:
            log.warning("AgentDB.prune_event_log failed: %s", exc)
            return 0

    async def prune_tool_calls(self, days: int = 30) -> int:
        """Delete tool_calls rows older than `days`. Returns rows deleted."""
        if not self._conn:
            return 0
        cutoff = time.time() - days * 86400
        try:
            async with self._conn.execute(
                "DELETE FROM tool_calls WHERE ts < ?", (cutoff,)
            ) as cur:
                deleted = cur.rowcount or 0
            await self._conn.commit()
            if deleted:
                log.info("AgentDB: pruned %d tool_calls rows (> %d days)", deleted, days)
            return deleted
        except Exception as exc:
            log.warning("AgentDB.prune_tool_calls failed: %s", exc)
            return 0

    async def prune_rate_limit_events(self, days: int = 7) -> int:
        """Delete rate_limit_events rows older than `days`. Returns rows deleted."""
        if not self._conn:
            return 0
        cutoff = time.time() - days * 86400
        try:
            async with self._conn.execute(
                "DELETE FROM rate_limit_events WHERE ts < ?", (cutoff,)
            ) as cur:
                deleted = cur.rowcount or 0
            await self._conn.commit()
            if deleted:
                log.info("AgentDB: pruned %d rate_limit_events rows (> %d days)", deleted, days)
            return deleted
        except Exception as exc:
            log.warning("AgentDB.prune_rate_limit_events failed: %s", exc)
            return 0

    # ---------------------------------------------------------------------- #
    # Event bus
    # ---------------------------------------------------------------------- #

    async def insert_event(
        self,
        topic: str,
        payload: str,
        source: str,
        *,
        session_id: Optional[int] = None,
        command_id: Optional[int] = None,
        trace_id: Optional[str] = None,
    ) -> Optional[int]:
        """Append one event to event_log. Returns the new row id, or None on error."""
        if not self._conn:
            return None
        try:
            async with self._conn.execute(
                "INSERT INTO event_log (ts, topic, session_id, command_id, trace_id, source, payload)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (time.time(), topic, session_id, command_id, trace_id, source, payload),
            ) as cur:
                row_id = cur.lastrowid
            await self._conn.commit()
            return row_id
        except Exception as exc:
            log.warning("AgentDB.insert_event failed: %s", exc)
            return None

    async def poll_events(
        self,
        topic_pattern: str,
        last_event_id: int,
        limit: int = 100,
    ) -> list[dict]:
        """Return up to `limit` events matching `topic_pattern` (SQL LIKE) with id > last_event_id."""
        if not self._conn:
            return []
        try:
            async with self._conn.execute(
                "SELECT id, ts, topic, session_id, command_id, trace_id, source, payload"
                " FROM event_log WHERE topic LIKE ? AND id > ? ORDER BY id LIMIT ?",
                (topic_pattern, last_event_id, limit),
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]
        except Exception as exc:
            log.warning("AgentDB.poll_events failed: %s", exc)
            return []

    async def upsert_event_consumer(self, consumer_name: str, topic_pattern: str) -> None:
        """Register a consumer; no-op if already registered."""
        if not self._conn:
            return
        try:
            await self._conn.execute(
                "INSERT OR IGNORE INTO event_consumers"
                " (consumer_name, topic_pattern, last_event_id, updated_at)"
                " VALUES (?, ?, 0, ?)",
                (consumer_name, topic_pattern, time.time()),
            )
            await self._conn.commit()
        except Exception as exc:
            log.warning("AgentDB.upsert_event_consumer failed: %s", exc)

    async def update_consumer_cursor(self, consumer_name: str, last_event_id: int) -> None:
        """Advance a consumer's cursor after processing events."""
        if not self._conn:
            return
        try:
            await self._conn.execute(
                "UPDATE event_consumers SET last_event_id = ?, updated_at = ?"
                " WHERE consumer_name = ?",
                (last_event_id, time.time(), consumer_name),
            )
            await self._conn.commit()
        except Exception as exc:
            log.warning("AgentDB.update_consumer_cursor failed: %s", exc)

    # ---------------------------------------------------------------------- #
    # Saga compensations
    # ---------------------------------------------------------------------- #

    async def insert_saga_compensation(
        self,
        run_id: int,
        step_id: int,
        compensation_action: str,
        compensation_args: Optional[str] = None,
    ) -> Optional[int]:
        """Register a compensation for a completed step. Returns new row id."""
        if not self._conn:
            return None
        try:
            async with self._conn.execute(
                "INSERT INTO saga_compensations"
                " (ts, run_id, step_id, compensation_action, compensation_args)"
                " VALUES (?, ?, ?, ?, ?)",
                (time.time(), run_id, step_id, compensation_action, compensation_args),
            ) as cur:
                row_id = cur.lastrowid
            await self._conn.commit()
            return row_id
        except Exception as exc:
            log.warning("AgentDB.insert_saga_compensation failed: %s", exc)
            return None

    async def update_saga_compensation(
        self,
        compensation_id: int,
        status: str,
        *,
        triggered_by: Optional[str] = None,
        error: Optional[str] = None,
        finished: bool = False,
    ) -> None:
        """Update the status of a saga compensation row."""
        if not self._conn:
            return
        now = time.time()
        try:
            await self._conn.execute(
                "UPDATE saga_compensations"
                " SET status = ?, triggered_by = COALESCE(?, triggered_by),"
                "     started_at = CASE WHEN started_at IS NULL THEN ? ELSE started_at END,"
                "     finished_at = CASE WHEN ? THEN ? ELSE finished_at END,"
                "     error = COALESCE(?, error)"
                " WHERE id = ?",
                (
                    status,
                    triggered_by,
                    now,
                    finished, now if finished else None,
                    error,
                    compensation_id,
                ),
            )
            await self._conn.commit()
        except Exception as exc:
            log.warning("AgentDB.update_saga_compensation failed: %s", exc)

    async def get_pending_compensations(self, run_id: int) -> list[dict]:
        """Return pending compensations for run_id in reverse step order (highest step_num first)."""
        if not self._conn:
            return []
        try:
            async with self._conn.execute(
                "SELECT sc.id, sc.step_id, sc.compensation_action, sc.compensation_args,"
                "       as2.step_num"
                " FROM saga_compensations sc"
                " JOIN agent_steps as2 ON as2.id = sc.step_id"
                " WHERE sc.run_id = ? AND sc.status = 'pending'"
                " ORDER BY as2.step_num DESC",
                (run_id,),
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]
        except Exception as exc:
            log.warning("AgentDB.get_pending_compensations failed: %s", exc)
            return []

    # ---------------------------------------------------------------------- #
    # Per-call tool execution (idempotency + timeout)
    # ---------------------------------------------------------------------- #

    async def insert_tool_call(
        self,
        tool_name: str,
        *,
        command_id: Optional[int] = None,
        run_id: Optional[int] = None,
        step_id: Optional[int] = None,
        idempotency_key: Optional[str] = None,
        args_json: Optional[str] = None,
        result_json: Optional[str] = None,
        success: Optional[bool] = None,
        latency_ms: Optional[float] = None,
        timeout_ms: Optional[int] = None,
        status: str = "completed",
    ) -> Optional[int]:
        """Record a tool call. Returns row id, or None if duplicate idempotency_key."""
        if not self._conn:
            return None
        try:
            async with self._conn.execute(
                "INSERT OR IGNORE INTO tool_calls"
                " (ts, command_id, run_id, step_id, tool_name, idempotency_key,"
                "  args_json, result_json, success, latency_ms, timeout_ms, status)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    time.time(), command_id, run_id, step_id, tool_name,
                    idempotency_key, args_json, result_json,
                    int(success) if success is not None else None,
                    latency_ms, timeout_ms, status,
                ),
            ) as cur:
                row_id = cur.lastrowid if cur.rowcount else None
            await self._conn.commit()
            return row_id
        except Exception as exc:
            log.warning("AgentDB.insert_tool_call failed: %s", exc)
            return None

    async def get_tool_call_by_idempotency(self, idempotency_key: str) -> Optional[dict]:
        """Return a completed tool_call row for the given key, or None if not found."""
        if not self._conn:
            return None
        try:
            async with self._conn.execute(
                "SELECT * FROM tool_calls WHERE idempotency_key = ? AND status = 'completed' LIMIT 1",
                (idempotency_key,),
            ) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None
        except Exception as exc:
            log.warning("AgentDB.get_tool_call_by_idempotency failed: %s", exc)
            return None

    # ---------------------------------------------------------------------- #
    # Config reads (tool timeout, cache TTL, rate limits)
    # ---------------------------------------------------------------------- #

    async def get_tool_timeout(self, tool_name: str) -> tuple[int, int]:
        """Return (timeout_ms, max_retries) for tool_name, or (15000, 0) if not configured."""
        if not self._conn:
            return (15_000, 0)
        try:
            async with self._conn.execute(
                "SELECT timeout_ms, max_retries FROM tool_timeout_config WHERE tool_name = ?",
                (tool_name,),
            ) as cur:
                row = await cur.fetchone()
                return (row["timeout_ms"], row["max_retries"]) if row else (15_000, 0)
        except Exception as exc:
            log.warning("AgentDB.get_tool_timeout failed: %s", exc)
            return (15_000, 0)

    async def get_cache_config(self, tool_name: str) -> tuple[float, int]:
        """Return (ttl_s, max_entries) for tool_name, or hardcoded fallback if not configured."""
        _FALLBACKS = {"vision_grounder": (2.0, 200), "ui_automation": (1.0, 200), "target_cache": (1.5, 500)}
        if not self._conn:
            return _FALLBACKS.get(tool_name, (2.0, 200))
        try:
            async with self._conn.execute(
                "SELECT ttl_s, max_entries FROM tool_cache_config WHERE tool_name = ?",
                (tool_name,),
            ) as cur:
                row = await cur.fetchone()
                return (row["ttl_s"], row["max_entries"]) if row else _FALLBACKS.get(tool_name, (2.0, 200))
        except Exception as exc:
            log.warning("AgentDB.get_cache_config failed: %s", exc)
            return _FALLBACKS.get(tool_name, (2.0, 200))

    async def get_rate_limit_config(self, resource: str) -> tuple[float, int]:
        """Return (max_rps, burst_capacity) for resource, or (2.0, 1) if not configured."""
        if not self._conn:
            return (2.0, 1)
        try:
            async with self._conn.execute(
                "SELECT max_rps, burst_capacity FROM rate_limit_config WHERE resource = ?",
                (resource,),
            ) as cur:
                row = await cur.fetchone()
                return (row["max_rps"], row["burst_capacity"]) if row else (2.0, 1)
        except Exception as exc:
            log.warning("AgentDB.get_rate_limit_config failed: %s", exc)
            return (2.0, 1)

    async def insert_rate_limit_event(
        self,
        resource: str,
        *,
        command_id: Optional[int] = None,
        wait_ms: float = 0.0,
        was_dropped: bool = False,
    ) -> None:
        """Record a rate-limit breach for observability. Non-fatal on error."""
        if not self._conn:
            return
        try:
            await self._conn.execute(
                "INSERT INTO rate_limit_events (ts, resource, command_id, wait_ms, was_dropped)"
                " VALUES (?, ?, ?, ?, ?)",
                (time.time(), resource, command_id, wait_ms, int(was_dropped)),
            )
            await self._conn.commit()
        except Exception as exc:
            log.debug("AgentDB.insert_rate_limit_event failed (non-fatal): %s", exc)

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
    # D4: sensor_rom read (onboarding ROM bounds → runtime calibration)
    # ---------------------------------------------------------------------- #

    async def get_sensor_rom(self, sensor: str) -> dict[str, dict]:
        """Return the most recent range-of-motion row per direction for a sensor.

        Returns dict keyed by direction, each value is
        {max_value, comfortable_value, unit}.
        """
        if not self._conn:
            return {}
        try:
            async with self._conn.execute(
                """SELECT direction, max_value, comfortable_value, unit
                   FROM sensor_rom
                   WHERE sensor = ?
                   GROUP BY direction
                   HAVING ts = MAX(ts)""",
                (sensor,),
            ) as cur:
                rows = await cur.fetchall()
            return {
                r["direction"]: {
                    "max_value": r["max_value"],
                    "comfortable_value": r["comfortable_value"],
                    "unit": r["unit"],
                }
                for r in rows
                if r["direction"] is not None
            }
        except Exception as exc:
            log.warning("AgentDB.get_sensor_rom failed: %s", exc)
            return {}

    # ---------------------------------------------------------------------- #
    # D5: Adaptation effectiveness log
    # ---------------------------------------------------------------------- #

    async def log_adaptation(
        self,
        component: str,
        metric_before: float,
        metric_after: float,
        cloud_rate: float = 0.0,
        failure_rate: float = 0.0,
        domain: Optional[str] = None,
    ) -> int:
        """Insert one adaptation_log row. Returns the new row id.

        `domain` tags per-domain SLO adaptations (gap H); None for global ones.
        """
        if not self._conn:
            return -1
        try:
            cur = await self._conn.execute(
                """INSERT INTO adaptation_log
                   (ts, component, metric_before, metric_after, cloud_rate, failure_rate, domain)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (time.time(), component, metric_before, metric_after,
                 cloud_rate, failure_rate, domain),
            )
            await self._conn.commit()
            return cur.lastrowid  # type: ignore[return-value]
        except Exception as exc:
            log.warning("AgentDB.log_adaptation failed: %s", exc)
            return -1

    async def get_inference_stats_by_domain(self, limit: int = 1000) -> dict:
        """Per-domain rolling stats from the inferences table (gap H).

        Returns {domain: {count, p50_latency_ms, success_rate}} over the most
        recent `limit` inference rows. p50 + success-rate are computed in Python
        (SQLite has no median); success = the inference recorded no error.
        """
        if not self._conn:
            return {}
        try:
            async with self._conn.execute(
                "SELECT domain, latency_ms, error FROM inferences ORDER BY ts DESC LIMIT ?",
                (limit,),
            ) as cur:
                rows = await cur.fetchall()
        except Exception as exc:
            log.warning("AgentDB.get_inference_stats_by_domain failed: %s", exc)
            return {}

        buckets: dict[str, list] = {}
        for r in rows:
            buckets.setdefault(r["domain"], []).append((r["latency_ms"], r["error"]))
        out: dict[str, dict] = {}
        for domain, items in buckets.items():
            lats = sorted(l for l, _ in items if l is not None)
            p50 = lats[len(lats) // 2] if lats else None
            ok = sum(1 for _, e in items if e is None)
            out[domain] = {
                "count": len(items),
                "p50_latency_ms": round(p50, 1) if p50 is not None else None,
                "success_rate": round(ok / len(items), 3) if items else None,
            }
        return out

    async def get_recent_adaptation_log(
        self, component: str, limit: int = 5
    ) -> list[dict]:
        """Return the most recent adaptation_log rows for a component."""
        if not self._conn:
            return []
        try:
            async with self._conn.execute(
                """SELECT id, ts, metric_before, metric_after,
                          cloud_rate, failure_rate, rolled_back
                   FROM adaptation_log
                   WHERE component = ?
                   ORDER BY ts DESC LIMIT ?""",
                (component, limit),
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]
        except Exception as exc:
            log.warning("AgentDB.get_recent_adaptation_log failed: %s", exc)
            return []

    async def mark_adaptation_rolled_back(self, row_id: int) -> None:
        """Set rolled_back=1 for the given adaptation_log row."""
        if not self._conn or row_id < 0:
            return
        try:
            await self._conn.execute(
                "UPDATE adaptation_log SET rolled_back=1 WHERE id=?", (row_id,)
            )
            await self._conn.commit()
        except Exception as exc:
            log.warning("AgentDB.mark_adaptation_rolled_back failed: %s", exc)

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
                    # Avoid double-serialization: if the value is already a JSON
                    # string (e.g. from PreferenceModel.to_json()), store as-is.
                    json.dumps(old_value) if (old_value is not None and not isinstance(old_value, str)) else old_value,
                    new_value if isinstance(new_value, str) else json.dumps(new_value),
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
