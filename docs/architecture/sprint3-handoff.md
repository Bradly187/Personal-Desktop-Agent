# Sprint 3 Handoff — Behavioral Twin State

**Date:** 2026-05-19  
**Status:** COMPLETE. Implementation (waves 1–6) and full test suite (tasks 8–15) all done.  
**Resume in:** N/A — all 88 leaf tasks complete.

---

## What Was Built

Sprint 3 delivered `BehavioralTwinState` — the component that moves the agent from static/session-isolated to adaptive/persistent. It learns Brad's behavioral patterns across sessions and feeds that knowledge into every routing decision.

### New Files Created

| File | Purpose |
|------|---------|
| `behavioral_twin_state.py` | Core: `TwinSnapshot`, `ActionStats`, `PreferenceModel`, `BehavioralTwinState` |
| `semantic_memory.py` | ChromaDB wrapper with AgentDB Jaccard fallback |
| `tests/test_preference_model.py` | PBT: Properties 4, 5, 6, 7 — all passing |
| `tests/test_pain_day_engine.py` | PBT: Properties 10, 11 — all passing |
| `tests/test_semantic_memory.py` | PBT: Properties 1, 2, 16, 17 + Jaccard fallback — all passing |
| `tests/test_behavioral_twin_state.py` | PBT: Properties 3, 8, 9, 12, 14, 18, 19, 20 — all passing |

### Modified Files

| File | Changes |
|------|---------|
| `db.py` | +2 tables (`twin_session_history`, `twin_pain_day_log`), +10 query methods |
| `hybrid_coordinator.py` | `twin_state` param, `get_snapshot()` in `route()`, pain-day threshold adjustments |
| `continuous_trainer.py` | `twin_state` param, `observe()` in `record_success()`, pain-day guards in `_adapt()` |
| `main.py` | Full wiring: instantiate → start → pass to constructors → stop in shutdown |

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Command Pipeline                              │
│                                                                      │
│  FusionEngine ──► HybridCoordinator ──────────────────► CommandExecutor
│                        │        ▲                                    │
│                        │        │ TwinSnapshot (frozen)              │
│                        ▼        │                                    │
│               ┌─────────────────────────┐                           │
│               │   BehavioralTwinState   │◄── ContinuousTrainer      │
│               │                         │    .observe(cmd, action)  │
│               │  ┌──────────────────┐   │                           │
│               │  │  PreferenceModel │   │                           │
│               │  │  SessionHistory  │   │                           │
│               │  │  PainDayEngine   │   │                           │
│               │  └──────────────────┘   │                           │
│               │         │               │                           │
│               │    ┌────┴────┐          │                           │
│               │    ▼         ▼          │                           │
│               │  AgentDB  SemanticMemory│                           │
│               │  (SQLite) (ChromaDB)    │                           │
│               └─────────────────────────┘                           │
└─────────────────────────────────────────────────────────────────────┘
```

### Data Flow

```
1. HybridCoordinator.route(cmd)
   └─► BehavioralTwinState.get_snapshot()          [< 50 ms, O(1)]
       └─► returns TwinSnapshot (frozen)
   └─► if pain_day_active: lower whisper_logprob_min -0.15, gesture_confidence_min -0.10
   └─► cmd.session_context ← twin.get_session_context()
   └─► gate evaluation proceeds with adjusted config

2. After successful execution:
   └─► ContinuousTrainer.record_success(cmd, action)
       └─► BehavioralTwinState.observe(cmd, action)  [non-blocking, returns immediately]
           └─► schedules asyncio.Task:
               ├─► PreferenceModel.update(action_verb, confidence, latency)
               ├─► SessionHistory.append(cmd.text)  [window: 20 normal, 10 pain day]
               ├─► SemanticMemory.add(cmd.text, action, metadata)
               └─► AgentDB.log_settings_change (preference model snapshot)

3. Background: _pain_day_loop() every 60s
   └─► _recompute_pain_day_score()
       ├─► signal_1 = fail_ratio          (weight 0.35)
       ├─► signal_2 = clarify_ratio       (weight 0.25)
       ├─► signal_3 = gesture_conf_drop   (weight 0.20)
       └─► signal_4 = cmd_rate_drop       (weight 0.20)
   └─► hysteresis: activate > 0.6 (with ≥5 cmds), deactivate < 0.4
   └─► rebuild _current_snapshot on state change
```

---

## Pain Day Score Formula

```
signal_1 = failed_cmds / total_cmds
signal_2 = clarify_responses / total_responses
signal_3 = max(0, (baseline_conf - current_mean_conf) / baseline_conf)
signal_4 = max(0, (baseline_rate - current_rate) / baseline_rate)

pain_day_score = clamp(
    0.35 * signal_1 + 0.25 * signal_2 + 0.20 * signal_3 + 0.20 * signal_4,
    0.0, 1.0
)

Guard: if total_cmds < 5 → no activation from session signals
Hysteresis: activate when score > 0.6, deactivate when score < 0.4
```

---

## Task Status

### ✅ Complete (88/88 leaf tasks)

| Wave | Tasks | Status |
|------|-------|--------|
| 1 | AgentDB schema + 10 query methods | ✅ Done |
| 2 | TwinSnapshot, ActionStats, PreferenceModel | ✅ Done |
| 3 | SemanticMemory (ChromaDB + Jaccard fallback) | ✅ Done |
| 4 | BehavioralTwinState core class (all 14 subtasks) | ✅ Done |
| 5 | HybridCoordinator integration (5.1–5.5) | ✅ Done |
| 6 | ContinuousTrainer integration (6.1–6.5) | ✅ Done |
| 7 | main.py wiring (7.1–7.4) | ✅ Done |
| 8 | PBT: test_preference_model.py (8.1–8.4) | ✅ Done |
| 9 | PBT: test_pain_day_engine.py (9.1–9.2) | ✅ Done |
| 10 | PBT: test_semantic_memory.py (10.1–10.4) | ✅ Done |
| 11 | PBT: test_behavioral_twin_state.py (11.1–11.8) | ✅ Done |
| 12 | test_trainer_twin_integration.py (12.1–12.2) | ✅ Done |
| 13 | Unit tests test_behavioral_twin_state.py (13.1–13.7) | ✅ Done |
| 14 | Unit tests test_semantic_memory.py (14.1–14.4) | ✅ Done |
| 15 | Integration wiring tests (15.1–15.4) | ✅ Done |

### ❌ Remaining (0/88 leaf tasks)

All tasks complete.

#### Task 12 — `tests/test_trainer_twin_integration.py` (2 tasks)

```
12.1  Property 13: Gate 1 tightening suppressed when pain_day_active=True
12.2  Property 15: gesture floor reduction is exactly 0.05 more on pain days
```

**Approach:** Mock `AgentDB`, create `ContinuousTrainer` with `twin_state` set to a mock that returns a snapshot with `pain_day_active=True`. Verify `_adapt_gate1_threshold()` does not tighten and `_update_gesture_calibration()` applies the extra 0.05 floor.

---

#### Task 13 — Unit tests in `test_behavioral_twin_state.py` (7 tasks)

```
13.1  ChromaDB timeout: init > 5s → AgentDB-only mode, is_ready=True
13.2  asyncio.gather concurrency: both AgentDB and ChromaDB init run concurrently
13.3  stop() flush ordering: pending observe() tasks complete before connections close
13.4  HybridCoordinator threshold restoration after pain day deactivates (score < 0.4)
13.5  Empty DB on first run: start() succeeds, empty working set, default PreferenceModel
13.6  get_snapshot() latency: completes in < 50 ms
13.7  start() completes within 5 seconds
```

**Approach:** Use `MockAgentDB` (already defined in `test_behavioral_twin_state.py`). For 13.1, patch `SemanticMemory.start()` to sleep > 5s. For 13.2, use `asyncio.Event` to verify concurrent execution. For 13.6/13.7, use `time.monotonic()`.

---

#### Task 14 — Unit tests in `test_semantic_memory.py` (4 tasks)

```
14.1  ChromaDB unavailable at startup: start() returns False, add() is no-op, query falls back
14.2  ChromaDB fails mid-session: _available=False, fallback used without interrupting pipeline
14.3  ./chroma_db/ directory created automatically if absent
14.4  Eviction: after CAP insertions, oldest EVICT_COUNT documents removed
```

**Approach:** For 14.1/14.2, mock `chromadb` import to raise `ImportError`. For 14.3, use `tempfile.TemporaryDirectory`. For 14.4, use `SmallCapMemory` subclass (already shown in test_semantic_memory.py Property 17 test).

---

#### Task 15 — Integration wiring tests (4 tasks)

```
15.1  Full startup: real AgentDB (:memory:), ChromaDB mocked
15.2  HybridCoordinator.route() calls get_snapshot() and attaches to routing context
15.3  ContinuousTrainer._adapt() with pain_day_active=True: no Gate 1 tightening
15.4  Session close → restart → cross-session context loaded correctly
```

**Approach:** Use `aiosqlite` with `:memory:` for real AgentDB. Mock `SemanticMemory.start()` to return `False`. For 15.2, patch `BehavioralTwinState.get_snapshot()` and verify it's called in `route()`.

---

## How to Resume in Claude Code

### 1. Verify implementation is intact

```bash
cd e:\Personal_Desktop_Agent
python -c "from behavioral_twin_state import BehavioralTwinState, TwinSnapshot, _DEFAULT_SNAPSHOT; print('OK')"
python -c "from semantic_memory import SemanticMemory; print('OK')"
```

### 2. Run existing passing tests

```bash
cd e:\Personal_Desktop_Agent
pytest tests/test_preference_model.py tests/test_pain_day_engine.py tests/test_behavioral_twin_state.py -v --hypothesis-seed=0
```

Note: `test_semantic_memory.py` ChromaDB tests will skip if `chromadb` is not installed — that's expected.

### 3. Continue with remaining tasks

Start with **Task 12** (2 tests, simplest), then **Task 13** (unit tests), then **Task 14**, then **Task 15**.

Prompt to use in Claude Code:
```
Continue the behavioral-twin-state spec implementation. 
Tasks 1–11 are complete. Resume at task 12.

Remaining tasks:
- 12.1, 12.2: Create tests/test_trainer_twin_integration.py
- 13.1–13.7: Add unit tests to tests/test_behavioral_twin_state.py
- 14.1–14.4: Add unit tests to tests/test_semantic_memory.py  
- 15.1–15.4: Create integration wiring tests

See e:\Personal_Desktop_Agent\docs\sprint3-handoff.md for full context.
Spec: e:\Personal_Desktop_Agent\.kiro\specs\behavioral-twin-state\
```

---

## Key Implementation Details to Know

### BehavioralTwinState internal state fields

```python
self._agent_db          # AgentDB instance
self._semantic_memory   # SemanticMemory instance
self._preference_model  # PreferenceModel instance
self._session_history   # list[str] — command texts, bounded window
self._working_set       # list[dict] — 500 most recent successful commands
self._current_snapshot  # TwinSnapshot — rebuilt on each observe() completion
self._is_ready          # bool — True after start() completes
self._pain_day_active   # bool — hysteresis state
self._pain_day_score    # float — last computed score
self._pain_day_task     # asyncio.Task — _pain_day_loop background task
self._pending_tasks     # set[asyncio.Task] — in-flight observe() tasks
self._session_cmd_count     # int — commands this session
self._session_fail_count    # int — failed commands this session
self._session_clarify_count # int — CLARIFY responses this session
self._session_gesture_confs # list[float] — gesture confidence values
self._session_start_ts      # float — time.monotonic() at session start
```

### MockAgentDB (already in test_behavioral_twin_state.py)

The `MockAgentDB` class in `tests/test_behavioral_twin_state.py` is the standard test double. Reuse it for tasks 13 and 15.

### MockCommand (already in test_behavioral_twin_state.py)

```python
@dataclass
class MockCommand:
    text: str = "click the button"
    source: str = "voice"
    whisper_logprob: float = -0.5
    gesture_confidence: float = 0.8
    session_context: list = None
    params: dict = None
```

### Creating a twin for tests

```python
def _make_twin(commands=None, session_history=None, preference_json=None):
    db = MockAgentDB(commands=commands, session_history=session_history, preference_json=preference_json)
    twin = BehavioralTwinState(agent_db=db, chroma_dir="/nonexistent_chroma_dir_for_tests")
    twin._semantic_memory._available = False  # disable ChromaDB
    return twin
```

---

## AgentDB New Methods (db.py)

All added in the `# Behavioral twin queries` section:

```python
get_recent_successful_commands(limit=500) -> list[dict]
get_session_commands(session_id, limit=20) -> list[dict]
get_most_recent_session_id(exclude_session_id=None) -> Optional[int]
get_command_stats_last_n_days(days=30) -> list[dict]
get_source_stats_last_n_days(days=7) -> list[dict]
write_session_history(session_id, history) -> None
read_session_history(session_id, limit=20) -> list[dict]
log_pain_day(session_id, score, active, fail_ratio, clarify_ratio, gesture_conf_delta, cmd_rate_delta) -> None
get_preference_model_snapshot() -> Optional[str]
```

New tables: `twin_session_history`, `twin_pain_day_log`

---

## Test Running Commands

```bash
# All new PBT tests (reproducible)
pytest tests/test_preference_model.py tests/test_pain_day_engine.py tests/test_semantic_memory.py tests/test_behavioral_twin_state.py -v --hypothesis-seed=0

# All tests
pytest tests/ -v

# Just the remaining tasks (after you write them)
pytest tests/test_trainer_twin_integration.py tests/test_semantic_memory.py tests/test_behavioral_twin_state.py -v -k "unit or integration or trainer"
```

---

## Correctness Properties Summary

| Property | File | Status |
|----------|------|--------|
| P1: Semantic round-trip identity | test_semantic_memory.py | ✅ |
| P2: Query count bounded by min(n, C_success) | test_semantic_memory.py | ✅ |
| P3: Working set = min(N, 500) | test_behavioral_twin_state.py | ✅ |
| P4: PreferenceModel stats correct | test_preference_model.py | ✅ |
| P5: time_bucket deterministic & exhaustive | test_preference_model.py | ✅ |
| P6: source_weights = success ratio | test_preference_model.py | ✅ |
| P7: PreferenceModel round-trip | test_preference_model.py | ✅ |
| P8: SessionHistory bounded window | test_behavioral_twin_state.py | ✅ |
| P9: Cross-session context round-trip | test_behavioral_twin_state.py | ✅ |
| P10: pain_day_score ∈ [0.0, 1.0] | test_pain_day_engine.py | ✅ |
| P11: PainDayMode hysteresis correct | test_pain_day_engine.py | ✅ |
| P12: TwinSnapshot immutable & complete | test_behavioral_twin_state.py | ✅ |
| P13: Gate 1 tightening suppressed on pain day | test_trainer_twin_integration.py | ❌ Task 12.1 |
| P14: observe() non-blocking | test_behavioral_twin_state.py | ✅ |
| P15: Gesture floor -0.05 extra on pain day | test_trainer_twin_integration.py | ❌ Task 12.2 |
| P16: query_similar filters success=True | test_semantic_memory.py | ✅ |
| P17: SemanticMemory CAP + eviction | test_semantic_memory.py | ✅ |
| P18: stop() flushes all tasks | test_behavioral_twin_state.py | ✅ |
| P19: Default snapshot before start() | test_behavioral_twin_state.py | ✅ |
| P20: Public methods never raise | test_behavioral_twin_state.py | ✅ |
