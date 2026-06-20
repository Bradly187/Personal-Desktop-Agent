# Code Analysis — Personal Desktop Agent

> **⚠️ Historical snapshot (2026-06-07) — superseded.** This is a point-in-time analysis; the
> codebase has since shipped Sprints N/O/P/Q, the audit hash-chain, Gmail OAuth, the skill model,
> proactivity, the audio/voice pipeline, and the eval harness. Counts and capability claims here
> are dated (e.g. `agent.db` is now **42 tables at v7**, not 30). For current state see `CLAUDE.md`.

> **Scope:** Documents what exists as of 2026-06-07 (master tip `657bb2c`, all five feature branches merged).
> Section 6 maps the codebase against a standard agentic-orchestration framework to identify solid,
> partial, and absent capabilities. No implementation changes are proposed here.

---

## Section 1 — Project Overview

### Purpose

Single-user multimodal accessibility desktop control for a user with rheumatoid arthritis (RA). The system
translates physical sensor inputs — voice, hand gesture, iPad tilt, and direct touch — into desktop actions on
a Windows PC, removing the need for sustained keyboard/mouse use. A secondary "dev-agent" mode exposes
a five-verb code-editing vocabulary driven by local and cloud LLMs.

### Hardware

| Component | Role | Specs |
|-----------|------|-------|
| iPad Pro (2020+, home-button model) | Sensor hub & touch surface | No TrueDepth sensor — eye-gaze and head-pose pipelines removed |
| Windows PC | Inference + desktop execution | RTX 5090 (32 GB VRAM), 192 GB DRAM |
| RTX 4070 laptop | Optional offload compute node | Whisper + Indexer offload via `cluster_config.json` |

### Input Modalities

| Modality | Hardware source | Processing path |
|----------|----------------|----------------|
| Voice | iPad mic → Whisper GPU | `sensors/whisper_stream.py` → FusionEngine priority 6 |
| On-device keyword | iPad Speech Framework | FusionEngine priority 5 (skips Gate 1) |
| Hand gesture | iPad camera → MediaPipe | `sensors/gesture_processor.py` → FusionEngine priority 4 |
| Tilt / Core Motion | iPad IMU | `core/ipad_bridge.py` → FusionEngine priority 3 (direct cursor, no LLM) |
| Direct touch | iPad CommandPad | FusionEngine priority 1 (bypasses all gates) |

Eye-gaze and head-pose tracking were removed in full (PC + iPad sides) because the available iPad lacks the
required TrueDepth front camera.

### Action Vocabulary

**Accessibility verbs (11):** `CLICK` `MOUSEDOWN` `MOUSEUP` `SCROLL` `TYPE` `OPEN` `CLOSE` `HOTKEY`
`DICTATE` `CLARIFY` `SCREENSHOT`

**Dev-agent verbs (5):** `WRITE_FILE` `RUN_TERMINAL` `EXPLAIN` `SEARCH_WEB` `READ_SCREEN`

All 16 verbs are dispatched by `core/command_executor.py` to `mcp_server/tools/`.

### Codebase Status (2026-06-07)

- Phases 1–6 complete + Sprints A–C, 5–7, G1–G5
- **Python test suite:** 983 passed across 76 `tests/test_*.py` files
- **iOS tests:** 16 XCTest files (Swift)
- **Branch:** `master`, clean working tree

---

## Section 2 — Architecture Overview

### Data Flow

```
iPad sensors  →  WebSocket :8765  →  ipad_bridge  →  FusionEngine  →  HybridCoordinator
                                                                              │
                                                          DomainClassifier ───┤
                                                         /                   │
                                                   COMMAND                   │
                                               llama3.1:8b                   │
                                               (verb-first)                  │
                                                         \                   │
                                                   CODE/MATH/VISION/PLAN/GENERAL
                                                       ModelRouter           │
                                                    specialist LLM           │
                                                              └──────────────┘
                                                                     │
                                                           CommandExecutor
                                                            (16 verbs)
                                                                 │
                                                    mcp_server/tools/ → pyautogui / Win32 / UIA

Claude (MCP)  →  stdio  →  mcp_server/desktop_mcp_server.py  →  mcp_server/tools/
```

### Pipeline Stages

**1. Sensor ingestion** (`core/ipad_bridge.py`, 934 lines)
Accepts 25 WebSocket message types from the iPad. High-priority types (`touch_command`, `trackpad`) bypass
FusionEngine and execute directly. Remaining types — tilt, keyword, audio stream, gesture, LiDAR depth,
camera frame, and settings — are forwarded into the fusion loop.

**2. Sensor fusion** (`core/fusion_engine.py`, 789 lines)
60 Hz tick loop arbitrates competing sensor inputs at 6 priority levels. Tilt drives the cursor via
`pyautogui.moveRel` with no LLM involvement. Higher-level inputs (gesture, voice) are packaged into a
`Command` dataclass and emitted to the coordinator. Cursor gravity (magnetic snap) runs as a post-processing
step using `desktop/target_cache.py`.

**3. Routing** (`core/hybrid_coordinator.py`, 1333 lines)
Four sequential gates determine whether to route locally or to the Anthropic cloud fallback:

| Gate | Criterion | Fail action |
|------|-----------|-------------|
| 0 — Privacy | Sensitive data pattern in text | Force local |
| 1 — Confidence | Whisper logprob + gesture conf above floors | Low-confidence → cloud (voice repair prompt) |
| 2 — Complexity | Token count + keyword heuristic | Exceeds limit → cloud |
| 3 — VRAM | `free_vram_gb` headroom | Insufficient → cloud |
| 4 — Latency EMA | Rolling latency vs SLO budget | Over budget → cloud |

Cloud models: `claude-haiku-4-5` (command path), `claude-opus-4-8` (dev path), `claude-sonnet-4-6`
(vision grounding fallback).

**4. Inference** (`inference/model_router.py`, 1022 lines; `inference/local_inference.py`, 1149 lines)
`DomainClassifier` scores the input across six domains; `ModelRouter` selects the resident specialist.
Ollama is the default backend. An optional vLLM INT4 AWQ pool (`--vllm-pool`) reduces cold-load from
~60 s (Ollama swap) to ~3–8 s (TTL wake). Dev-domain queries go to `DevAgent` (`inference/dev_agent.py`,
1445 lines) which executes a plan→execute→reflect loop with DAG fan-out for independent steps.

**5. Execution** (`core/command_executor.py`, 599 lines)
Maps each of the 16 action verbs to `mcp_server/tools/` calls. `_resolve_coords` falls back through a
chain: UIAutomation BFS → vision grounder (local qwen3-vl:30b / cloud Sonnet) → OCR → cursor + CLARIFY.
`MOUSEDOWN`/`MOUSEUP` run synchronously (no `asyncio.to_thread`) because they are timing-critical.

**6. Continuous learning** (`adaptive/continuous_trainer.py`, 395 lines; `adaptive/behavioral_twin_state.py`,
822 lines)
After each command: gesture velocity calibration (p10 floor per gesture, −30% on pain days), routing
threshold adaptation, few-shot ranking, and pain-day score update. All writes delegate to `AgentDB` via
`storage/memory_manager.py`.

### Supporting Systems

| Concern | Key modules |
|---------|------------|
| Resource governance | `core/resource_governor.py`, `core/vram_arbiter.py`, `core/circuit_breaker.py` |
| Scheduling | `core/scheduler.py` — 5 priority tiers, `fan_out()` for DAG sub-steps |
| Supervision | `core/supervisor.py` — one-for-one liveness watchdog, ≤5 restarts/60 s policy |
| Storage | `storage/db.py` (AgentDB 30-table SQLite + AnalyticsDB DuckDB) |
| Vector memory | `storage/semantic_memory.py` — ChromaDB cosine, all-MiniLM-L6-v2 |
| Audit | `storage/audit_log.py` — append-only SQLite, UPDATE/DELETE blocked by triggers |
| Observability | `monitoring/metrics.py`, `monitoring/trace.py` (opt-in `DA_TRACE`), `monitoring/session_analyzer.py`, `monitoring/dashboard.py` |

---

## Section 3 — Module Inventory

Line counts are from `wc -l` on the live files (2026-06-07).

### `core/` — Pipeline backbone (17 modules)

| Lines | Module | Purpose |
|-------|--------|---------|
| 1333 | `hybrid_coordinator.py` | 4-gate local/cloud routing; gate decisions; cloud fallback |
| 934 | `ipad_bridge.py` | aiohttp WebSocket server :8765; 25 message types |
| 789 | `fusion_engine.py` | 6-level priority sensor fusion at 60 Hz |
| 599 | `command_executor.py` | 16-verb dispatcher → mcp_server/tools/ |
| 470 | `scheduler.py` | 5-tier priority queue; `fan_out()` for parallel sub-steps |
| 347 | `resource_governor.py` | Pain-aware VRAM/model eviction; pauses scheduler on flare |
| 287 | `goal_session.py` | Goal-level authorization; deny-by-default Bash allowlist |
| 204 | `supervisor.py` | One-for-one liveness watchdog; bounded restart policy |
| 173 | `domain_classifier.py` | Keyword-scoring domain detection (6 domains) |
| 168 | `cluster_health.py` | Laptop offload node health polling |
| 103 | `cluster_config.py` | Laptop endpoint config (`cluster_config.json`) |
| 99 | `circuit_breaker.py` | Closed→open after N failures; half-open probe on recovery |
| 94 | `slo.py` | Per-domain latency SLOs; Gate-4 budget; `ContinuousTrainer` breach log |
| 81 | `approval_keywords.py` | Single source of truth for approve/deny classification |
| 57 | `vram_arbiter.py` | VRAM admission policy; `can_admit(vram_gb, free_gb)` |
| 44 | `async_utils.py` | `fire_and_log` — safe background DB writes |
| 41 | `vram.py` | Shared VRAM signal; `free_vram_gb()` / `used_vram_gb()` |

### `inference/` — LLM and agent layer (10 modules)

| Lines | Module | Purpose |
|-------|--------|---------|
| 1445 | `dev_agent.py` | Plan→execute→reflect loop; DAG wave execution; crash ledger |
| 1149 | `local_inference.py` | `OllamaInference` + `VLLMInference` ABC implementations |
| 1022 | `model_router.py` | VRAM-aware domain→model selection; Ollama / vLLM pool |
| 894 | `codebase_indexer.py` | ChromaDB RAG indexer; chunk sub-splitting; file watcher |
| 263 | `cloud_dev_agent.py` | Anthropic API dev path (`claude-opus-4-8`) |
| 231 | `bridge_client.py` | WebSocket client for VS Code bridge :8767 |
| 198 | `sandbox.py` | WSL2 bubblewrap jail for `RUN_TERMINAL`; `DA_SANDBOX` flag |
| 177 | `voice_prompt_composer.py` | Prompt assembly from voice + context |
| 123 | `remote_indexer_service.py` | Remote RAG indexing HTTP service :9000 |
| 29 | `remote_indexer_client.py` | Client for `remote_indexer_service` |

### `sensors/` — Sensor pipelines (9 modules)

| Lines | Module | Purpose |
|-------|--------|---------|
| 893 | `whisper_stream.py` | Silero VAD + faster-whisper large-v3; wake phrase; hallucination filter |
| 700 | `gesture_processor.py` | MediaPipe HandLandmarker; 13 gestures; velocity learning |
| 575 | `sensor_viewer.py` | tkinter desktop window; camera + LiDAR; landmark overlay |
| 305 | `realsense_publisher.py` | Intel RealSense L515 depth stream (Python 3.10 sidecar) |
| 184 | `hand_pointer.py` | Hand-pointing directional cursor |
| 183 | `remote_whisper_service.py` | Offload Whisper transcription (laptop node) |
| 122 | `lidar_receiver.py` | Decodes iPad `depth_frame`; confidence-map filtering |
| 103 | `one_euro_filter.py` | Casiez 2012 adaptive 1€ low-pass filter |
| 100 | `remote_whisper_client.py` | Client for `remote_whisper_service` |

### `storage/` — Persistence (5 modules)

| Lines | Module | Purpose |
|-------|--------|---------|
| 2295 | `db.py` | `AgentDB` (30-table SQLite, versioned migrations) + `AnalyticsDB` (DuckDB) |
| 333 | `session_analyzer.py` | Post-session DuckDB analytics; route distribution; latency percentiles |
| 252 | `audit_log.py` | Append-only audit trail; trigger-protected |
| 244 | `semantic_memory.py` | ChromaDB cosine vector store; Jaccard fallback |
| 223 | `memory_manager.py` | Syscall façade over AgentDB + SemanticMemory; `_VALID_KEYS` validation |

### `adaptive/` — Online learning (4 modules)

| Lines | Module | Purpose |
|-------|--------|---------|
| 822 | `behavioral_twin_state.py` | `TwinSnapshot`, `PreferenceModel`, `PainDayEngine`; ChromaDB-backed |
| 395 | `continuous_trainer.py` | Routing thresholds; gesture velocity floors; few-shot ranking |
| 247 | `mcp_trust_classifier.py` | Trust-level classification for MCP tool calls |
| 134 | `content_filter.py` | Output content filtering |

### `desktop/` — Desktop integration (6 modules)

| Lines | Module | Purpose |
|-------|--------|---------|
| 491 | `ui_automation.py` | Win32 UIAutomation BFS; fuzzy name scoring; 0.3 s timeout |
| 401 | `flick_engine.py` | GRAB_SNAP_* → `Win+Arrow` window snapping |
| 249 | `vision_grounder.py` | qwen3-vl:30b / Sonnet fallback; named target → pixel coords |
| 210 | `target_cache.py` | Daemon thread publishing clickable targets for cursor gravity |
| 168 | `snap_zones.py` | Snap-zone geometry for cursor gravity / magnetic click |
| 153 | `action_verifier.py` | Pillow perceptual diff pre/post screenshot; 2% threshold |

### `calibration/` — Sensor calibration (3 modules)

| Lines | Module | Purpose |
|-------|--------|---------|
| 494 | `acoustic_profiler.py` | Per-user VAD threshold + logprob floor; drift detection |
| 278 | `voice_calibrator.py` | Guided 20-phrase calibration for 4 conditions |
| 195 | `gyro_bias_calibrator.py` | Gyro bias state machine; stationary detection; lerp smoothing |

### `monitoring/` — Observability (8 modules)

| Lines | Module | Purpose |
|-------|--------|---------|
| 452 | `dashboard.py` | curses TUI — live metrics + session stats |
| 441 | `benchmark_models.py` | Ollama/vLLM model benchmark; p50/p95 latency; VRAM snapshots |
| 377 | `metrics.py` | In-process metrics singleton; VRAM poller; optional `/metrics` endpoint |
| 151 | `reasoning_probe.py` | Reasoning quality scoring |
| 139 | `hard_coding_eval.py` | Harder coding evaluation harness |
| 133 | `trace.py` | Cross-layer tracing; `DA_TRACE` ContextVar; `GET /trace/{id}` |
| 131 | `coding_eval.py` | Coding task evaluation |
| 98 | `token_budget_sweep.py` | Token budget optimization sweeps |

### `tts/` — Text-to-speech (2 modules)

| Lines | Module | Purpose |
|-------|--------|---------|
| 288 | `polly_stream.py` | AWS Polly bidirectional streaming; OGG decode; Chatterbox dispatch |
| 171 | `chatterbox_tts.py` | Local GPU TTS (RTX 5090); zero-shot voice cloning |

### `mcp_server/` — MCP integration (6 modules)

| Lines | Module | Purpose |
|-------|--------|---------|
| 349 | `desktop_mcp_server.py` | MCP stdio server; 14 tools; `SAFE_MODE` env var |
| 149 | `tools/handwriting.py` | pix2tex LaTeX OCR + unicode fallback |
| 66 | `tools/screen.py` | screenshot (base64 PNG), screen size, OCR |
| 56 | `tools/windows.py` | get/list/focus windows (win32gui + psutil) |
| 47 | `tools/keyboard.py` | type, hotkey, press, paste (unicode via clipboard) |
| 46 | `tools/mouse.py` | move, click, double-click, scroll, drag |

### Root entry points

| Lines | File | Purpose |
|-------|------|---------|
| 1081 | `main.py` | Full pipeline assembly; 35+ CLI flags; startup status table; watchdog |
| 251 | `approval_hook.py` | Claude Code `PreToolUse` gate; voice approval; signal-file protocol |
| 84 | `windows_action_proxy.py` | Win32 helper for elevated desktop actions |

### iOS App (`iPadApp/DesktopAgent/`) — 40 Swift source files, 16 XCTest files

**Sensors:** `AudioStreamer`, `KeywordListener`, `OneEuroFilter`, `TiltSensor`
**Audio:** `SharedAudioSession`
**Network:** `DwellActionSyncer`, `FeatureToggleSyncer`, `ServiceDiscovery`, `WebSocketManager`
**UI (20 files):** `CommandPadView`, `CommandToast`, `ContentView`, `DwellActionToolbar`,
`DwellToolbarContainer`, `DwellProgressRing`, `FlareProfileSheet`, `GestureAssessmentSheet`,
`HandwritingCanvasView`, `MicMuteIndicator`, `OnboardingView`, `QuickRecalSheet`,
`ScreenshotOverlayView`, `SensorActivityBar`, `SensorDashboardView`, `SettingsView`,
`TiltCalibrationSheet`, `TrackpadView`, `VoiceCalibrationSheet`, `VoiceProfilingSheet`
**Design system:** `AppTheme`, `DAButton`, `DACard`, `DAConnectionBanner`, `DASectionHeader`, `DesignTokens`
**App-level:** `AppLogger`, `DesktopAgentApp`, `ScreenshotStore`, `SensorManager`, `SettingsStore`

VS Code extension: `desktop-agent-bridge/` (TypeScript — VS Code bridge on :8767)

---

## Section 4 — Data Layer

### AgentDB (`agent.db`, SQLite via aiosqlite)

Schema version 2 (`_AGENT_DB_SCHEMA_VERSION = 2`), managed by `storage/db.py` via `PRAGMA user_version`
additive column migrations. 30 tables:

| Category | Tables |
|----------|--------|
| Sessions & runs | `sessions`, `agent_runs`, `agent_steps` |
| Command pipeline | `commands` (with `trace_id`), `inferences` |
| Goals | `goal_queue` (idempotency_key; enqueue/claim/complete/requeue_stale) |
| Gesture | `gesture_samples`, `gesture_calibration`, `gesture_velocity_samples`, `gesture_velocity_calibration` |
| Few-shot & routing | `few_shot_examples`, `word_counts`, `hotwords`, `routing_thresholds` |
| Sensor telemetry | `sensor_events`, `sensor_telemetry` |
| Adaptation | `adaptation_log` (with `domain` column for per-domain SLO tracking) |
| Behavioral twin | `twin_session_history`, `twin_pain_day_log`, `settings_versions` |
| Voice & audio | `voice_calibration`, `voice_calibration_sessions`, `voice_profile`, `voice_profiles`, `voice_phrases`, `voice_pronunciations`, `ambient_transcripts` |
| Accessibility | `sensor_rom`, `flare_profile` |
| Diagnostics | `ipad_logs`, `session_summaries` |

Note: `gaze_monitor_calibration` is present in older database files as an orphaned table — the gaze
pipeline was removed in full but `AgentDB` does not run `DROP TABLE` on it.

### AnalyticsDB (`analytics.duckdb`, DuckDB)

3 columnar tables for benchmark analytics: `benchmark_runs`, `benchmark_results`, `benchmark_prompts`.
Accessed via `storage/db.py::AnalyticsDB`; separate from `agent.db`.

### ChromaDB (`chroma_db/`, HNSW cosine)

| Collection | Size | Notes |
|-----------|------|-------|
| `codebase` | 1937 chunks | Python + Swift source; sub-split at `_(i/N)` boundaries |
| `documents` | 128 pages | PDF documentation; cosine space |
| `behavioral_memory` | Rebuilt on `twin.start()` | User preference embeddings |

All three collections use `hnsw.space=cosine` (rebuilt 2026-06-07; backup at `%TEMP%\chroma_db.bak-pre-cosine`).
Embedder: all-MiniLM-L6-v2 (384-dim, sentence-transformers). Accessed via `storage/semantic_memory.py`;
Jaccard fallback when ChromaDB unavailable.

### AuditDB (`audit.db`, SQLite)

Append-only audit trail managed by `storage/audit_log.py`. SQLite triggers block `UPDATE` and `DELETE`.
Records every MCP tool invocation, session lifecycle event, and security finding. No DROP-TABLE or
hash-chaining protection (deferred — single-user home LAN scope).

### File-based state

| File | Owner | Purpose |
|------|-------|---------|
| `approval_config.json` | `approval_hook.py` | Per-tool policy, voice mic device, TTS backend |
| `cluster_config.json` | `core/cluster_config.py` | Laptop offload endpoints and policy |
| `hand_pointer_calibration.json` | `sensors/hand_pointer.py` | Cursor gravity tuning |
| `~/.claude/approval/goal_session.json` | `core/goal_session.py` | Atomic-replace goal authorization signal |
| `~/.claude/approval/pending` / `response` | `approval_hook.py` | Per-tool approval gate handshake |

---

## Section 5 — Technology Stack

### Runtime

| Component | Version | Notes |
|-----------|---------|-------|
| Python (main) | 3.12 | Primary runtime |
| Python (RealSense sidecar) | 3.10 | `sensors/realsense_publisher.py` in `.venv-realsense` |
| OS | Windows 11 Pro 10.0.26200 | |
| Timer resolution | 1 ms | `timeBeginPeriod(1)` at startup for true 60 Hz loops |

### LLM Inference

| Component | Version / ID | Notes |
|-----------|-------------|-------|
| Ollama | 0.30.6 | Default backend; ~190 ms warm wall p50, ~29 ms compute |
| vLLM | 0.21.0 | Optional; Ubuntu WSL2; INT4 AWQ specialist pool |
| Anthropic SDK | ≥0.40.0 | Cloud fallback |
| Command model | `llama3.1:8b` (4.6 GB) | Verb-first; 100% accuracy on command eval |
| Code / Plan model | `qwen3-coder:30b` (18 GB) | Thinking ON, trace stripped |
| Math model | `deepseek-r1:8b` (4.9 GB) | FP16; chain-of-thought kept |
| Vision model | `qwen3-vl:30b` (19 GB) | Multimodal screen grounding |
| General model | `gemma4:12b` (9.1 GB) | Co-resides with command + Whisper |
| General flare fallback | `gemma4:e4b-it-qat` (+5.1 GB) | Pain-day low-resource alternative |
| Cloud command fallback | `claude-haiku-4-5` | 10 s circuit-breaker |
| Cloud dev path | `claude-opus-4-8` | `CloudDevAgent` |
| Cloud vision fallback | `claude-sonnet-4-6` | `vision_grounder.py` secondary |

### Speech

| Component | Version | Notes |
|-----------|---------|-------|
| faster-whisper | 1.2.1 | Local GPU transcription, large-v3 model |
| Silero VAD | — | Pre-filter before Whisper |
| AWS Polly | SDK v3 | Danielle neural, Generative engine 24 kHz; Node.js sidecar |
| Chatterbox TTS | 0.1.3 | Local GPU TTS alternative; zero-shot voice cloning |

### Gesture & Vision

| Component | Version | Notes |
|-----------|---------|-------|
| MediaPipe | 0.10.35 | HandLandmarker Task API; peace-sign base pose; 13 gestures |
| pix2tex | 0.1.4 | LaTeX OCR for handwriting canvas |
| pytesseract | 0.3.13 | OCR fallback in `_resolve_coords` |
| Pillow | 12.2.0 | Perceptual diff for `action_verifier.py` |
| mss | 10.2.0 | Fast screen capture |

### Desktop Automation

| Component | Version | Notes |
|-----------|---------|-------|
| pyautogui | 0.9.54 | Mouse/keyboard; `FAILSAFE=True` |
| pywin32 | 311 | Win32 API |
| comtypes | ≥1.4 | UIAutomation COM for BFS tree walking |

### Networking & MCP

| Component | Version | Notes |
|-----------|---------|-------|
| aiohttp | 3.14.0 | WebSocket server :8765 |
| zeroconf | 0.149.16 | mDNS iPad discovery |
| websockets | 16.0 | VS Code bridge :8767 |
| MCP | 1.27.0 | stdio transport for Claude integration |

### Storage

| Component | Version | Notes |
|-----------|---------|-------|
| aiosqlite | 0.22.1 | Async SQLite driver |
| duckdb | 1.5.2 | Columnar analytics |
| chromadb | 1.5.9 | HNSW vector store (server mode only for CVE gate) |
| sentence-transformers | 5.4.1 | all-MiniLM-L6-v2 embedder |

### iOS App

| Component | Version / Config | Notes |
|-----------|-----------------|-------|
| Swift / SwiftUI | 5.10 | Primary app language |
| iOS SDK | 18.5 | Minimum deployment target |
| Xcode CI | 16.4 on macOS 15 | GitHub Actions |
| ARWorldTrackingConfiguration | — | LiDAR depth, `.smoothedSceneDepth` |
| AVFoundation | — | Audio streaming, keyword listener |
| Core Motion | — | Tilt / accelerometer |
| Network.framework | — | WebSocket client |

### Monitoring & Dev Tools

| Component | Version | Notes |
|-----------|---------|-------|
| nvidia-ml-py | 13.595.45 | pynvml replacement; Gate 3 VRAM check |
| psutil | 7.2.2 | System metrics |
| windows-curses | 2.4.1 | TUI dashboard on Windows |
| watchdog | 6.0.0 | File watcher for incremental RAG indexing |
| pytest-asyncio | 1.3.0 | Async test runner |

---

## Section 6 — Orchestration Pattern Analysis

This section maps the codebase against a standard agentic-orchestration framework. Each pattern is rated:

- **Solid** — fully implemented, tested, in production use
- **Partial** — implemented but with known gaps or incomplete coverage
- **Absent** — not implemented; capability does not exist in any form

---

### 6.1 Coordination Patterns

#### Central Orchestrator (Conductor)

**Status: Solid**

`HybridCoordinator` is the conductor. Every `Command` from FusionEngine passes through it before
execution. It holds the gate decision logic, selects the inference path (local vs. cloud), delegates to
`DevAgent` for multi-step plans, and logs the outcome. There is one and only one entry point for routing
decisions.

`AccessibilityScheduler` (`core/scheduler.py`) adds priority-queue semantics on top of the coordinator:
five tiers (ACCESSIBILITY, VOICE, GESTURE run concurrently; DEV_AGENT, BACKGROUND are semaphore-gated).
`DevAgent._run_dag_waves` provides fan-out for plan steps that are declared independent — these run
concurrently under `scheduler.fan_out()`.

**Gaps:**
- The conductor has no explicit "plan registry" that an external caller can inspect mid-flight to see
  which step is currently executing. The `agent_runs`/`agent_steps` tables record this after the fact
  but there is no live query API.
- `DevAgent` replanning is capped at `MAX_REPLANS=2` with no mechanism to escalate beyond that to a
  human or a different strategy.

#### Choreography (Event-Driven)

**Status: Partial**

The sensor layer is loosely event-driven: each sensor posts messages into the WebSocket server
independently; `ipad_bridge.py` dispatches them without a central coordinator. `FusionEngine`'s 60 Hz
tick loop collects from multiple queues and arbitrates — this is choreography within the sensor tier.

At the pipeline level, however, there is no pub/sub event bus. The voice-drift recalibration trigger
(`bridge.send_recalibration_request()`) is the closest thing to a choreographed downstream action
(PC detects drift → sends `recalibration_request` → iPad opens `QuickRecalSheet`). But this is a point-
to-point RPC, not a topic-based fan-out.

**Gaps:**
- No event bus or message broker. Adding a new "watcher" (e.g., an alerting agent that fires on repeated
  CLARIFY outcomes) requires wiring it directly into the coordinator, not subscribing to a topic.
- No replay capability for sensor events — `sensor_events` is written but there is no tool to replay a
  recorded session through the pipeline.

---

### 6.2 Capability Design

#### Structured Tool Interface

**Status: Solid**

`mcp_server/tools/` implements the tool layer with consistent signatures. Each tool function has typed
input parameters, returns a dict with predictable keys, and is registered with the MCP server. `SAFE_MODE`
blocks destructive tools globally.

`approval_config.json` maps each tool name to `"approve"` or `"silent"` — this is a per-tool approval
policy equivalent to the `requires_approval` field in a standard tool interface.

`core/circuit_breaker.py` implements the latched-failure breaker: closed → open after N consecutive
failures, fast-fails for `cooldown_s`, half-open probe on recovery. Wired into `OllamaInference`.

**Gaps:**
- No per-tool `timeout` enforced at the tool level itself. Timeouts live in the coordinator's local
  circuit-breaker (`local_timeout_s`, default 15 s) and in the cloud path (10 s). Individual MCP tool
  calls have no independent timeout.
- No per-tool `idempotency_key` at the tool-call layer. `goal_queue` has idempotency keys for goal-level
  operations but individual `WRITE_FILE` or `RUN_TERMINAL` calls have no such guarantee.
- `max_retries` is implicit (circuit breaker N-failures) rather than a first-class per-tool config.

#### Multi-Agent Communication / Shared State

**Status: Solid**

`Command` is the universal DTO — the invariant that no raw dict crosses a pipeline boundary is enforced
by convention and tested. `trace_id` rides `Command` and a ContextVar through the full stack
(coordinator → router → inference → executor), enabling cross-layer trace reconstruction via `GET /trace/{id}`.

`AgentDB` functions as the shared event log: every command, routing decision, gate label, and plan step
is written as an immutable record. The `adaptation_log` table captures per-domain SLO breaches.

**Gaps:**
- The event log is written append-only but there is no formal event-sourcing replay. You can query history
  but cannot re-drive the pipeline from a recorded log.
- Agents do not communicate with each other directly. All inter-agent state flows through `AgentDB` or
  signal files — there is no agent-to-agent message passing.

---

### 6.3 State Management

#### Saga / Compensation Pattern

**Status: Partial**

`DevAgent` implements a plan→execute→reflect loop with crash recovery (`mark_interrupted_runs` on startup
reconciles any plan marked `running` in `agent_runs`). This covers forward progress and crash reconciliation
but not compensation (reverse-order rollback on failure).

`goal_queue` provides durable goal backlog with `requeue_stale` (stale in-progress goals are requeued on
startup). Idempotency keys prevent duplicate execution.

`action_verifier.py` detects whether a CLICK/OPEN/CLOSE/SCROLL actually changed the screen (2% pixel
threshold, 400 ms delay). A failed verification result is returned to the coordinator but does not
automatically trigger a compensation action.

**Gaps:**
- No reverse compensation. If `DevAgent` writes a file, runs a terminal command, and then the next step
  fails, there is no automatic rollback of the written file or undoing of the terminal output.
- `MAX_REPLANS=2` exhaustion causes a silent CLARIFY rather than an escalation to a human-review queue.
- Saga state is implicit in `agent_runs.status` — there is no explicit saga state machine with named
  phases and registered compensators.

#### Checkpoint System

**Status: Solid**

`agent_runs` + `agent_steps` in `AgentDB` form the checkpoint journal. On startup, `main.py` calls
`mark_interrupted_runs` to reconcile crashed plans. `goal_queue` provides a durable backlog that survives
restarts. `ContinuousTrainer` drains gesture velocity samples after each success, preventing data loss on
crash.

`BehavioralTwinState` persists `TwinSnapshot` to `AgentDB` after each session, providing a resumable
user-model state across restarts.

---

### 6.4 Integration with Engineering Tools

#### Tool Dependency Graph / API Gateway

**Status: Partial**

`mcp_server/desktop_mcp_server.py` functions as the API gateway for desktop actions — normalizing tool
outputs, enforcing `SAFE_MODE`, and routing to typed tool modules. `command_executor.py`'s `_resolve_coords`
fallback chain (UIAutomation → vision → OCR → cursor) is an implicit dependency graph for coordinate
resolution.

`cluster_config.py` + `cluster_health.py` add a second integration tier: Whisper and the codebase indexer
can be offloaded to a laptop node, with `is_healthy()` gating the routing decision.

**Gaps:**
- No rate limiting or backoff at the tool level. If `pyautogui` or Win32 calls fail repeatedly, the
  circuit breaker catches inference failures but not desktop-action failures.
- No caching layer for expensive tool outputs (e.g., vision grounder results are cached 2 s per target,
  UIAutomation results 1 s, but there is no shared cache policy object).
- `remote_indexer_service.py` exposes plaintext `0.0.0.0:9000` with no authentication — intentionally
  deferred for single-user home LAN scope.

#### Example: PR-Review-Style Workflow Mapping

The orchestration framework describes a PR review pipeline: Code Analysis → Test → Security → Aggregate →
Gate → Notify. The Personal Desktop Agent has analogous capabilities:

| PR review stage | Equivalent in this codebase |
|----------------|-----------------------------|
| Code analysis | `inference/codebase_indexer.py` (RAG), `DevAgent` EXPLAIN |
| Test execution | `DevAgent` RUN_TERMINAL → `inference/sandbox.py` (WSL2 jail) |
| Security scan | `core/goal_session.py` deny-by-default Bash allowlist; `storage/audit_log.py` |
| Risk assessment | `HybridCoordinator` gate decisions + `core/slo.py` SLO evaluation |
| Human gate | `approval_hook.py` voice approval; `goal_session.py` authorization |
| Notification | `tts/polly_stream.py` Danielle TTS; iPad `CommandToast.swift` |

The system does not, however, implement an autonomous multi-step PR review workflow — these capabilities
exist individually but are not wired into a coordinated review pipeline.

---

### 6.5 Error Handling & Resilience

#### Circuit Breaker

**Status: Solid**

`core/circuit_breaker.py` is a first-class latched-failure breaker wired into `OllamaInference`. State
machine: CLOSED → OPEN after N consecutive failures → HALF_OPEN probe after `cooldown_s` → CLOSED on
recovery. Injectable clock for testability.

`HybridCoordinator` has a separate local-inference circuit-breaker (`local_timeout_s`, default 15 s → CLARIFY)
and a cloud circuit-breaker (10 s timeout → CLARIFY). These are timeout-based, not failure-count-based.

#### Graceful Degradation

**Status: Solid**

Degradation is layered throughout:

| Component | Primary | Fallback(s) |
|-----------|---------|-------------|
| Coordinate resolution | UIAutomation BFS | Vision grounder → OCR → cursor + CLARIFY |
| Vision grounder | qwen3-vl:30b (local) | claude-sonnet-4-6 (cloud) |
| TTS | Chatterbox (local GPU) | AWS Polly → `_polly_speak()` direct (sidecar down) |
| Whisper | Local GPU large-v3 | Laptop-offload remote Whisper |
| RAG embedder | VLLMEmbedder | all-MiniLM-L6-v2 → Jaccard |
| Routing | Local LLM | Anthropic cloud (gate fail) → CLARIFY (cloud timeout) |
| General LLM | gemma4:12b | gemma4:e4b-it-qat (pain day) |
| ChromaDB | HNSW cosine | Jaccard text similarity |
| Scheduler | Full 5-tier queue | Direct dispatch (scheduler FAILED via Supervisor) |

Every hardware-dependent import is wrapped in `try/except ImportError` — sensors degrade to no-ops,
never crashes.

**Gaps:**
- `action_verifier.py` detects failed actions but the coordinator has no automatic retry-with-alternative
  strategy. A failed CLICK returns a verification result; re-routing is not automatic.
- `RUN_TERMINAL` sandbox (`inference/sandbox.py`) falls back to unsandboxed execution when bubblewrap is
  unavailable — the fallback is logged but not surfaced as a degraded-mode indicator in the startup table.

---

### 6.6 Observability & Debugging

#### Structured Trace Format

**Status: Solid**

`monitoring/trace.py` implements opt-in cross-layer tracing (`DA_TRACE` env var). A `trace_id` is
generated per command, stored in `Command.trace_id`, and propagated through a ContextVar through:
coordinator → router → inference → executor. Spans are written to `AgentDB` and queryable via
`GET /trace/{id}`. Reconstructs the full enqueue → dispatch → route_decision → inference → execute chain.

`AgentDB.commands` stores every command with `trace_id`, `gate_label`, `latency_ms`, `model_used`, and
`outcome`. `adaptation_log` stores per-domain SLO breach events.

**Gaps:**
- Tracing is opt-in (`DA_TRACE=1`) and off by default. It is not always-on with sampling — there is no
  production-safe head-based sampler that records a fraction of traces without full overhead.
- No distributed trace propagation to the iPad side. The `ipad_logs` table receives iPad AppLogger
  entries, but these are not correlated to the PC-side `trace_id` that triggered the action.
- No critical-path analysis tooling. `session_analyzer.py` reports latency percentiles per domain but
  does not identify which pipeline stage is the bottleneck for a given command.

#### Visualization

**Status: Partial**

`monitoring/dashboard.py` provides a live curses TUI with route distribution and latency metrics.
`monitoring/metrics.py` exposes a JSON `/metrics` endpoint. `monitoring/session_analyzer.py` writes
post-session DuckDB analytics.

**Gaps:**
- No DAG visualization of agent call chains. There is no graph rendering of "which agents ran for this
  request, in what order, for how long."
- No heat map of failure rates by agent type or domain — `adaptation_log` has the raw data but there
  is no aggregation view.

---

### 6.7 Summary Table

| Pattern | Status | Key Evidence |
|---------|--------|-------------|
| Central orchestrator | **Solid** | `HybridCoordinator` + `AccessibilityScheduler` |
| Choreography / event-driven | **Partial** | Sensor tier only; no event bus at pipeline level |
| Structured tool interface | **Solid** | `mcp_server/tools/`, `approval_config.json`, `circuit_breaker.py` |
| Per-tool timeout / idempotency | **Partial** | Circuit breaker exists; per-call timeout/idempotency absent |
| Multi-agent communication | **Solid** | `Command` DTO + `trace_id` ContextVar + `AgentDB` event log |
| Saga / compensation | **Partial** | Forward progress + crash recovery; no reverse rollback |
| Checkpoint / resume | **Solid** | `agent_runs/agent_steps` + `mark_interrupted_runs` + `goal_queue` |
| API gateway / tool normalization | **Partial** | `mcp_server/` normalizes; no rate limiting or shared cache policy |
| Circuit breaker | **Solid** | `core/circuit_breaker.py`; timeout breakers in coordinator |
| Graceful degradation | **Solid** | Multi-level fallback chains throughout all sensor/inference paths |
| Structured tracing | **Solid** | `monitoring/trace.py`; `DA_TRACE`; `GET /trace/{id}` |
| Visualization / dashboards | **Partial** | TUI dashboard + metrics endpoint; no DAG or heat-map view |
