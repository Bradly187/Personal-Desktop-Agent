from typing import Tuple

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
    trace_id           TEXT,         -- cross-layer trace id (DA_TRACE); links to monitoring/trace.py spans
    resolved_by        TEXT          -- CLICK resolver tier (explicit|uia|vision|gaze|cursor)
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

-- Multi-agent orchestration ledger (specs/workflow-orchestration). One row per
-- WorkflowRunner.run(): a fan-out / pipeline of fresh-context sub-agent inference
-- calls over the resident model (no new VRAM — AGENTS.md #6). Additive,
-- backward-compatible; experimental + OFF by default, so the table is empty
-- until workflow_orchestration.enabled is set. Status records the terminal
-- outcome (completed | skipped_flare | disabled | error).
CREATE TABLE IF NOT EXISTS agent_workflows (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             REAL    NOT NULL,
    name           TEXT    NOT NULL,
    goal           TEXT,
    mode           TEXT    NOT NULL DEFAULT 'fan_out',   -- fan_out | pipeline
    subtask_count  INTEGER NOT NULL DEFAULT 0,
    success_count  INTEGER NOT NULL DEFAULT 0,
    verified_count INTEGER,                              -- NULL = no verify pass
    status         TEXT    NOT NULL DEFAULT 'completed',
    latency_ms     REAL,
    error          TEXT
);

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
    run_id          INTEGER REFERENCES agent_runs(id),
    -- N+2 proactivity: a future-dated goal is enqueued with status='scheduled'
    -- and execute_at set; the ProactiveScheduler promotes it to 'queued' when due
    -- (claim_next_goal only sees 'queued', so the hot drain path is untouched).
    execute_at      REAL,                  -- NULL = run ASAP; else promote when now >= execute_at
    recurrence      TEXT,                  -- NULL = one-shot; else JSON rule (daily/interval)
    source_trigger  TEXT,                  -- provenance: manual | schedule | event_rule:<id>
    -- Claim lease (audit O #9): owner_pid + claimed_at stamp the process that
    -- claimed a 'running' goal so requeue_stale_running can recover only goals
    -- whose owner is dead/ours and never clobber a concurrent instance's claim.
    owner_pid       INTEGER,
    claimed_at      REAL
);
CREATE INDEX IF NOT EXISTS idx_goalq_status ON goal_queue(status, ts);
-- NOTE: the index on goal_queue(execute_at) is created in AgentDB.open() AFTER
-- _migrate(), not here. execute_at is an additive migration column (v5→6); on a
-- DB created before it existed this script's CREATE TABLE IF NOT EXISTS is a
-- no-op, so building the index here would fail with "no such column: execute_at"
-- before the migration can add it. See _DEFERRED_INDEXES.

-- ── Event rules — event-triggered automation (N+2) ───────────────────────────
-- "when <topic matches + predicate>, notify me / run a goal". Consumed by
-- core/event_rule_engine.py off the EventBus.
CREATE TABLE IF NOT EXISTS event_rules (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      REAL    NOT NULL,
    enabled         INTEGER NOT NULL DEFAULT 1,
    name            TEXT,
    topic_pattern   TEXT    NOT NULL,           -- SQL-LIKE, same semantics as EventBus
    predicate       TEXT,                       -- JSON [{path,op,value}] AND-ed; NULL = any match
    goal_template   TEXT    NOT NULL,           -- rendered with the event payload
    action_kind     TEXT    NOT NULL DEFAULT 'notify',  -- 'notify' | 'enqueue_goal'
    cooldown_s      REAL    NOT NULL DEFAULT 0, -- min seconds between fires (anti-storm)
    last_fired_at   REAL,
    fire_count      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_event_rules_enabled ON event_rules(enabled);

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

-- Episodic / archival memory (R-2): MemGPT-style "how the user solved X under
-- physical state Y" notes. Synthesized locally (zero egress) by memory_compactor
-- from agent_runs/agent_steps or written directly via MemoryManager.write_memory_note.
-- Recall is cosine (when embedding present) else Jaccard × recency — mirrors
-- few_shot_examples. The SQLite row is the durable source of truth; `embedding`
-- is the recall index. pain_day_active/score tag the physical state at capture.
CREATE TABLE IF NOT EXISTS episodic_memory (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                REAL    NOT NULL,
    kind              TEXT    NOT NULL DEFAULT 'note',   -- 'correction' | 'recovery' | 'note'
    goal              TEXT    NOT NULL,
    summary           TEXT    NOT NULL,
    source_run_id     INTEGER REFERENCES agent_runs(id),
    source_command_id INTEGER REFERENCES commands(id),
    domain            TEXT    NOT NULL DEFAULT 'general',
    pain_day_active   INTEGER NOT NULL DEFAULT 0,
    pain_day_score    REAL    NOT NULL DEFAULT 0.0,
    embedding         BLOB,
    salience          REAL    NOT NULL DEFAULT 1.0,
    usage_count       INTEGER NOT NULL DEFAULT 0,
    last_recalled_ts  REAL
);
CREATE INDEX IF NOT EXISTS idx_epm_ts     ON episodic_memory(ts);
CREATE INDEX IF NOT EXISTS idx_epm_kind   ON episodic_memory(kind);
CREATE INDEX IF NOT EXISTS idx_epm_domain ON episodic_memory(domain);

-- Self-evolution candidate staging (R-3): the offline self_evolution pipeline
-- synthesizes few-shot examples/counterexamples from adaptation_log + dev_escalations
-- + corrections and STAGES them here (status='proposed') — never straight into the
-- active few_shot tables. Promotion is human-approved by default; eval-gated
-- auto-promote only when DA_SELF_EVOLVE=1. action_or_wrong holds the target action
-- (kind='example') or the rejected action (kind='counterexample').
CREATE TABLE IF NOT EXISTS self_evolution_candidates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL    NOT NULL,
    kind            TEXT    NOT NULL,            -- 'example' | 'counterexample'
    domain          TEXT    NOT NULL DEFAULT 'command',
    text            TEXT    NOT NULL,
    action_or_wrong TEXT    NOT NULL,
    reason          TEXT,
    source_refs     TEXT,                        -- JSON: {escalation_ids:[], adaptation_ids:[], command_ids:[]}
    eval_delta      REAL,                        -- baseline-lock accuracy delta measured at gate time
    status          TEXT    NOT NULL DEFAULT 'proposed',  -- proposed | promoted | rejected | rolled_back
    decided_ts      REAL,
    UNIQUE(kind, text, action_or_wrong)
);
CREATE INDEX IF NOT EXISTS idx_sec_status ON self_evolution_candidates(status, ts);

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
    condition TEXT    NOT NULL,  -- good_day | flare_day | allergy_day (legacy rows may hold svt_attack)
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
    -- DEAD (legacy): gaze_*/head_* are always NULL since gaze + head-pose removal
    -- (2026-05-30). Kept because the schema is additive-only; no writer populates
    -- them. Do not reintroduce gaze/head pipelines here.
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
    rms_ambient      REAL,
    -- v9: most recent command trace within FusionEngine._TRACE_WINDOW_S at
    -- sample time (NULL when no command ran recently). Same semantics as
    -- ipad_logs.trace_id — joins ambient sensor state to commands.trace_id.
    trace_id         TEXT
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
    compensation_action TEXT    NOT NULL,  -- verb: 'RESTORE_FILE', 'DELETE_FILE', 'REVERT_TERMINAL'
    compensation_args   TEXT,             -- JSON; must be fully self-contained
    status              TEXT    NOT NULL DEFAULT 'pending',  -- pending/running/done/failed/skipped
    triggered_by        TEXT,             -- 'step_failure'|'user_cancel'|'max_replans'|'max_steps'
    started_at          REAL,
    finished_at         REAL,
    error               TEXT
);
CREATE INDEX IF NOT EXISTS idx_saga_run  ON saga_compensations(run_id);
CREATE INDEX IF NOT EXISTS idx_saga_step ON saga_compensations(step_id);

-- ── Dev-plan escalation queue — human-review backlog (R-10 residual) ──────────
-- When a dev plan exhausts MAX_REPLANS or hits MAX_STEPS, the run is rolled back
-- (saga above) and the failed goal lands HERE for review instead of evaporating.
-- A user cancel is deliberate and is NOT escalated. Lifecycle: pending →
-- acknowledged (reviewed) / dismissed. Never auto-requeued — re-running a goal
-- that failed twice needs a human decision.
CREATE TABLE IF NOT EXISTS dev_escalations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL    NOT NULL,
    run_id        INTEGER REFERENCES agent_runs(id),
    goal          TEXT    NOT NULL,
    reason        TEXT    NOT NULL,   -- 'max_replans' | 'max_steps'
    failed_action TEXT,               -- verb of the step that exhausted the budget
    replans       INTEGER NOT NULL DEFAULT 0,
    detail        TEXT,               -- JSON: executed-step summary for review
    status        TEXT    NOT NULL DEFAULT 'pending',  -- pending/acknowledged/dismissed
    resolved_ts   REAL
);
CREATE INDEX IF NOT EXISTS idx_escalations_status ON dev_escalations(status, ts);

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

-- ── Skill invocations — audit log for MCP-client skill calls (N+1) ───────────
-- Every SKILL_QUERY/SKILL_CALL: which skill/tool, send vs read, status, whether
-- a HIGH-risk taint verdict blocked the result, and a scrubbed result summary.
CREATE TABLE IF NOT EXISTS skill_invocations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL    NOT NULL,
    skill_id        TEXT    NOT NULL,
    tool_name       TEXT    NOT NULL,
    send            INTEGER NOT NULL DEFAULT 0,
    status          TEXT,
    blocked         INTEGER NOT NULL DEFAULT 0,
    result_summary  TEXT
);
CREATE INDEX IF NOT EXISTS idx_skillinv_ts ON skill_invocations(ts);

-- Learned per-domain keyword overlay (E2). A small, bounded additive nudge on top
-- of the static DomainClassifier keyword sets, populated by ContinuousTrainer from
-- confirmed-correct per-domain vocabulary and applied only when DA_DOMAIN_LEARN is
-- on. Plain CREATE TABLE IF NOT EXISTS (appears on existing DBs too) — no
-- migration bump needed, same as skill_invocations.
CREATE TABLE IF NOT EXISTS domain_keyword_weights (
    domain   TEXT NOT NULL,
    keyword  TEXT NOT NULL,
    weight   REAL NOT NULL DEFAULT 0.0,
    ts       REAL NOT NULL,
    PRIMARY KEY (domain, keyword)
);

-- Persisted command traces (GAP-4 — Pillar 6 Observability / eval replay). The
-- in-memory TraceRecorder ring buffer (monitoring/trace.py) holds only the last
-- 200 commands and is lost on restart; completed traces are flushed here
-- (fire-and-forget, off the 60 Hz hot path) so the eval framework can replay a
-- command's trajectory and diagnose session failures. One row per span, ordered
-- by `seq`. Plain CREATE TABLE IF NOT EXISTS — no migration bump (same as
-- skill_invocations / domain_keyword_weights).
CREATE TABLE IF NOT EXISTS command_traces (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id    TEXT    NOT NULL,
    session_id  INTEGER,
    seq         INTEGER NOT NULL,
    stage       TEXT    NOT NULL,
    ts          REAL    NOT NULL,
    dur_ms      REAL,
    attrs_json  TEXT
);
CREATE INDEX IF NOT EXISTS idx_ctrace_trace ON command_traces(trace_id, seq);

-- Intent drift / trust-decay log (GAP-6 — Pillar 6). When a multi-turn dev
-- session's current command diverges from its opening intent below the drift
-- threshold for several consecutive turns, a row is recorded for later review.
-- Plain CREATE TABLE IF NOT EXISTS — no migration bump.
CREATE TABLE IF NOT EXISTS intent_drift_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL    NOT NULL,
    session_id      INTEGER,
    trace_id        TEXT,
    drift_score     REAL    NOT NULL,
    original_intent TEXT,
    current_command TEXT
);
CREATE INDEX IF NOT EXISTS idx_driftlog_session ON intent_drift_log(session_id, ts);

-- Harvested user corrections (GAP-9 — labeled failure data). Every confirmed
-- "no, not like that" correction, persisted so scripts/cluster_corrections.py can
-- cluster them offline into candidate eval cases / systematic failure modes.
-- Plain CREATE TABLE IF NOT EXISTS — no migration bump.
CREATE TABLE IF NOT EXISTS user_corrections (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL    NOT NULL,
    session_id      INTEGER,
    trace_id        TEXT,
    correction_text TEXT    NOT NULL,
    prior_action    TEXT,
    domain          TEXT
);
CREATE INDEX IF NOT EXISTS idx_usercorr_ts ON user_corrections(ts);

-- Graph-Based Memory (Gap 2): Knowledge Graph Layer
CREATE TABLE IF NOT EXISTS knowledge_nodes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE,
    type        TEXT    NOT NULL,
    attributes  TEXT    -- JSON serialized attributes
);

CREATE TABLE IF NOT EXISTS knowledge_edges (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id   INTEGER NOT NULL REFERENCES knowledge_nodes(id),
    target_id   INTEGER NOT NULL REFERENCES knowledge_nodes(id),
    relation    TEXT    NOT NULL,
    weight      REAL    DEFAULT 1.0,
    UNIQUE(source_id, target_id, relation)
);
"""


# Additive column migrations (see AgentDB._migrate). Each entry adds a column to
# a table created before that column existed. Bump _AGENT_DB_SCHEMA_VERSION and
# append a row when introducing a new additive column; the batch is gated by
# PRAGMA user_version so it runs at most once per database file.

_AGENT_DB_SCHEMA_VERSION = 9
_AGENT_DB_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    # v8→9: sensor→command trace correlation (mirrors ipad_logs.trace_id)
    ("sensor_telemetry", "trace_id", "TEXT"),
    # v7→8 (Sprint S3.0)
    ("commands", "resolved_by", "TEXT"),
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
    # v5→6 (N+2: proactive scheduling on goal_queue)
    ("goal_queue", "execute_at",     "REAL"),
    ("goal_queue", "recurrence",     "TEXT"),
    ("goal_queue", "source_trigger", "TEXT"),
    # v6→7 (Sprint O #9: claim lease for crash-safe requeue)
    ("goal_queue", "owner_pid",  "INTEGER"),
    ("goal_queue", "claimed_at", "REAL"),
)

# Indexes on additive-migration columns. These CANNOT live in AGENT_DB_SCHEMA:
# on a DB created before the column existed, CREATE TABLE IF NOT EXISTS is a no-op
# so the column is absent when executescript runs, and the CREATE INDEX fails with
# "no such column". They are created in AgentDB.open() AFTER _migrate() has added
# the columns (fresh DBs already have them from CREATE TABLE). IF NOT EXISTS makes
# this idempotent.
_DEFERRED_INDEXES: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_goalq_sched ON goal_queue(execute_at)",
    "CREATE INDEX IF NOT EXISTS idx_st_trace ON sensor_telemetry(trace_id)",
)





# ---------------------------------------------------------------------------
# Scoring helpers (shared with ContinuousTrainer)
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# AgentDB — SQLite operational store
# ---------------------------------------------------------------------------

