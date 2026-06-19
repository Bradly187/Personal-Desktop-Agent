# Requirements Document

## Introduction

The Behavioral Twin State is Sprint 3's core deliverable. It moves the Personal Desktop Agent from a static, session-isolated system (External/Loose/Static in the Turing taxonomy) to an adaptive, persistent one (Internal/Tight/Adaptive). The component maintains a persistent model of Brad's behavioral patterns — preferred commands, time-of-day usage rhythms, pain-day adaptations, and cross-session context — and exposes that model as a queryable state object that `HybridCoordinator` consults before every routing decision and `ContinuousTrainer` uses to drive adaptation.

The twin state is backed by two complementary stores: ChromaDB (vector embeddings for semantic memory and preference retrieval) running locally on the RTX 5090, and the existing `AgentDB` (aiosqlite SQLite) for structured session and preference data. No cloud dependency is introduced. The system degrades gracefully when ChromaDB is unavailable, falling back to `AgentDB`-only operation.

## Glossary

- **BehavioralTwinState**: The new component introduced by this feature. Maintains Brad's persistent behavioral model and exposes it as a queryable async interface.
- **TwinSnapshot**: An immutable dataclass capturing the current behavioral state at a point in time. Consumed by `HybridCoordinator` and `ContinuousTrainer`.
- **PreferenceModel**: The structured representation of Brad's learned command preferences, time-of-day patterns, and sensor preferences, stored in `AgentDB`.
- **SemanticMemory**: The ChromaDB-backed vector store of past commands and outcomes, enabling semantic similarity retrieval.
- **SessionHistory**: The ordered record of commands executed in prior sessions, persisted across restarts in `AgentDB`.
- **PainDayMode**: An operational mode the `BehavioralTwinState` enters when Brad's usage signals indicate a high-pain or low-energy session. Lowers confidence thresholds and prefers lower-effort interaction paths.
- **AgentDB**: The existing aiosqlite SQLite operational store (`agent.db`), already containing sessions, commands, few-shot examples, and gesture calibration tables.
- **HybridCoordinator**: The existing 4-gate routing engine that decides local vs. cloud inference. Consumes `TwinSnapshot` before gate evaluation.
- **ContinuousTrainer**: The existing background learning system. Feeds new observations into `BehavioralTwinState` and reads adaptation signals from it.
- **Command**: The universal DTO that crosses all pipeline boundaries. No new message types are introduced.
- **ChromaDB**: The local vector database used for semantic memory. Runs on the RTX 5090 machine. Optional dependency — system must function without it.

---

## Requirements

### Requirement 1: Persistent Behavioral Memory

**User Story:** As Brad, I want the agent to remember my past commands and outcomes across restarts, so that it does not start from scratch every session and can immediately apply what it has learned about how I work.

#### Acceptance Criteria

1. THE `BehavioralTwinState` SHALL persist all successfully executed commands to `AgentDB` with their source, action, timestamp, and session identifier.
2. WHEN the `BehavioralTwinState` is initialized, THE `BehavioralTwinState` SHALL load the 500 most recent successful commands from `AgentDB` into an in-memory working set.
3. WHEN a command is persisted and ChromaDB is available, THE `SemanticMemory` SHALL store a vector embedding of the command text alongside its action and outcome metadata.
4. IF ChromaDB is unavailable at startup, THEN THE `BehavioralTwinState` SHALL log a warning and continue operating using `AgentDB`-only retrieval.
5. IF ChromaDB becomes unavailable after startup, THEN THE `BehavioralTwinState` SHALL degrade to `AgentDB`-only retrieval without interrupting the command pipeline.
6. THE `BehavioralTwinState` SHALL expose an async `query_similar(text: str, n: int) -> list[dict]` method that returns the n most semantically similar past commands, using ChromaDB cosine similarity when available and `AgentDB` Jaccard scoring as fallback.
7. FOR ALL command texts `t`, encoding then storing then querying `t` SHALL return `t` as the top result when the collection is non-empty (round-trip semantic identity property).

---

### Requirement 2: Preference Model

**User Story:** As Brad, I want the agent to learn my preferred commands, typical usage times, and sensor preferences over time, so that routing decisions reflect my actual patterns rather than generic defaults.

#### Acceptance Criteria

1. THE `BehavioralTwinState` SHALL maintain a `PreferenceModel` that tracks, per command action verb: frequency count, mean confidence, mean latency, and last-used timestamp.
2. WHEN a command is successfully executed, THE `BehavioralTwinState` SHALL update the `PreferenceModel` entry for that command's action verb within 1 second.
3. THE `BehavioralTwinState` SHALL compute a time-of-day usage distribution (bucketed into 4 six-hour windows: 00–06, 06–12, 12–18, 18–24) from the last 30 days of `AgentDB` command history.
4. THE `BehavioralTwinState` SHALL track per-source preference weights (voice, gesture, touch, gaze, tilt, head) derived from the ratio of successful to total commands per source over the last 7 days.
5. WHEN the `PreferenceModel` is queried, THE `BehavioralTwinState` SHALL return the top 5 most-used action verbs for the current time-of-day window.
6. THE `PreferenceModel` SHALL be persisted to `AgentDB` in the `settings_versions` table on every update, keyed by `component = 'preference_model'`.
7. WHEN the `BehavioralTwinState` is initialized, THE `BehavioralTwinState` SHALL reconstruct the `PreferenceModel` from `AgentDB` history so that preferences survive restarts.

---

### Requirement 3: Session History and Cross-Session Context

**User Story:** As Brad, I want the agent to pick up where I left off after a restart, so that I do not have to re-establish context or repeat commands I already gave in a prior session.

#### Acceptance Criteria

1. THE `BehavioralTwinState` SHALL maintain a `SessionHistory` of the last 20 successfully executed commands within the current session, consistent with the existing `Command.session_context` field.
2. WHEN a new session starts, THE `BehavioralTwinState` SHALL populate `SessionHistory` with the last 20 successful commands from the most recent prior session in `AgentDB`.
3. THE `BehavioralTwinState` SHALL expose a `get_session_context() -> list[str]` method that returns the current `SessionHistory` as a list of command text strings, suitable for direct assignment to `Command.session_context`.
4. WHEN `HybridCoordinator` constructs a `Command`, THE `HybridCoordinator` SHALL call `BehavioralTwinState.get_session_context()` to populate `Command.session_context`; IF `BehavioralTwinState` is unavailable, THEN THE `HybridCoordinator` SHALL fall back to its own context list or other available configuration sources.
5. THE `SessionHistory` SHALL be written to `AgentDB` at session close so that it is available to the next session on restart.
6. IF the prior session ended abnormally (no `ended_at` timestamp), THEN THE `BehavioralTwinState` SHALL still load that session's last 20 commands as cross-session context.

---

### Requirement 4: Pain Day Adaptation

**User Story:** As Brad, I want the agent to automatically detect when I am having a high-pain or low-energy day and adapt its behavior accordingly, so that I do not have to manually reconfigure the system when my condition makes precise interaction harder.

#### Acceptance Criteria

1. THE `BehavioralTwinState` SHALL compute a `pain_day_score` (float in [0.0, 1.0]) from the following signals in the current session: ratio of failed commands to total commands, ratio of CLARIFY responses to total responses, mean gesture confidence relative to the 30-day baseline, and session command rate relative to the 30-day baseline.
2. WHEN `pain_day_score` exceeds 0.6, THE `BehavioralTwinState` SHALL set `PainDayMode` to active.
3. WHEN `PainDayMode` is active, THE `BehavioralTwinState` SHALL include a `pain_day_active: True` flag in the `TwinSnapshot` returned to `HybridCoordinator`.
4. WHEN `HybridCoordinator` receives a `TwinSnapshot` with `pain_day_active: True`, THE `HybridCoordinator` SHALL lower the Gate 1 `whisper_logprob_min` threshold by 0.15 and the `gesture_confidence_min` threshold by 0.10 for the duration of that session.
5. WHEN `PainDayMode` is active, THE `BehavioralTwinState` SHALL increase the `session_context` window from 20 to 10 commands (shorter window reduces cognitive load on the LLM prompt).
6. WHEN `pain_day_score` drops below 0.4 within the same session, THE `BehavioralTwinState` SHALL deactivate `PainDayMode` and restore original thresholds.
7. THE `pain_day_score` SHALL be recomputed every 60 seconds during an active session.
8. IF fewer than 5 commands have been executed in the current session, THEN THE `BehavioralTwinState` SHALL not activate `PainDayMode` based on session-derived signals alone; WHERE external signals (such as a manually set flag) are present, THE `BehavioralTwinState` SHALL permit `PainDayMode` activation regardless of session command count.

---

### Requirement 5: TwinSnapshot Interface for HybridCoordinator

**User Story:** As the system, I want `HybridCoordinator` to query the behavioral twin state before each routing decision, so that routing is informed by Brad's current context and learned patterns rather than static configuration alone.

#### Acceptance Criteria

1. THE `BehavioralTwinState` SHALL expose an async `get_snapshot() -> TwinSnapshot` method that returns the current behavioral state.
2. THE `TwinSnapshot` dataclass SHALL contain: `pain_day_active: bool`, `preferred_actions: list[str]` (top 5 for current time window), `source_weights: dict[str, float]`, `session_context: list[str]`, `command_count_today: int`, `pain_day_score: float`, and `snapshot_ts: float`.
3. WHEN `HybridCoordinator.route()` is called, THE `HybridCoordinator` SHALL call `BehavioralTwinState.get_snapshot()` before gate evaluation and attach the snapshot to the routing context.
4. THE `get_snapshot()` method SHALL complete within 50 ms under normal operating conditions so that it does not add measurable latency to the command pipeline.
5. IF `BehavioralTwinState` is not initialized, unavailable, or has partial state or stale data, THEN THE `HybridCoordinator` SHALL log a WARNING and proceed with routing using default configuration values or other available configuration sources (such as cached state or user preferences).
6. THE `TwinSnapshot` SHALL be immutable after creation (frozen dataclass) so that concurrent reads from multiple pipeline components are safe.

---

### Requirement 6: ContinuousTrainer Integration

**User Story:** As the system, I want `ContinuousTrainer` to feed new observations into the behavioral twin state and read adaptation signals from it, so that the twin state and the trainer reinforce each other rather than operating in isolation.

#### Acceptance Criteria

1. WHEN `ContinuousTrainer.record_success()` is called, THE `ContinuousTrainer` SHALL also call `BehavioralTwinState.observe(cmd, action_str)` to update the twin state with the new observation.
2. WHEN `ContinuousTrainer._adapt()` runs its adaptation pass, THE `ContinuousTrainer` SHALL read `TwinSnapshot.pain_day_active` and `TwinSnapshot.source_weights` to inform threshold adaptation decisions.
3. WHEN `TwinSnapshot.pain_day_active` is True during an adaptation pass, THE `ContinuousTrainer` SHALL skip Gate 1 threshold tightening (relaxation is permitted; tightening is suppressed).
4. THE `BehavioralTwinState.observe()` method SHALL be non-blocking — it SHALL schedule persistence as a background asyncio task and return immediately; multiple concurrent persistence tasks are permitted, with each `observe()` call scheduling its own background task independently of any previously scheduled tasks.
5. WHEN `ContinuousTrainer` computes gesture confidence floors, THE `ContinuousTrainer` SHALL use `TwinSnapshot.pain_day_active` to apply a 0.05 additional floor reduction on pain days.

---

### Requirement 7: ChromaDB Semantic Memory Store

**User Story:** As the system, I want semantic memory backed by ChromaDB so that the agent can retrieve contextually relevant past commands by meaning rather than exact keyword match, improving few-shot example quality for the LLM.

#### Acceptance Criteria

1. THE `SemanticMemory` SHALL use a local ChromaDB instance with a persistent directory at `./chroma_db/` relative to the project root, with no network calls.
2. THE `SemanticMemory` SHALL maintain a single collection named `"behavioral_memory"` containing documents of the form `{text: command_text, action: action_str, source: source, ts: timestamp, success: bool}`.
3. WHEN a command is stored in `SemanticMemory`, THE `SemanticMemory` SHALL use ChromaDB's default embedding function (all-MiniLM-L6-v2 via chromadb's built-in) unless a custom encoder is injected at construction time.
4. WHEN `query_similar(text, n)` is called, THE `SemanticMemory` SHALL return results ranked by cosine distance, filtered to `success=True` documents only.
5. THE `SemanticMemory` collection SHALL be capped at 10,000 documents; WHEN the cap is reached, THE `SemanticMemory` SHALL evict the oldest 1,000 documents by timestamp.
6. IF the `chroma_db/` directory does not exist at startup, THEN THE `SemanticMemory` SHALL create it automatically.
7. THE `SemanticMemory` SHALL expose a `count() -> int` method returning the current document count, usable for health checks and eviction logic.

---

### Requirement 8: Graceful Degradation and Lifecycle

**User Story:** As Brad, I want the behavioral twin state to start, stop, and fail gracefully so that a ChromaDB crash or slow initialization never blocks the command pipeline.

#### Acceptance Criteria

1. THE `BehavioralTwinState` SHALL implement `start()` and `stop()` async lifecycle methods consistent with the project's component lifecycle convention.
2. WHEN `start()` is called, THE `BehavioralTwinState` SHALL initialize `AgentDB` connections and attempt ChromaDB initialization concurrently using `asyncio.gather`, completing startup within 5 seconds.
3. IF ChromaDB initialization exceeds 5 seconds, THEN THE `BehavioralTwinState` SHALL cancel the ChromaDB init, log a WARNING, and complete startup in `AgentDB`-only mode.
4. WHEN `stop()` is called, THE `BehavioralTwinState` SHALL flush all pending background persistence tasks before closing connections, and the `stop()` method SHALL complete successfully after all tasks are flushed and connections are closed.
5. THE `BehavioralTwinState` SHALL expose an `is_ready: bool` property that is `False` until `start()` completes successfully and `True` thereafter.
6. WHEN `get_snapshot()` is called before `start()` completes, THE `BehavioralTwinState` SHALL return a default `TwinSnapshot` with `pain_day_active=False`, empty `preferred_actions`, and `pain_day_score=0.0`.
7. THE `BehavioralTwinState` SHALL not raise exceptions from `observe()`, `get_snapshot()`, or `query_similar()` — all errors SHALL be caught, logged at WARNING level, and safe defaults returned.
8. WHERE the `sentence-transformers` package is unavailable, THE `SemanticMemory` SHALL fall back to ChromaDB's built-in embedding function without requiring manual configuration.
