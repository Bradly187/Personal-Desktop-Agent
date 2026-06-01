# Handoff — AIOS Alignment Sprint (2026-06-01)

## Context

This session evaluated the Personal Desktop Agent against two external references:

1. **PDF:** *Navigating the Architectural Crossroads: Key Trade-Offs in Developing Autonomous Personal AI* — a survey of multi-agent OS design patterns (MCP, A2A/ACP protocols, cloud vs. edge execution, SAS feedback loops, centralized vs. decentralized orchestration).
2. **AIOS (Cerebrum) architecture diagram** — a formal multi-agent OS kernel with Planning/Action/Memory/Storage modules, an explicit syscall layer (LSC/MSC/TSC/SSC), a scheduler, and a kernel-level resource manager.

### Evaluation Summary

The project was assessed as a **specialized accessibility runtime** that exceeds most generic AIOS frameworks on the dimensions that matter (low-latency sensor fusion, pain-adaptive behavior, deterministic desktop control). The gaps were architectural formalization, not capability.

| Dimension | Score |
|---|---|
| Cloud/edge hybrid balance | Strong — edge-first, Bedrock Gate 4 fallback |
| Context awareness | Strong — BehavioralTwinState, AcousticProfiler, PainDayEngine |
| Memory architecture | Strong — three-tier (ChromaDB / aiosqlite / DuckDB) |
| Orchestration robustness | Adequate — centralized HybridCoordinator, no circuit-breaker yet |
| Inter-component decoupling | Was Weak → now addressed (MemoryManager syscall layer) |
| Semantic determinism | Weak — no LLM output schema validation (out of scope) |

---

## Implementation Plan Executed

Four architectural gaps were identified and fully implemented. All files pass `python -m py_compile`.

---

### Gap 4 — Context Namespacing in BehavioralTwinState ✅

**Problem:** `_session_history` was a flat list shared by both accessibility commands and DevAgent queries. A long dev debugging session could contaminate the few-shot context used for accessibility routing.

**Solution:** Namespaced `_session_history: dict[str, list[str]]` with two keys:
- `"accessibility"` — persistent, written to ChromaDB + PreferenceModel, persisted to AgentDB
- `"dev_agent"` — ephemeral, max depth 5, auto-clears on `get_session_context("dev_agent")`, never touches SemanticMemory or PreferenceModel

**Files changed:**
- `adaptive/behavioral_twin_state.py` — `_session_history` dict; `observe(namespace=)`, `get_session_context(namespace=)`, `clear_dev_namespace()`, `_persist_observation(namespace=)`; only accessibility namespace updates PreferenceModel/accumulators/SemanticMemory
- `adaptive/continuous_trainer.py` — `record_success(namespace=)` passes namespace through to `twin.observe()`
- `core/hybrid_coordinator.py` — session context injection always uses `"accessibility"`; both dev-agent early-return paths call `self._twin.clear_dev_namespace()`

---

### Gap 1 — Priority-Aware Scheduler ✅

**Problem:** `FusionEngine._emit()` used bare `asyncio.create_task(coordinator.route(cmd))` with no priority. A DevAgent RAG query running concurrently with a voice accessibility command could delay the command by hundreds of milliseconds during a flare.

**Solution:** `AccessibilityScheduler` backed by `asyncio.PriorityQueue`. Five priority tiers:

| Priority | Sources |
|---|---|
| 0 ACCESSIBILITY | touch, sound_action, multimodal (voice-click bypass) |
| 1 VOICE | WhisperStream transcription |
| 2 GESTURE | MediaPipe gesture commands |
| 3 DEV_AGENT | DevAgent plan/execute chains (semaphore-gated, max 1) |
| 4 BACKGROUND | CodebaseIndexer re-indexing, ContinuousTrainer |

ACCESSIBILITY/VOICE/GESTURE tasks run concurrently (no gate) — identical to the previous `create_task` behaviour. DEV_AGENT/BACKGROUND are gated by a `Semaphore(1)` with a 30-second `asyncio.wait_for` timeout. The 60 Hz tilt/cursor path in `FusionEngine.run()` is completely untouched.

**Files changed:**
- `core/scheduler.py` — **new file**; `AccessibilityScheduler`, `Priority` enum, `_worker`, `_run_task`, `_run_dev_task`
- `core/fusion_engine.py` — `set_scheduler()`, `_source_to_priority()`, `_emit()` submits via scheduler when wired (falls back to bare `create_task`)
- `inference/dev_agent.py` — `set_scheduler()` wiring stub + `self._scheduler = None`
- `main.py` — scheduler created after `FusionEngine`, started, wired to `fusion` and `dev_agent`, registered with `shutdown`

---

### Gap 2 — Memory Syscall Abstraction ✅

**Problem:** All components called `AgentDB` and `ChromaDB` directly by schema name. Adding a new agent required learning the 32-table schema. No schema validation at write boundaries.

**Solution:** `MemoryManager` — a thin façade with three canonical operations plus zero-copy pain-day hot-path accessors.

```python
memory.read_context(query, namespace, n)   # ChromaDB → AgentDB few-shot fallback
memory.write_state(key, value, namespace)  # schema-validated dispatch
memory.search_semantic(query, n)           # accessibility namespace search
memory.get_pain_day_active()               # zero-copy, no await, safe at 60 Hz
memory.get_pain_day_score()                # zero-copy, no await, safe at 60 Hz
```

Write validation uses `_VALID_KEYS[namespace]` frozensets. Invalid (key, namespace) pairs are logged and dropped, never silently written.

**Migration was incremental (Phases A–C):**
- **Phase A:** `MemoryManager` created, wired into coordinator/dev_agent/trainer via `set_memory()`. No behaviour change.
- **Phase B:** `DevAgent._persist_run()` routes agent_step writes through `write_state("agent_step")`. Falls back to direct `AgentDB` calls when `_memory` is None (unit test compatibility preserved).
- **Phase C:** `ContinuousTrainer.record_success()` routes few-shot writes through `write_state("few_shot_example")`. Same fallback pattern.

`BehavioralTwinState` internal calls were intentionally **not** migrated — they have subtle zero-copy pain-day semantics.

**Files changed:**
- `storage/memory_manager.py` — **new file**; `MemoryManager`, `_VALID_KEYS`, `_dispatch_write`
- `inference/dev_agent.py` — `set_memory()`, `self._memory`, `_persist_run()` Phase B migration
- `adaptive/continuous_trainer.py` — `set_memory()`, `self._memory`, `record_success()` Phase C migration
- `core/hybrid_coordinator.py` — `set_memory()`, `self._memory`
- `main.py` — `MemoryManager` instantiated after `twin_state.start()`, wired to coordinator/dev_agent/trainer

---

### Gap 3 — ResourceGovernor / Pain-Aware Kernel Primitive ✅

**Problem:** `PainDayEngine` only relaxed LLM thresholds and sensor dead zones. It had no connection to VRAM allocation, thread scheduling, or background job control. A flare or SVT attack had no effect on the hardware resource layer.

**Solution:** `ResourceGovernor` polls `MemoryManager.get_pain_day_score()` every 5 seconds (zero-copy, no event loop starvation). On flare (score ≥ 0.6, hysteresis mirrors `BehavioralTwinState`):

1. `fusion.apply_pain_day(True)` — relax sensor thresholds
2. `indexer.pause()` — skip new index jobs (in-progress completes)
3. Windows `SetThreadPriority(ABOVE_NORMAL)` on WhisperStream VAD thread via `ctypes` (non-blocking, runs in `asyncio.to_thread`)
4. POST to Ollama `keep_alive=0` for `qwen3-vl:30b` — evicts ~18 GB from VRAM

On recovery (score < 0.4) or `stop()`: all four actions reversed idempotently.

**Known risk:** If a `VisionGrounder` CLICK resolution fires just after `qwen3-vl:30b` is evicted, the cold load takes ~60 s. Mitigated by the existing `VisionGrounder` timeout fallthrough to Tesseract OCR.

**Files changed:**
- `core/resource_governor.py` — **new file**; `ResourceGovernor`, `_poll_loop`, `_on_flare_start/end`, `_raise/restore_whisper_priority`, `_reduce/restore_ollama_keepalive`, `_restore_resources_sync`
- `inference/codebase_indexer.py` — `self._paused`, `pause()`, `resume()`, pause guard in `index()` and `_on_file_changed()`
- `main.py` — governor created after `whisper.start()`, wired to fusion/whisper/indexer, started, registered with shutdown

---

## New Files

| File | Purpose |
|---|---|
| `core/scheduler.py` | `AccessibilityScheduler` — priority queue over `coordinator.route()` coroutines |
| `storage/memory_manager.py` | `MemoryManager` — schema-validated façade over AgentDB + SemanticMemory |
| `core/resource_governor.py` | `ResourceGovernor` — pain-day hardware resource control |

---

## Startup Sequence Changes in main.py

Four new stanzas added, in order:

```
twin_state.start()
  → MemoryManager(agent_db, twin_state)          # Gap 2 — after twin ready

DevAgent created
  → coordinator.set_memory(memory)
  → dev_agent.set_memory(memory)
  → trainer.set_memory(memory)                   # Gap 2 — wire all writers

FusionEngine created
  → AccessibilityScheduler created + started
  → fusion.set_scheduler(scheduler)
  → dev_agent.set_scheduler(scheduler)           # Gap 1 — before profiler

shutdown registered
  → shutdown.register(scheduler)                 # Gap 1

whisper.start()
  → ResourceGovernor(memory) created + started
  → governor.set_fusion_engine / whisper / indexer
  → shutdown.register(governor)                  # Gap 3
```

---

## What Remains (Not Implemented)

- **Circuit-breaker on HybridCoordinator:** If `route()` hangs (e.g. Bedrock timeout with no `asyncio.timeout`), the whole pipeline stalls. No fix yet.
- **LLM output schema validation:** Verb responses from local/cloud inference are still parsed by string split; a malformed response silently becomes a bad verb. No Pydantic gate added.
- **`_memory` not threadsafe for concurrent writes:** `MemoryManager._dispatch_write` is not re-entrant. Fine for the current single-event-loop design; relevant if true multi-threading is introduced.
- **SVT fast-path for ResourceGovernor:** Current 5-second poll means up to 5 s before VRAM is released on an SVT attack. A `set_manual_pain_day(True)` callback hook into `BehavioralTwinState` would reduce this to <1 s if needed.
- **aios_sdk package:** The plan included a Python SDK (`aios_sdk/`) exposing `register_agent()`, `subscribe_to_sensor()`, `invoke_tool()`. Not started — lower priority for single-user system.
- **Semantic determinism / output validation:** No LLM response schema enforcement. Out of scope for this sprint per the plan.

---

## Test Coverage Needed

The plan specified these assertions (not yet written):

**Gap 4:**
- After 10 DevAgent calls, `twin.get_session_context("accessibility")` unchanged
- `few_shot_examples` table has no `domain="dev"` rows after DevAgent routing
- `PreferenceModel.action_stats` does not accumulate WRITE_FILE/RUN_TERMINAL

**Gap 1:**
- ACCESSIBILITY coroutine completes before a concurrently-submitted DEV_AGENT coroutine that sleeps 500 ms
- 5 concurrent BACKGROUND tasks with 1 DEV_AGENT running = only 1 executing (semaphore check)

**Gap 2:**
- `write_state("nonexistent_key", ..., "accessibility")` → error log, no DB write
- `write_state("agent_run", ..., "accessibility")` → rejected (wrong namespace)
- `read_context()` parity with direct `AgentDB.get_few_shot_examples()`
- `get_pain_day_active()` returns True immediately after `twin.set_manual_pain_day(True)` without any await

**Gap 3:**
- After `twin._pain_day_score = 0.7` + one poll cycle → `flare_active == True`, `fusion._pain_day_active == True`
- After score drops to 0.3 + poll → `flare_active == False`
- `indexer._paused == True` during flare, `False` after
- Ollama keepalive POST called with `keep_alive=0` on flare start (mock `urllib.request`)
- `governor.stop()` always calls restore regardless of `_flare_active` state
