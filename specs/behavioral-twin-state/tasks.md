# Implementation Plan: Behavioral Twin State

## Overview

Implementation tasks for the `BehavioralTwinState` component — Sprint 3's core deliverable. Covers new files (`behavioral_twin_state.py`, `semantic_memory.py`), AgentDB schema additions (`db.py`), integration changes to `hybrid_coordinator.py` and `continuous_trainer.py`, `main.py` wiring, and the full test suite.

## Task Dependency Graph

```json
{
  "waves": [
    { "wave": 1, "tasks": ["1"], "description": "AgentDB schema and query methods — foundation for all other tasks" },
    { "wave": 2, "tasks": ["2"], "description": "TwinSnapshot and PreferenceModel dataclasses — required by core class and tests" },
    { "wave": 3, "tasks": ["3"], "description": "SemanticMemory ChromaDB wrapper — depends on AgentDB fallback from wave 1" },
    { "wave": 4, "tasks": ["4"], "description": "BehavioralTwinState core class — depends on waves 1, 2, 3" },
    { "wave": 5, "tasks": ["5", "6"], "description": "HybridCoordinator and ContinuousTrainer integration — depend on wave 4" },
    { "wave": 6, "tasks": ["7"], "description": "main.py wiring — depends on waves 5 and 6" },
    { "wave": 7, "tasks": ["8", "9", "10", "11", "12", "13", "14", "15"], "description": "All tests — depend on implementation tasks being complete" }
  ]
}
```

## Tasks

- [x] 1. AgentDB schema additions and new query methods
  - [x] 1.1 Add `twin_session_history` and `twin_pain_day_log` tables to `db.py` (CREATE TABLE IF NOT EXISTS, with indexes)
  - [x] 1.2 Implement `get_recent_successful_commands(limit=500)` on `AgentDB`
  - [x] 1.3 Implement `get_session_commands(session_id, limit=20)` on `AgentDB`
  - [x] 1.4 Implement `get_most_recent_session_id(exclude_session_id=None)` on `AgentDB`
  - [x] 1.5 Implement `get_command_stats_last_n_days(days=30)` on `AgentDB`
  - [x] 1.6 Implement `get_source_stats_last_n_days(days=7)` on `AgentDB`
  - [x] 1.7 Implement `write_session_history(session_id, history)` on `AgentDB`
  - [x] 1.8 Implement `read_session_history(session_id, limit=20)` on `AgentDB`
  - [x] 1.9 Implement `log_pain_day(session_id, score, active, fail_ratio, clarify_ratio, gesture_conf_delta, cmd_rate_delta)` on `AgentDB`
  - [x] 1.10 Implement `get_preference_model_snapshot()` on `AgentDB` (reads from `settings_versions` where `component='preference_model'`)

- [x] 2. `TwinSnapshot` dataclass and `PreferenceModel`
  - [x] 2.1 Create `behavioral_twin_state.py` with `TwinSnapshot` frozen dataclass (all 7 fields per Req 5.2) and `_DEFAULT_SNAPSHOT` constant
  - [x] 2.2 Implement `ActionStats` dataclass with Welford accumulator fields
  - [x] 2.3 Implement `PreferenceModel` dataclass with `action_stats`, `time_buckets`, and `source_counts`
  - [x] 2.4 Implement `PreferenceModel.update(action_verb, confidence, latency_ms, source, ts, success)`
  - [x] 2.5 Implement `PreferenceModel.top_actions(time_bucket, n=5) -> list[str]`
  - [x] 2.6 Implement `PreferenceModel.source_weights() -> dict[str, float]` (success_count / total_count per source, clamped to [0.0, 1.0])
  - [x] 2.7 Implement `PreferenceModel.time_bucket(ts) -> int` static method (floor(hour / 6), returns 0–3)
  - [x] 2.8 Implement `PreferenceModel.to_json()` and `PreferenceModel.from_json(data)` for AgentDB round-trip

- [x] 3. `SemanticMemory` — ChromaDB wrapper with AgentDB fallback
  - [x] 3.1 Create `semantic_memory.py` with `SemanticMemory` class skeleton (constants: `COLLECTION_NAME`, `CHROMA_DIR`, `CAP=10_000`, `EVICT_COUNT=1_000`)
  - [x] 3.2 Implement `SemanticMemory.start() -> bool` — initialize ChromaDB client, create/open `"behavioral_memory"` collection, create `./chroma_db/` if absent; return `True` if available
  - [x] 3.3 Implement `SemanticMemory.add(text, action, source, ts, success, doc_id=None)` — store document with deterministic SHA-256 id; schedule eviction if cap reached
  - [x] 3.4 Implement `SemanticMemory.query_similar(text, n=5) -> list[dict]` — ChromaDB cosine path (success=True filter) with AgentDB Jaccard fallback
  - [x] 3.5 Implement `SemanticMemory.count() -> int` — returns 0 if ChromaDB unavailable
  - [x] 3.6 Implement `SemanticMemory._evict_oldest()` — remove `EVICT_COUNT` oldest documents by `ts` metadata
  - [x] 3.7 Wrap all ChromaDB calls in try/except; set `_available=False` on any failure; ensure `sentence-transformers` absence falls back to ChromaDB built-in embedding

- [x] 4. `BehavioralTwinState` — core class
  - [x] 4.1 Implement `BehavioralTwinState.__init__` with all constants (`WORKING_SET_SIZE=500`, `SESSION_HISTORY_MAX=20`, `SESSION_HISTORY_PAIN_DAY=10`, thresholds, `STARTUP_TIMEOUT_S=5.0`) and internal state fields
  - [x] 4.2 Implement `BehavioralTwinState.start()` — concurrent `asyncio.gather(_init_agent_db, _init_chroma)` with 5-second timeout; set `is_ready=True` in `finally`; start `_pain_day_loop` task
  - [x] 4.3 Implement `BehavioralTwinState.stop()` — cancel `_pain_day_loop`, flush all pending tasks via `asyncio.gather`, persist final preference model, write session history, close connections
  - [x] 4.4 Implement `is_ready` property
  - [x] 4.5 Implement `BehavioralTwinState.get_snapshot() -> TwinSnapshot` — O(1) read of `_current_snapshot`; return `_DEFAULT_SNAPSHOT` if not ready or on error; never raises
  - [x] 4.6 Implement `BehavioralTwinState.observe(cmd, action_str)` — schedule `_persist_observation` as independent background `asyncio.Task`; add to `_pending_tasks` set with done-callback discard; never raises
  - [x] 4.7 Implement `BehavioralTwinState.query_similar(text, n=5) -> list[dict]` — delegates to `SemanticMemory.query_similar`; never raises
  - [x] 4.8 Implement `BehavioralTwinState.get_session_context() -> list[str]` — returns current `SessionHistory` as list of command text strings
  - [x] 4.9 Implement `BehavioralTwinState._persist_observation(cmd, action_str)` — update `PreferenceModel`, append to `SessionHistory` (respecting pain-day window size), add to `SemanticMemory`, persist preference model to AgentDB; rebuild `_current_snapshot`
  - [x] 4.10 Implement `BehavioralTwinState._load_working_set()` — load 500 most recent successful commands from AgentDB on startup
  - [x] 4.11 Implement `BehavioralTwinState._load_preference_model()` — reconstruct `PreferenceModel` from `settings_versions` JSON snapshot in AgentDB
  - [x] 4.12 Implement `BehavioralTwinState._load_session_history()` — populate `SessionHistory` from most recent prior session (including abnormally terminated sessions with no `ended_at`)
  - [x] 4.13 Implement `BehavioralTwinState._recompute_pain_day_score() -> float` — weighted average of 4 signals (0.35 fail_ratio + 0.25 clarify_ratio + 0.20 gesture_conf_drop + 0.20 cmd_rate_drop), clamped to [0.0, 1.0]; guard: < 5 commands → no activation from session signals
  - [x] 4.14 Implement `BehavioralTwinState._pain_day_loop()` — background task recomputing score every 60 seconds; apply hysteresis (activate > 0.6, deactivate < 0.4); rebuild `_current_snapshot` on state change

- [x] 5. `HybridCoordinator` integration
  - [x] 5.1 Add `twin_state: Optional[BehavioralTwinState] = None` parameter to `HybridCoordinator.__init__`
  - [x] 5.2 In `HybridCoordinator.route()`, call `twin_state.get_snapshot()` before gate evaluation; log WARNING and use `_DEFAULT_SNAPSHOT` if unavailable
  - [x] 5.3 Implement `_apply_pain_day_adjustments(cfg, snapshot) -> CoordinatorConfig` helper — non-mutating `dataclasses.replace` lowering `whisper_logprob_min` by 0.15 and `gesture_confidence_min` by 0.10
  - [x] 5.4 Apply pain-day config adjustment in `route()` when `snapshot.pain_day_active` is True; restore original config when `pain_day_active` becomes False
  - [x] 5.5 Populate `cmd.session_context` from `twin_state.get_session_context()` in `route()`; fall back to coordinator's own context list if twin unavailable

- [x] 6. `ContinuousTrainer` integration
  - [x] 6.1 Add `twin_state: Optional[BehavioralTwinState] = None` parameter to `ContinuousTrainer.__init__`
  - [x] 6.2 In `ContinuousTrainer.record_success()`, call `twin_state.observe(cmd, action_str)` after existing `upsert_few_shot_example` call
  - [x] 6.3 In `ContinuousTrainer._adapt()`, call `twin_state.get_snapshot()` and pass `pain_day_active` to `_adapt_gate1_threshold()` and `_update_gesture_calibration()`
  - [x] 6.4 In `_adapt_gate1_threshold()`, add pain day guard: skip tightening (return early) when `pain_day_active=True`; relaxation is still permitted
  - [x] 6.5 In `_update_gesture_calibration()`, apply additional `0.05` floor reduction when `pain_day_active=True`

- [x] 7. `main.py` wiring
  - [x] 7.1 Instantiate `BehavioralTwinState(agent_db=agent_db)` after `AgentDB` is opened and session is created
  - [x] 7.2 Call `await twin_state.start()` before constructing `ContinuousTrainer` and `HybridCoordinator`
  - [x] 7.3 Pass `twin_state=twin_state` to both `ContinuousTrainer` and `HybridCoordinator` constructors
  - [x] 7.4 Call `await twin_state.stop()` in the shutdown sequence before `trainer.stop()` and `agent_db.close()`

- [x] 8. Property-based tests — `test_preference_model.py`
  - [x] 8.1 Write property test for Property 4: PreferenceModel statistics are correct (frequency, mean_confidence, mean_latency after k observe calls)
  - [x] 8.2 Write property test for Property 5: time_bucket is deterministic and exhaustive (all hours 0–23 map to exactly one bucket in {0,1,2,3})
  - [x] 8.3 Write property test for Property 6: source_weights equal success ratio (clamped to [0.0, 1.0])
  - [x] 8.4 Write property test for Property 7: PreferenceModel survives restart round-trip (to_json → from_json produces equivalent model)

- [x] 9. Property-based tests — `test_pain_day_engine.py`
  - [x] 9.1 Write property test for Property 10: pain_day_score is always in [0.0, 1.0] for any combination of signal inputs including extreme values
  - [x] 9.2 Write property test for Property 11: PainDayMode hysteresis is correct (activate > 0.6, deactivate < 0.4, no activation with < 5 commands from session signals)

- [x] 10. Property-based tests — `test_semantic_memory.py`
  - [x] 10.1 Write property test for Property 1: round-trip semantic identity (store t, query_similar(t,1) returns t as top result)
  - [x] 10.2 Write property test for Property 2: query result count bounded by min(n, C_success)
  - [x] 10.3 Write property test for Property 16: query_similar filters to success=True only
  - [x] 10.4 Write property test for Property 17: SemanticMemory count bounded by CAP; after eviction count = CAP - EVICT_COUNT + new_insertions

- [ ] 11. Property-based tests — `test_behavioral_twin_state.py`
  - [x] 11.1 Write property test for Property 3: working set size is min(N, 500) after start()
  - [ ] 11.2 Write property test for Property 8: SessionHistory is a bounded window (min(N,20) normal, min(N,10) pain day) in chronological order
  - [ ] 11.3 Write property test for Property 9: cross-session context round-trip (prior session commands loaded on new instance start, including abnormal termination)
  - [ ] 11.4 Write property test for Property 12: TwinSnapshot is immutable (FrozenInstanceError on set) and all required fields present with correct types
  - [ ] 11.5 Write property test for Property 14: observe() returns before persistence completes (non-blocking)
  - [ ] 11.6 Write property test for Property 18: stop() flushes all pending tasks (all N observe() background tasks complete after stop() returns)
  - [ ] 11.7 Write property test for Property 19: default snapshot returned before start() completes
  - [ ] 11.8 Write property test for Property 20: public methods never raise for any input (None, empty string, 10k-char string, error-inducing inputs)

- [ ] 12. Property-based tests — `test_trainer_twin_integration.py`
  - [ ] 12.1 Write property test for Property 13: Gate 1 tightening suppressed when pain_day_active=True
  - [ ] 12.2 Write property test for Property 15: gesture floor reduction is exactly 0.05 more on pain days (or f if f < 0.05)

- [ ] 13. Unit tests — `test_behavioral_twin_state.py` (unit section)
  - [ ] 13.1 Test ChromaDB timeout behavior: ChromaDB init exceeding 5s → startup completes in AgentDB-only mode, `is_ready=True`
  - [ ] 13.2 Test `asyncio.gather` concurrency in `start()`: both AgentDB and ChromaDB init run concurrently
  - [ ] 13.3 Test `stop()` flush ordering: pending observe() tasks complete before connections close
  - [ ] 13.4 Test `HybridCoordinator` threshold restoration after pain day deactivates (score drops below 0.4)
  - [ ] 13.5 Test empty DB on first run: start() succeeds with empty working set and default PreferenceModel
  - [ ] 13.6 Test `get_snapshot()` latency: completes in < 50 ms under normal conditions
  - [ ] 13.7 Test `start()` completes within 5 seconds

- [ ] 14. Unit tests — `test_semantic_memory.py` (unit section)
  - [ ] 14.1 Test ChromaDB unavailable at startup: SemanticMemory.start() returns False, add() is a no-op, query_similar() falls back to AgentDB Jaccard
  - [ ] 14.2 Test ChromaDB fails mid-session: `_available` set to False, subsequent calls use fallback without interrupting pipeline
  - [ ] 14.3 Test `./chroma_db/` directory created automatically if absent
  - [ ] 14.4 Test eviction: after CAP insertions, oldest EVICT_COUNT documents are removed

- [ ] 15. Unit tests — integration wiring
  - [ ] 15.1 Test full startup sequence with real AgentDB (in-memory SQLite `:memory:`), ChromaDB mocked
  - [ ] 15.2 Test `HybridCoordinator.route()` calls `get_snapshot()` and attaches snapshot to routing context
  - [ ] 15.3 Test `ContinuousTrainer._adapt()` with pain_day_active=True: verify no Gate 1 tightening occurs
  - [ ] 15.4 Test session close → restart → cross-session context loaded correctly


## Notes

- All new Python files go in `e:\Personal_Desktop_Agent\` (project root), not in a subdirectory.
- Test files go in `e:\Personal_Desktop_Agent\tests\`.
- PBT library is **Hypothesis** (`pip install hypothesis`). Minimum `max_examples=100` per property.
- Tag each property test with `# Feature: behavioral-twin-state, Property N: <property_text>` and `# Validates: Requirements X.Y`.
- ChromaDB is an optional dependency — all tests that exercise `SemanticMemory` must mock or skip ChromaDB when unavailable; use `pytest.importorskip("chromadb")` where appropriate.
- AgentDB tests use in-memory SQLite (`:memory:`) — no file I/O required.
- Tasks 8–12 are PBT tasks. Run with `pytest tests/test_*.py --hypothesis-seed=0` to reproduce failures.
- Tasks 13–15 are unit/integration tests. Run with `pytest tests/`.
- Do not modify existing tables in `db.py` — only add new tables and new methods.
- `TwinSnapshot` must remain a `frozen=True` dataclass throughout — do not relax this constraint.
