# ADR-001: Personal Desktop Agent — System Architecture Review

**Status:** Accepted (existing system)
**Date:** 2026-05-12
**Deciders:** Brad Tarver

---

## Context

The Personal Desktop Agent is a multimodal accessibility system for a single user with rheumatoid arthritis. An iPad Pro serves as the sensor hub; a Windows PC with RTX 5090 runs inference and executes desktop actions. Phases 1–4 skeleton are complete (~6,300 LOC across 18 Python modules and a SwiftUI iPad app).

This ADR evaluates the current design at the end of Phase 4 and records what is worth preserving vs. what must be addressed before production use.

**Pipeline under review:**
```
iPad sensors → WebSocket :8765 → IPadBridge → FusionEngine
  → HybridCoordinator (4-gate) → LocalInference / DevAgent
  → CommandExecutor → MCP tools → pyautogui / Win32
```

Supporting systems: AgentDB (aiosqlite, 12 tables), AnalyticsDB (DuckDB), ContinuousTrainer, WhisperStream, GestureProcessor.

---

## Strengths (what to preserve)

| Dimension | Assessment |
|-----------|------------|
| Separation of concerns | High — FusionEngine, HybridCoordinator, DevAgent, ContinuousTrainer each own a single responsibility with no cross-cutting state |
| Universal DTO | `Command` dataclass carries source, confidence scores, gaze coords, and session context; no raw dicts cross pipeline boundaries |
| Gate semantics | 4 gates (Privacy → Confidence → Complexity → VRAM/Latency) are independent, documented, and individually bypassable per source type |
| Graceful degradation | Every optional dependency (Whisper, MediaPipe, MiniLM, zeroconf) logs a warning and continues without crashing |
| Database consolidation | AgentDB replaces 3 scattered files (trainer.db, routing_log.jsonl, gesture_calibration.json); MiniLM few-shot retrieval shares the same store |
| Async discipline | 60 Hz fusion loop is never blocked; all blocking I/O uses `asyncio.to_thread()`; MOUSEDOWN/MOUSEUP are correctly synchronous for timing-critical drag |
| VRAM awareness | Gate 3 checks free VRAM before inference; measured per-model budgets on RTX 5090 prevent OOM |

---

## Risks & Weaknesses

### Critical — Block production readiness

**1. No timeout protection**

`OllamaInference.infer()` calls Ollama via HTTP with no timeout. If Ollama crashes or hangs, the fusion loop backs up indefinitely. Similarly, each `DevAgent` step has no per-step timeout.

*Fix:* Wrap Ollama HTTP call in `asyncio.wait_for(timeout=30)`. Add `asyncio.wait_for(timeout=60)` per DevAgent step. Location: [`local_inference.py:~130`](../local_inference.py), [`dev_agent.py:~200`](../dev_agent.py).

**2. No backpressure / queue depth management**

FusionEngine can emit up to 60 Commands/second. If HybridCoordinator is slow (LLM inference), there is no queue size limit — Commands accumulate in memory.

*Fix:* Replace direct `asyncio.create_task()` with `asyncio.Queue(maxsize=10)` between FusionEngine and HybridCoordinator. Drop (and log at WARNING) when the queue is full. Location: [`fusion_engine.py`](../fusion_engine.py), [`hybrid_coordinator.py`](../hybrid_coordinator.py).

**3. Large monolithic dispatch methods**

- `ipad_bridge.py::_handle_message()` is ~400 lines handling 13 message types via if-elif
- `command_executor.py::_dispatch()` is ~200 lines handling 16 verbs via if-elif

Both make unit testing impossible without sending real WebSocket messages.

*Fix:* Replace with dispatch dicts: `HANDLERS: dict[str, Callable] = {msg_type: handler_fn, ...}`. Each handler becomes a small, independently testable method.

### Moderate — Limit adaptability

**4. Incomplete backend stubs**

- `VLLMInference` — stub, returns `NotImplementedError` (task 2.13)
- `NemotronInference` — stub
- Amazon Transcribe Gate 1 re-transcription fallback — `pass`
- `boto3` not in `requirements.txt` (Bedrock cloud path is entirely untested)

**5. Hardcoded configuration**

| Hardcoded value | Location | Risk |
|-----------------|----------|------|
| `localhost:11434` (Ollama endpoint) | `local_inference.py` | Cannot run Ollama on separate machine |
| `large-v3` (Whisper model) | `whisper_stream.py` | Cannot swap to medium for lower VRAM |
| `0.015` RMS (VAD silence threshold) | `whisper_stream.py` | Quiet environments need different tuning |
| Model names per domain | `model_router.py` | No fallback if a model is not pulled |

*Fix:* Read from env vars with defaults: `OLLAMA_HOST`, `WHISPER_MODEL`, `VAD_THRESHOLD`.

**6. DevAgent step-parsing fragility**

The regex `\[ACTION arg\] body` for parsing model output is brittle — any change to the model's output format breaks it silently (steps are skipped, not errored).

*Fix:* Validate parsed step count > 0; fall back to a clarification reply if 0 steps parsed. Add retry with a simplified prompt on parse failure.

### Low — Technical debt

| Issue | Location |
|-------|----------|
| No unit tests for core gate logic | FusionEngine, HybridCoordinator gates, DomainClassifier scoring |
| No protocol versioning between iPad ↔ PC | `ipad_bridge.py` message type routing |
| No schema migration strategy for AgentDB | `db.py`, `migrate.py` |
| MiniLM lazy-loads on first few-shot query (+2–4 s latency) | `db.py::AgentDB` |

---

## Decision

The architecture is **sound and should not be restructured**. The separation of concerns, Command DTO, gate semantics, and async discipline are all correct and should be carried forward unchanged into production.

The critical risks above are not design flaws — they are incomplete edges that were intentionally deferred during skeleton phase. They must be closed before this system is used as a daily accessibility driver.

---

## Action Items (priority order)

1. [ ] Add `asyncio.wait_for(timeout=30)` to `OllamaInference.infer()` — [`local_inference.py:~130`](../local_inference.py)
2. [ ] Add `asyncio.wait_for(timeout=60)` per DevAgent step — [`dev_agent.py:~200`](../dev_agent.py)
3. [ ] Add bounded `asyncio.Queue(maxsize=10)` between FusionEngine and HybridCoordinator — [`fusion_engine.py`](../fusion_engine.py), [`hybrid_coordinator.py`](../hybrid_coordinator.py)
4. [ ] Refactor `_handle_message()` to dispatch dict — [`ipad_bridge.py:~80`](../ipad_bridge.py)
5. [ ] Refactor `_dispatch()` to verb→handler dict — [`command_executor.py:~60`](../command_executor.py)
6. [ ] Implement `VLLMInference` (task 2.13) — [`local_inference.py`](../local_inference.py)
7. [ ] Add env-var overrides: `OLLAMA_HOST`, `WHISPER_MODEL`, `VAD_THRESHOLD`
8. [ ] Add unit tests: DomainClassifier scoring, HybridCoordinator gate evaluation, few-shot Jaccard scoring — new `tests/unit/`
9. [ ] Add `schema_version` table to AgentDB + migration helper in [`migrate.py`](../migrate.py)
10. [ ] Pre-load MiniLM at startup alongside Whisper to avoid cold-start latency — [`main.py`](../main.py)
11. [ ] Add `boto3` to `requirements.txt` and implement real Bedrock inference — [`hybrid_coordinator.py`](../hybrid_coordinator.py)

---

## Consequences

**What becomes easier:**
- Production incident diagnosis — timeouts surface instead of hanging the loop
- Performance tuning — env-var thresholds don't require code edits
- Adding new LLM backends — VLLMInference stub becomes a real implementation path
- Unit testing gate logic independently of the WebSocket server

**What becomes harder:**
- Nothing. These are pure risk-reduction changes with no architectural trade-offs.

**What to revisit:**
- After VLLMInference ships: re-run [`benchmark_models.py`](../benchmark_models.py) to compare vLLM vs Ollama p50/p95 on RTX 5090.
- After unit test suite exists: add to GitHub Actions CI alongside the existing build workflow.
