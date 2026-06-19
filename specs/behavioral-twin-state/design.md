# Design Document: Behavioral Twin State

## Overview

`BehavioralTwinState` is Sprint 3's core deliverable. It moves the Personal Desktop Agent from a static, session-isolated system to an adaptive, persistent one by maintaining a live model of Brad's behavioral patterns — preferred commands, time-of-day rhythms, pain-day signals, and cross-session context.

The component sits between `HybridCoordinator` (consumer) and `ContinuousTrainer` (feeder). Every routing decision is informed by a `TwinSnapshot` pulled from the twin state; every successful command feeds back into it. Two complementary stores back the state: ChromaDB for vector-semantic retrieval and the existing `AgentDB` (aiosqlite SQLite) for structured preference and session data. ChromaDB is optional — the system degrades to `AgentDB`-only operation if it is absent or slow.

### Key Design Decisions

- **No new DTO types.** `Command` remains the only cross-pipeline message. `TwinSnapshot` is an internal read-only view, not a pipeline message.
- **Non-blocking `observe()`.** Persistence is always scheduled as a background `asyncio.Task`; the caller returns immediately. This keeps the command pipeline latency unaffected.
- **Frozen snapshot.** `TwinSnapshot` is a `frozen=True` dataclass. Concurrent reads from `HybridCoordinator` and `ContinuousTrainer` are safe without locks.
- **Graceful degradation at every layer.** ChromaDB failure → AgentDB fallback. AgentDB failure → in-memory defaults. All three public methods (`observe`, `get_snapshot`, `query_similar`) swallow exceptions and return safe defaults.
- **Pain day hysteresis.** Activate at score > 0.6, deactivate at score < 0.4. Prevents rapid mode-flipping on borderline sessions.


## Architecture

### Component Diagram

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
1. HybridCoordinator.route(cmd) called
   └─► BehavioralTwinState.get_snapshot()          [< 50 ms]
       └─► returns TwinSnapshot (frozen)
   └─► snapshot injected into routing context
   └─► gate evaluation uses snapshot.pain_day_active, source_weights
   └─► Command.session_context ← snapshot.session_context

2. After successful execution:
   └─► ContinuousTrainer.record_success(cmd, action)
       └─► BehavioralTwinState.observe(cmd, action)  [non-blocking]
           └─► schedules asyncio.Task:
               ├─► AgentDB.insert_command (if not already persisted)
               ├─► PreferenceModel.update(action_verb, confidence, latency)
               ├─► SessionHistory.append(cmd.text)
               ├─► SemanticMemory.add(cmd.text, action, metadata)
               └─► pain_day_score recomputed (if 60s elapsed)

3. ContinuousTrainer._adapt() called (every 300s)
   └─► BehavioralTwinState.get_snapshot()
       └─► reads pain_day_active, source_weights
   └─► if pain_day_active: skip Gate 1 tightening
   └─► gesture floor = p10 - 0.05 - (0.05 if pain_day_active else 0)
```


## Components and Interfaces

### `TwinSnapshot` — frozen dataclass

```python
# behavioral_twin_state.py
from dataclasses import dataclass
from typing import Optional

@dataclass(frozen=True)
class TwinSnapshot:
    pain_day_active: bool                  # PainDayMode currently active
    preferred_actions: list[str]           # Top-5 action verbs for current time window
    source_weights: dict[str, float]       # Per-source success ratio (last 7 days)
    session_context: list[str]             # Last N command texts (20 normal, 10 pain day)
    command_count_today: int               # Commands executed today
    pain_day_score: float                  # Current score in [0.0, 1.0]
    snapshot_ts: float                     # time.monotonic() at creation

# Default snapshot returned before start() completes or on error
_DEFAULT_SNAPSHOT = TwinSnapshot(
    pain_day_active=False,
    preferred_actions=[],
    source_weights={},
    session_context=[],
    command_count_today=0,
    pain_day_score=0.0,
    snapshot_ts=0.0,
)
```

### `PreferenceModel` — structured preference tracking

```python
@dataclass
class ActionStats:
    frequency: int = 0
    mean_confidence: float = 0.0
    mean_latency_ms: float = 0.0
    last_used_ts: float = 0.0
    # Running Welford accumulators (avoids storing all samples)
    _conf_sum: float = field(default=0.0, repr=False)
    _lat_sum: float = field(default=0.0, repr=False)

@dataclass
class PreferenceModel:
    # Per-action-verb stats
    action_stats: dict[str, ActionStats] = field(default_factory=dict)
    # Time-of-day buckets: 0=00-06, 1=06-12, 2=12-18, 3=18-24
    # Maps bucket_index → {action_verb: count}
    time_buckets: dict[int, dict[str, int]] = field(default_factory=lambda: {i: {} for i in range(4)})
    # Per-source weights: source → (success_count, total_count)
    source_counts: dict[str, tuple[int, int]] = field(default_factory=dict)

    def update(self, action_verb: str, confidence: float, latency_ms: float,
               source: str, ts: float, success: bool) -> None: ...

    def top_actions(self, time_bucket: int, n: int = 5) -> list[str]: ...

    def source_weights(self) -> dict[str, float]: ...

    @staticmethod
    def time_bucket(ts: float) -> int:
        """Map a Unix timestamp to a 6-hour bucket index (0–3)."""
        import datetime
        hour = datetime.datetime.fromtimestamp(ts).hour
        return hour // 6

    def to_json(self) -> str: ...

    @classmethod
    def from_json(cls, data: str) -> "PreferenceModel": ...
```


### `SemanticMemory` — ChromaDB wrapper with AgentDB fallback

```python
class SemanticMemory:
    """ChromaDB-backed vector store for past commands.

    Falls back to AgentDB Jaccard scoring when ChromaDB is unavailable.
    All ChromaDB calls are wrapped in try/except; failures set _available=False.

    pip install chromadb
    """

    COLLECTION_NAME = "behavioral_memory"
    CHROMA_DIR = "./chroma_db"
    CAP = 10_000
    EVICT_COUNT = 1_000

    def __init__(
        self,
        chroma_dir: str = CHROMA_DIR,
        agent_db: Optional["AgentDB"] = None,
        encoder=None,  # Optional custom SentenceTransformer; None = ChromaDB default
    ) -> None: ...

    async def start(self) -> bool:
        """Initialize ChromaDB client. Returns True if available."""
        ...

    async def add(
        self,
        text: str,
        action: str,
        source: str,
        ts: float,
        success: bool,
        doc_id: Optional[str] = None,
    ) -> None:
        """Store a command document. Schedules eviction if cap reached."""
        ...

    async def query_similar(self, text: str, n: int = 5) -> list[dict]:
        """Return n most similar successful commands.

        ChromaDB path: cosine distance, filtered where success=True.
        Fallback path: AgentDB Jaccard scoring via get_few_shot_examples().
        """
        ...

    async def count(self) -> int:
        """Current document count. Returns 0 if ChromaDB unavailable."""
        ...

    async def _evict_oldest(self) -> None:
        """Remove the EVICT_COUNT oldest documents by ts metadata."""
        ...

    @property
    def available(self) -> bool: ...
```

### `BehavioralTwinState` — core class

```python
class BehavioralTwinState:
    """Persistent behavioral model for Brad.

    Lifecycle:
        twin = BehavioralTwinState(agent_db=db)
        await twin.start()          # concurrent AgentDB + ChromaDB init
        # ... pipeline runs ...
        await twin.stop()           # flush tasks, close connections

    Thread safety: all public methods are coroutines safe to call from
    the single asyncio event loop. TwinSnapshot is frozen — safe for
    concurrent reads without locks.
    """

    WORKING_SET_SIZE = 500
    SESSION_HISTORY_MAX = 20
    SESSION_HISTORY_PAIN_DAY = 10
    PAIN_DAY_ACTIVATE_THRESHOLD = 0.6
    PAIN_DAY_DEACTIVATE_THRESHOLD = 0.4
    PAIN_DAY_MIN_COMMANDS = 5
    PAIN_DAY_RECOMPUTE_INTERVAL_S = 60.0
    STARTUP_TIMEOUT_S = 5.0

    def __init__(
        self,
        agent_db: "AgentDB",
        chroma_dir: str = "./chroma_db",
        encoder=None,
    ) -> None: ...

    # ── Lifecycle ──────────────────────────────────────────────────────

    async def start(self) -> None:
        """Concurrent AgentDB + ChromaDB init via asyncio.gather.
        Sets is_ready=True on completion. Completes within STARTUP_TIMEOUT_S.
        """
        ...

    async def stop(self) -> None:
        """Flush all pending background tasks, persist final state, close."""
        ...

    @property
    def is_ready(self) -> bool: ...

    # ── Public pipeline API ────────────────────────────────────────────

    async def get_snapshot(self) -> TwinSnapshot:
        """Return current behavioral state. Always returns; never raises.
        Returns _DEFAULT_SNAPSHOT if not ready or on error.
        Completes in < 50 ms under normal conditions.
        """
        ...

    async def observe(self, cmd: "Command", action_str: str) -> None:
        """Record a successful command. Non-blocking — schedules background task.
        Never raises.
        """
        ...

    async def query_similar(self, text: str, n: int = 5) -> list[dict]:
        """Return n most semantically similar past commands. Never raises."""
        ...

    def get_session_context(self) -> list[str]:
        """Return current SessionHistory as list[str] for Command.session_context."""
        ...

    # ── Internal ───────────────────────────────────────────────────────

    async def _persist_observation(self, cmd: "Command", action_str: str) -> None:
        """Background task: update PreferenceModel, SessionHistory, SemanticMemory."""
        ...

    async def _load_working_set(self) -> None:
        """Load 500 most recent successful commands from AgentDB on startup."""
        ...

    async def _load_preference_model(self) -> None:
        """Reconstruct PreferenceModel from AgentDB settings_versions."""
        ...

    async def _load_session_history(self) -> None:
        """Populate SessionHistory from most recent prior session in AgentDB."""
        ...

    def _recompute_pain_day_score(self) -> float:
        """Compute pain_day_score from current session signals. Returns [0.0, 1.0]."""
        ...

    async def _pain_day_loop(self) -> None:
        """Background task: recompute pain_day_score every 60 seconds."""
        ...
```


## Data Models

### AgentDB Schema Additions

Two new tables and one new query pattern are added to `db.py`. No existing tables are modified.

```sql
-- New table: twin_session_history
-- Stores the SessionHistory snapshot written at session close.
-- Used to populate cross-session context on restart.
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

-- New table: twin_pain_day_log
-- Audit trail of pain_day_score values and mode transitions.
-- Useful for retrospective analysis and debugging.
CREATE TABLE IF NOT EXISTS twin_pain_day_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      INTEGER NOT NULL REFERENCES sessions(id),
    ts              REAL    NOT NULL,
    pain_day_score  REAL    NOT NULL,
    pain_day_active INTEGER NOT NULL,  -- 0 or 1
    fail_ratio      REAL,
    clarify_ratio   REAL,
    gesture_conf_delta REAL,
    cmd_rate_delta  REAL
);
CREATE INDEX IF NOT EXISTS idx_pdl_session ON twin_pain_day_log(session_id, ts);
```

**New `settings_versions` usage** (existing table, new key):

```
component = 'preference_model'
key       = 'snapshot'
new_value = <JSON blob of PreferenceModel>
changed_by = 'BehavioralTwinState'
```

**New AgentDB query methods** added to `AgentDB` class in `db.py`:

```python
async def get_recent_successful_commands(
    self, limit: int = 500
) -> list[dict]:
    """Return the N most recent successful commands across all sessions."""
    ...

async def get_session_commands(
    self, session_id: int, limit: int = 20
) -> list[dict]:
    """Return the last N commands from a specific session."""
    ...

async def get_most_recent_session_id(
    self, exclude_session_id: Optional[int] = None
) -> Optional[int]:
    """Return the id of the most recent session (optionally excluding current)."""
    ...

async def get_command_stats_last_n_days(
    self, days: int = 30
) -> list[dict]:
    """Return commands from the last N days for time-of-day distribution."""
    ...

async def get_source_stats_last_n_days(
    self, days: int = 7
) -> list[dict]:
    """Return per-source success/total counts for the last N days."""
    ...

async def write_session_history(
    self, session_id: int, history: list[dict]
) -> None:
    """Persist SessionHistory to twin_session_history table at session close."""
    ...

async def read_session_history(
    self, session_id: int, limit: int = 20
) -> list[dict]:
    """Read SessionHistory for a given session."""
    ...

async def log_pain_day(
    self, session_id: int, score: float, active: bool,
    fail_ratio: float, clarify_ratio: float,
    gesture_conf_delta: float, cmd_rate_delta: float,
) -> None:
    """Append a pain_day_log row."""
    ...

async def get_preference_model_snapshot(self) -> Optional[str]:
    """Return the most recent preference_model JSON from settings_versions."""
    ...
```

### ChromaDB Collection Schema

Collection name: `"behavioral_memory"`  
Persistent directory: `./chroma_db/`  
Embedding function: ChromaDB default (`all-MiniLM-L6-v2` via `chromadb.utils.embedding_functions.DefaultEmbeddingFunction`)

```python
# Document structure stored in ChromaDB
{
    # ChromaDB document (the text that gets embedded)
    "documents": [cmd.text],

    # ChromaDB metadata (filterable, not embedded)
    "metadatas": [{
        "action":  action_str,       # str — e.g. "CLICK"
        "source":  cmd.source,       # str — e.g. "voice"
        "ts":      time.time(),      # float — Unix timestamp
        "success": 1,                # int — 1=True, 0=False (ChromaDB metadata is str/int/float)
        "session_id": session_id,    # int
    }],

    # ChromaDB id — deterministic from content hash to allow idempotent adds
    "ids": [hashlib.sha256(f"{cmd.text}:{action_str}:{ts}".encode()).hexdigest()[:16]],
}

# Query filter (success=True only)
where = {"success": {"$eq": 1}}
```


## Pain Day Score Computation Pipeline

The `pain_day_score` is a weighted average of four normalized signals, each in [0.0, 1.0]:

```
signal_1 = fail_ratio          = failed_cmds / total_cmds
signal_2 = clarify_ratio       = clarify_responses / total_responses
signal_3 = gesture_conf_drop   = max(0, (baseline_conf - current_mean_conf) / baseline_conf)
signal_4 = cmd_rate_drop       = max(0, (baseline_rate - current_rate) / baseline_rate)

pain_day_score = clamp(
    0.35 * signal_1 +
    0.25 * signal_2 +
    0.20 * signal_3 +
    0.20 * signal_4,
    0.0, 1.0
)
```

Where:
- `baseline_conf` = mean gesture confidence over the last 30 days from `AgentDB`
- `baseline_rate` = mean commands-per-hour over the last 30 days from `AgentDB`
- `current_mean_conf` = mean gesture confidence in the current session
- `current_rate` = commands-per-hour in the current session

**Guard conditions:**
- If `total_cmds < PAIN_DAY_MIN_COMMANDS (5)`: score is computed but `PainDayMode` is not activated from session signals alone
- If baselines are unavailable (new user, no history): `signal_3` and `signal_4` default to 0.0

**Hysteresis state machine:**

```
State: NORMAL
  score > 0.6 AND total_cmds >= 5 → transition to PAIN_DAY
  (or external flag set) → transition to PAIN_DAY

State: PAIN_DAY
  score < 0.4 → transition to NORMAL
```

**Recomputation:** Every 60 seconds via `_pain_day_loop()` background task. Also recomputed immediately on each `observe()` call if the 60s interval has elapsed.


## Concurrency Model

### Startup: `asyncio.gather` with timeout

```python
async def start(self) -> None:
    self._is_ready = False
    try:
        async with asyncio.timeout(self.STARTUP_TIMEOUT_S):
            await asyncio.gather(
                self._init_agent_db(),      # load working set, preferences, session history
                self._init_chroma(),        # connect ChromaDB, create/open collection
            )
    except asyncio.TimeoutError:
        log.warning("BehavioralTwinState: startup timed out — ChromaDB may be slow")
        # AgentDB init is fast; ChromaDB is the likely culprit
        # Cancel ChromaDB and continue in AgentDB-only mode
        self._semantic_memory._available = False
    except Exception as exc:
        log.warning("BehavioralTwinState: startup error: %s", exc)
    finally:
        self._is_ready = True
        self._pain_day_task = asyncio.create_task(self._pain_day_loop())
        log.info("BehavioralTwinState ready (chroma=%s)", self._semantic_memory.available)
```

### Background `observe()` pattern

```python
async def observe(self, cmd: "Command", action_str: str) -> None:
    """Non-blocking. Each call creates its own independent background task."""
    try:
        task = asyncio.create_task(
            self._persist_observation(cmd, action_str),
            name=f"twin_observe_{id(cmd)}",
        )
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)
    except Exception as exc:
        log.warning("BehavioralTwinState.observe scheduling failed: %s", exc)
```

### Shutdown: flush pending tasks

```python
async def stop(self) -> None:
    # Cancel pain day recompute loop
    if self._pain_day_task:
        self._pain_day_task.cancel()
        try:
            await self._pain_day_task
        except asyncio.CancelledError:
            pass

    # Flush all pending observe() tasks
    if self._pending_tasks:
        await asyncio.gather(*self._pending_tasks, return_exceptions=True)

    # Persist final state
    await self._persist_preference_model()
    await self._write_session_history()

    log.info("BehavioralTwinState stopped (%d tasks flushed)", len(self._pending_tasks))
```

### `get_snapshot()` latency budget

`get_snapshot()` reads only in-memory state — no I/O. The `_snapshot_cache` is rebuilt on each `observe()` completion and on each pain day recompute. The method itself is a simple attribute read:

```python
async def get_snapshot(self) -> TwinSnapshot:
    try:
        if not self._is_ready:
            return _DEFAULT_SNAPSHOT
        return self._current_snapshot  # pre-built frozen dataclass
    except Exception as exc:
        log.warning("BehavioralTwinState.get_snapshot error: %s", exc)
        return _DEFAULT_SNAPSHOT
```

This guarantees < 50 ms completion — it is effectively O(1).


## Integration Points

### `HybridCoordinator` changes

Three modifications to `hybrid_coordinator.py`:

**1. Constructor — accept `BehavioralTwinState`:**

```python
def __init__(
    self,
    ...
    twin_state: Optional["BehavioralTwinState"] = None,  # NEW
) -> None:
    ...
    self._twin = twin_state
```

**2. `route()` — call `get_snapshot()` before gate evaluation:**

```python
async def route(self, cmd: Command) -> dict:
    # NEW: pull snapshot before any gate logic
    snapshot: TwinSnapshot = _DEFAULT_SNAPSHOT
    if self._twin:
        try:
            snapshot = await self._twin.get_snapshot()
        except Exception as exc:
            log.warning("BehavioralTwinState.get_snapshot failed: %s", exc)

    # NEW: apply pain day threshold adjustments
    if snapshot.pain_day_active:
        self._cfg = _apply_pain_day_adjustments(self._cfg, snapshot)

    # NEW: populate session_context from twin state
    if self._twin and self._twin.is_ready:
        cmd = _dc_replace(cmd, session_context=self._twin.get_session_context())

    # ... existing gate logic unchanged ...
```

**3. Helper — pain day threshold adjustment (non-mutating):**

```python
def _apply_pain_day_adjustments(
    cfg: CoordinatorConfig, snapshot: TwinSnapshot
) -> CoordinatorConfig:
    """Return a modified config copy with pain-day threshold relaxations.
    Does not mutate the original config.
    """
    from dataclasses import replace
    return replace(
        cfg,
        whisper_logprob_min=cfg.whisper_logprob_min - 0.15,
        gesture_confidence_min=cfg.gesture_confidence_min - 0.10,
    )
```

Note: The adjusted config is used only for the duration of the session while `pain_day_active` is True. The original `CoordinatorConfig` instance is preserved and restored when `pain_day_active` becomes False.

### `ContinuousTrainer` changes

Two modifications to `continuous_trainer.py`:

**1. Constructor — accept `BehavioralTwinState`:**

```python
def __init__(
    self,
    agent_db: "AgentDB",
    ...
    twin_state: Optional["BehavioralTwinState"] = None,  # NEW
) -> None:
    ...
    self._twin = twin_state
```

**2. `record_success()` — call `observe()`:**

```python
async def record_success(
    self,
    cmd: "Command",
    action_str: str,
    domain: str = "command",
    command_id: Optional[int] = None,
) -> None:
    await self._db.upsert_few_shot_example(cmd, action_str, domain, command_id)

    # NEW: feed observation into twin state
    if self._twin:
        await self._twin.observe(cmd, action_str)  # non-blocking

    # ... existing gesture tracking unchanged ...
```

**3. `_adapt()` — read snapshot for pain day guard and gesture floor:**

```python
async def _adapt(self) -> None:
    log.debug("ContinuousTrainer: running adaptation pass")

    # NEW: read twin snapshot for adaptation decisions
    snapshot = _DEFAULT_SNAPSHOT
    if self._twin:
        try:
            snapshot = await self._twin.get_snapshot()
        except Exception as exc:
            log.warning("ContinuousTrainer: twin snapshot failed: %s", exc)

    entries = await self._db.get_recent_routing_stats(limit=1000)
    if entries:
        self._adapt_gate1_threshold(entries, pain_day_active=snapshot.pain_day_active)  # MODIFIED
    await self._db.promote_hotwords(self._hotword_threshold)
    await self._update_gesture_calibration(pain_day_active=snapshot.pain_day_active)  # MODIFIED
```

**4. `_adapt_gate1_threshold()` — pain day guard:**

```python
def _adapt_gate1_threshold(
    self, entries: list[dict], pain_day_active: bool = False
) -> None:
    """Requirement 6.3 — skip tightening on pain days."""
    if not self._config:
        return
    # ... existing cloud_rate / failure_rate computation ...

    if cloud_rate > self._cloud_limit and failure_rate < self._failure_limit:
        if pain_day_active:
            log.info("Gate 1 tightening suppressed (pain day active)")
            return  # NEW: skip tightening on pain days
        old = self._config.whisper_logprob_min
        self._config.whisper_logprob_min = min(-0.1, old + self._gate1_step)
        # ... existing log ...
```

**5. `_update_gesture_calibration()` — pain day floor reduction:**

```python
async def _update_gesture_calibration(self, pain_day_active: bool = False) -> None:
    """Requirement 6.5 — additional 0.05 floor reduction on pain days."""
    for gesture in ["POINT", "PINCH", "OPEN_PALM", "FIST"]:
        # ... existing p10 computation ...
        floor = max(0.0, p10 - 0.05)
        if pain_day_active:
            floor = max(0.0, floor - 0.05)  # NEW: additional reduction
        # ... existing update ...
```


## Error Handling

### Degradation Hierarchy

```
Level 1 (full): ChromaDB + AgentDB both available
  → semantic similarity via cosine distance
  → full preference model from history
  → cross-session context from prior session

Level 2 (partial): AgentDB only (ChromaDB unavailable/slow)
  → similarity via Jaccard scoring on few_shot_examples
  → full preference model from history
  → cross-session context from prior session
  → SemanticMemory.available = False; all add() calls are no-ops

Level 3 (minimal): Neither store available
  → in-memory state only (resets on restart)
  → _DEFAULT_SNAPSHOT returned from get_snapshot()
  → observe() schedules tasks that fail silently
  → system continues operating with static defaults
```

### Exception Boundaries

Every public method has a top-level `try/except Exception`:

```python
async def observe(self, cmd, action_str):
    try:
        ...
    except Exception as exc:
        log.warning("BehavioralTwinState.observe error: %s", exc)
        # no re-raise

async def get_snapshot(self):
    try:
        ...
    except Exception as exc:
        log.warning("BehavioralTwinState.get_snapshot error: %s", exc)
        return _DEFAULT_SNAPSHOT

async def query_similar(self, text, n):
    try:
        ...
    except Exception as exc:
        log.warning("BehavioralTwinState.query_similar error: %s", exc)
        return []
```

### ChromaDB Timeout Handling

```python
async def _init_chroma(self) -> None:
    try:
        async with asyncio.timeout(self.STARTUP_TIMEOUT_S):
            available = await self._semantic_memory.start()
            if not available:
                log.warning("SemanticMemory: ChromaDB unavailable — AgentDB fallback active")
    except asyncio.TimeoutError:
        log.warning("SemanticMemory: ChromaDB init timed out — AgentDB fallback active")
        self._semantic_memory._available = False
    except Exception as exc:
        log.warning("SemanticMemory: ChromaDB init error: %s — AgentDB fallback active", exc)
        self._semantic_memory._available = False
```


## File Layout

```
e:\Personal_Desktop_Agent\
├── behavioral_twin_state.py      # NEW — BehavioralTwinState, TwinSnapshot, PreferenceModel
├── semantic_memory.py            # NEW — SemanticMemory (ChromaDB wrapper + AgentDB fallback)
├── db.py                         # MODIFIED — new tables + new query methods
├── hybrid_coordinator.py         # MODIFIED — twin_state param, get_snapshot() call, pain day adjustments
├── continuous_trainer.py         # MODIFIED — twin_state param, observe() call, pain day guards
├── main.py                       # MODIFIED — wire BehavioralTwinState into startup sequence
├── chroma_db/                    # CREATED at runtime — ChromaDB persistent storage
│   └── (ChromaDB internal files)
└── tests/
    ├── test_behavioral_twin_state.py   # NEW — unit + property tests
    ├── test_semantic_memory.py         # NEW — unit + property tests
    └── test_preference_model.py        # NEW — unit + property tests
```

### `main.py` wiring (startup sequence)

```python
# In main.py startup:
agent_db = AgentDB()
await agent_db.open(Path("agent.db"))
session_id = await agent_db.insert_session(mode="normal")

twin_state = BehavioralTwinState(agent_db=agent_db)
await twin_state.start()  # concurrent AgentDB + ChromaDB init

trainer = ContinuousTrainer(
    agent_db=agent_db,
    config=coordinator_config,
    twin_state=twin_state,   # NEW
)
await trainer.start()

coordinator = HybridCoordinator(
    local=local_inference,
    config=coordinator_config,
    trainer=trainer,
    agent_db=agent_db,
    session_id=session_id,
    twin_state=twin_state,   # NEW
)

# On shutdown:
await twin_state.stop()
await trainer.stop()
await agent_db.close_session(session_id)
await agent_db.close()
```


## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

PBT is appropriate here because `BehavioralTwinState` contains substantial pure logic: score computation, preference model statistics, session history windowing, time bucket assignment, and source weight ratios. These are all functions with clear input/output behavior where input variation reveals edge cases. The PBT library used is **Hypothesis** (Python).


### Property 1: Round-trip semantic identity

*For any* non-empty command text `t`, storing `t` in `SemanticMemory` and then calling `query_similar(t, 1)` SHALL return `t` as the top result.

**Validates: Requirements 1.7**


### Property 2: Query result count bounded by n and collection size

*For any* `SemanticMemory` collection of size `C` and any query with `n > 0`, `query_similar(text, n)` SHALL return exactly `min(n, C_success)` results, where `C_success` is the count of `success=True` documents.

**Validates: Requirements 1.6**


### Property 3: Working set size is min(N, 500)

*For any* `AgentDB` containing `N` successful commands, after `BehavioralTwinState.start()`, the in-memory working set SHALL contain exactly `min(N, 500)` entries.

**Validates: Requirements 1.2**


### Property 4: PreferenceModel statistics are correct

*For any* sequence of `k` commands with the same action verb, each with known confidence `c_i` and latency `l_i`, after calling `observe()` for each, the `PreferenceModel` entry for that verb SHALL have `frequency = k`, `mean_confidence = mean(c_i)`, and `mean_latency = mean(l_i)`.

**Validates: Requirements 2.1**


### Property 5: Time bucket assignment is deterministic and exhaustive

*For any* Unix timestamp `ts`, `PreferenceModel.time_bucket(ts)` SHALL return an integer in `{0, 1, 2, 3}` where `bucket = floor(hour(ts) / 6)`, and every hour in `[0, 23]` maps to exactly one bucket.

**Validates: Requirements 2.3**


### Property 6: Source weights equal success ratio

*For any* command history with known per-source success and total counts over the last 7 days, `PreferenceModel.source_weights()` SHALL return weights equal to `success_count / total_count` for each source, clamped to `[0.0, 1.0]`.

**Validates: Requirements 2.4**


### Property 7: Preference model survives restart (round-trip)

*For any* `PreferenceModel` state built from a sequence of `observe()` calls, serializing it to `AgentDB` and reconstructing it in a new `BehavioralTwinState` instance SHALL produce an equivalent `PreferenceModel` (same action stats, time buckets, and source counts).

**Validates: Requirements 2.7**


### Property 8: SessionHistory is a bounded window with correct projection

*For any* sequence of `N` successfully observed commands, `get_session_context()` SHALL return a list of exactly `min(N, 20)` strings (or `min(N, 10)` when `pain_day_active` is True), containing the command texts of the last `min(N, window)` commands in chronological order.

**Validates: Requirements 3.1, 3.3, 4.5**


### Property 9: Cross-session context round-trip

*For any* prior session containing `N` successful commands, starting a new `BehavioralTwinState` instance pointing at the same `AgentDB` SHALL populate `SessionHistory` with the last `min(N, 20)` commands from that prior session, regardless of whether the prior session has an `ended_at` timestamp.

**Validates: Requirements 3.2, 3.6**


### Property 10: pain_day_score is always in [0.0, 1.0]

*For any* combination of `fail_ratio`, `clarify_ratio`, `gesture_conf_delta`, and `cmd_rate_delta` (including extreme values: 0.0, 1.0, negative, NaN-guarded), `_recompute_pain_day_score()` SHALL return a float in `[0.0, 1.0]`.

**Validates: Requirements 4.1**


### Property 11: PainDayMode hysteresis is correct

*For any* sequence of `pain_day_score` values, `pain_day_active` SHALL be True if and only if the most recent score that crossed a threshold was > 0.6 (activate), and False if the most recent crossing was < 0.4 (deactivate). Sessions with fewer than 5 commands SHALL not activate from session signals alone.

**Validates: Requirements 4.2, 4.6, 4.8**


### Property 12: TwinSnapshot is immutable and complete

*For any* `TwinSnapshot` returned by `get_snapshot()`, (a) attempting to set any field SHALL raise `dataclasses.FrozenInstanceError`, and (b) all required fields (`pain_day_active`, `preferred_actions`, `source_weights`, `session_context`, `command_count_today`, `pain_day_score`, `snapshot_ts`) SHALL be present with correct types.

**Validates: Requirements 5.2, 5.6**


### Property 13: Gate 1 tightening is suppressed on pain days

*For any* routing statistics that would normally trigger Gate 1 threshold tightening (cloud escalation > 30%, local failure < 10%), when `pain_day_active` is True, `ContinuousTrainer._adapt_gate1_threshold()` SHALL NOT decrease `whisper_logprob_min`.

**Validates: Requirements 6.3**


### Property 14: observe() returns before persistence completes

*For any* `observe()` call where the background persistence task is artificially delayed, `observe()` SHALL return before the persistence task completes (i.e., the return is non-blocking).

**Validates: Requirements 6.4**


### Property 15: Gesture floor reduction on pain days

*For any* set of gesture samples that produces a computed floor `f`, the floor applied when `pain_day_active` is True SHALL be `max(0.0, f - 0.05)`, and the floor when `pain_day_active` is False SHALL be `f`. The difference SHALL always be exactly 0.05 (or `f` if `f < 0.05`).

**Validates: Requirements 6.5**


### Property 16: SemanticMemory query filters to success=True only

*For any* `SemanticMemory` collection containing a mix of `success=True` and `success=False` documents, `query_similar(text, n)` SHALL return only documents where `success=True`, regardless of how similar the `success=False` documents are to the query.

**Validates: Requirements 7.4**


### Property 17: SemanticMemory count is bounded by cap

*For any* sequence of insertions, `count()` SHALL return the number of successfully stored documents up to `CAP (10,000)`. After the cap is reached and eviction runs, `count()` SHALL be `CAP - EVICT_COUNT + new_insertions` and the evicted documents SHALL be the oldest by timestamp.

**Validates: Requirements 7.5, 7.7**


### Property 18: stop() flushes all pending tasks

*For any* number `N` of `observe()` calls made before `stop()`, after `stop()` returns, all `N` background persistence tasks SHALL have completed (either successfully or with a logged error).

**Validates: Requirements 8.4**


### Property 19: Default snapshot before start()

*For any* call to `get_snapshot()` on a `BehavioralTwinState` instance where `start()` has not yet completed, the returned `TwinSnapshot` SHALL have `pain_day_active=False`, `preferred_actions=[]`, `pain_day_score=0.0`, and `session_context=[]`.

**Validates: Requirements 8.6**


### Property 20: Public methods never raise

*For any* input to `observe()`, `get_snapshot()`, or `query_similar()` — including `None`, empty strings, strings of length 10,000, and inputs that cause internal errors — none of these methods SHALL raise an exception; all SHALL return a safe default value.

**Validates: Requirements 8.7**


## Testing Strategy

### Dual Testing Approach

Unit tests cover specific examples, integration wiring, and edge cases. Property tests cover universal invariants across generated inputs. Both are required for comprehensive coverage.

### Property-Based Testing

**Library:** [Hypothesis](https://hypothesis.readthedocs.io/) (`pip install hypothesis`)  
**Minimum iterations:** 100 per property (Hypothesis default `max_examples=100`)  
**Tag format:** `# Feature: behavioral-twin-state, Property N: <property_text>`

Each correctness property maps to exactly one Hypothesis `@given` test. Example:

```python
from hypothesis import given, settings
from hypothesis import strategies as st

# Feature: behavioral-twin-state, Property 10: pain_day_score is always in [0.0, 1.0]
@given(
    fail_ratio=st.floats(min_value=0.0, max_value=1.0),
    clarify_ratio=st.floats(min_value=0.0, max_value=1.0),
    gesture_conf_delta=st.floats(min_value=-1.0, max_value=1.0),
    cmd_rate_delta=st.floats(min_value=-1.0, max_value=1.0),
)
@settings(max_examples=100)
def test_pain_day_score_bounds(fail_ratio, clarify_ratio, gesture_conf_delta, cmd_rate_delta):
    score = compute_pain_day_score(fail_ratio, clarify_ratio, gesture_conf_delta, cmd_rate_delta)
    assert 0.0 <= score <= 1.0
```

### Unit Tests

Focus on:
- Specific degradation scenarios (ChromaDB unavailable at startup, ChromaDB fails mid-session)
- Integration wiring (coordinator calls `get_snapshot()`, trainer calls `observe()`)
- Lifecycle transitions (`is_ready` before/after `start()`)
- Timing constraints (`get_snapshot()` < 50 ms, `start()` < 5 s)
- Edge cases: empty DB on first run, abnormal session termination, < 5 commands in session

### Integration Tests

- Full startup sequence with real `AgentDB` (in-memory SQLite via `:memory:`)
- `HybridCoordinator.route()` with real `BehavioralTwinState` (ChromaDB mocked)
- `ContinuousTrainer._adapt()` with pain day active — verify no tightening
- Session close → restart → verify cross-session context loaded

### Test File Layout

```
tests/
├── test_behavioral_twin_state.py   # Properties 3, 8, 9, 11, 12, 14, 18, 19, 20
│                                   # + unit tests for lifecycle, degradation, wiring
├── test_semantic_memory.py         # Properties 1, 2, 16, 17
│                                   # + unit tests for ChromaDB init, eviction, fallback
├── test_preference_model.py        # Properties 4, 5, 6, 7
│                                   # + unit tests for serialization, reconstruction
└── test_pain_day_engine.py         # Properties 10, 11
│                                   # + unit tests for hysteresis, guard conditions
└── test_trainer_twin_integration.py # Properties 13, 15
                                    # + unit tests for wiring
```

### Avoiding Over-Testing

Unit tests should not duplicate what property tests already cover. Specifically:
- Do not write unit tests for `pain_day_score` boundary values — Property 10 covers all inputs
- Do not write unit tests for every `SessionHistory` length — Property 8 covers all lengths
- Do write unit tests for: ChromaDB timeout behavior, `asyncio.gather` concurrency in `start()`, `stop()` flush ordering, and `HybridCoordinator` threshold restoration after pain day deactivation
