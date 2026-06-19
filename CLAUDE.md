# Personal Desktop Agent

@AGENTS.md

<!-- ^ Shared cross-tool behavior rules (also read natively by Antigravity).
     Keep behavioral rules in AGENTS.md, not here, so both IDEs stay in sync. -->

Multimodal accessibility desktop control for a single user with rheumatoid arthritis. An iPad Pro (2020+) is the sensor hub and primary touch surface; a Windows PC with RTX 5090 runs inference and executes desktop actions.

## What This Is

The user controls a Windows desktop through voice, hand gesture, iPad tilt, and direct touch — all mapped to a 16-verb action vocabulary (11 accessibility + 5 dev-agent). (Eye-gaze and head-pose control were removed — the standard iPad lacks the required TrueDepth sensor.) Sensor data streams over WebSocket from a native Swift iPad app to a Python backend on the PC. The PC runs local LLM inference (Ollama → vLLM in production) and executes commands via pyautogui/Win32.

- Full requirements (17): `specs/ipad-sensor-focus/requirements.md`
- Architecture diagrams (13): `specs/ipad-sensor-focus/diagrams/00-index.md`
- Tech stack: `specs/steering/tech.md`
- Open tasks: `specs/ipad-sensor-focus/tasks.md`
- Daily reviews: `docs/daily/`

## Current Status (2026-06-16)

> **Schema fact (authoritative):** `agent.db` = **42 tables** at `PRAGMA user_version = 7` (`storage/db.py` is the schema source of truth); `AnalyticsDB` (DuckDB) holds the **3** `benchmark_*` tables. Table counts quoted in `docs/CHANGELOG.md` are historical (as-of-their-date), not current.

Phases 1–6 + Sprints A–C / 5–7 / G1–G5 / N–Q shipped and merged. Current capability spans
the iPad sensor pipeline; the voice/audio stack + behavioral eval harness (`evals/`);
MCP-connector skills (notes / arxiv / weather / files / diagrams / pain-journal, plus
Gmail+Calendar); proactivity (scheduler + event rules + notifier); the agent substrate
(observer agents, episodic memory, offline self-evolution); and error-handling hardening
(EH-1–4). **Full dated history → [`docs/CHANGELOG.md`](docs/CHANGELOG.md). Day-by-day notes → `docs/daily/`.**

## Run Commands

```bash
# Full pipeline — bridge + FusionEngine + HybridCoordinator + ContinuousTrainer
python main.py [--port 8765] [--host 0.0.0.0] [--no-mdns] [--debug] [--safe-mode] [--viewer] [--viewer-only]

# Measure actual VRAM usage on RTX 5090 (loads all models, prints table, exits)
python main.py --measure-vram

# MCP server — Claude's desktop control interface (stdio transport)
python mcp_server/desktop_mcp_server.py

# iPad WebSocket bridge (standalone, without FusionEngine)
python ipad_bridge.py [--port 8765] [--no-mdns] [--debug]

# End-to-end integration test (start bridge first in another terminal)
python tests/test_bridge_client.py

# Install dependencies
pip install -r requirements.txt
```

Set `--safe-mode` (or `SAFE_MODE=1`) to block `keyboard_type` and `mouse_drag` during testing.

## Action Vocabulary

**Accessibility verbs (11)** — for iPad sensor pipeline and simple commands:
`CLICK` `MOUSEDOWN` `MOUSEUP` `SCROLL` `TYPE` `OPEN` `CLOSE` `HOTKEY` `DICTATE` `CLARIFY` `SCREENSHOT`

`MOUSEDOWN`/`MOUSEUP` are executed synchronously (no `asyncio.to_thread`) because they are timing-critical for drag-select and must not compete with trackpad moves.

**Dev-agent verbs (5)** — emitted by specialist models via DevAgent:
`WRITE_FILE` `RUN_TERMINAL` `EXPLAIN` `SEARCH_WEB` `READ_SCREEN`

The `CommandExecutor` handles all 16 verbs. The `DomainClassifier` determines which pipeline a query enters — accessibility (llama3.1:8b, verb-first) or dev-agent (specialist model, free-form).

## Architecture

```
iPad sensors  → WebSocket :8765 → ipad_bridge → FusionEngine → HybridCoordinator ─┐
                                                                                    │
                                               DomainClassifier                     │
                                              /               \                     │
                                       command domain       dev domains             │
                                             │           (CODE/MATH/VISION/         │
                                        llama3.1:8b       PLAN/GENERAL)            │
                                        verb-first         ModelRouter              │
                                             │            specialist LLM            │
                                             └──────────────────┘                  │
                                                      │                             │
                                               CommandExecutor                      │
                                            (16 verbs: 11 access + 5 dev)          │
                                                      │                             │
                                         mcp_server/tools/ → pyautogui / Win32 ←──┘

Claude (MCP) → stdio → mcp_server/desktop_mcp_server.py → mcp_server/tools/
```

Every pipeline boundary carries a `Command` dataclass. `DomainClassifier` gates the pipeline: simple commands go straight to `llama3.1:8b`; dev-domain queries go to `DevAgent` which selects the right specialist model.

## Key Files

| File | Purpose |
|------|---------|
| `core/ipad_bridge.py` | aiohttp WebSocket server on :8765; routes 25 incoming message types; sends `ack`, `status`, `screenshot`, `handwriting_result`, `mic_state`, `recalibration_request` replies |
| `core/command_executor.py` | Maps 16 action verbs to mcp_server tool calls; `_resolve_coords` falls back to screen centre; SCREENSHOT defaults to active window and copies to Windows clipboard |
| `mcp_server/desktop_mcp_server.py` | MCP stdio server; 14 tools; `SAFE_MODE` env var |
| `mcp_server/tools/mouse.py` | move, click, double_click, scroll, drag |
| `mcp_server/tools/keyboard.py` | type, hotkey, press, paste (unicode via clipboard) |
| `mcp_server/tools/screen.py` | screenshot (base64 PNG), get_screen_size, find_text_on_screen (OCR) |
| `mcp_server/tools/windows.py` | get_active_window, list_windows, focus_window (win32gui + psutil) |
| `mcp_server/tools/handwriting.py` | pix2tex LaTeX OCR; latex_to_unicode fallback converter |
| `core/fusion_engine.py` | 60 Hz tick loop; 7-level sensor priority; direct pyautogui for tilt (gaze/head removed) |
| `core/hybrid_coordinator.py` | 4-gate routing (Gate 0 privacy + Gates 1–4); cloud fallback via `core/cloud_backend.py` (Amazon Bedrock when `AWS_BEARER_TOKEN_BEDROCK` set, else direct Anthropic; 10s timeout circuit-breaker); local-inference circuit-breaker (`local_timeout_s`, default 15s → CLARIFY); LLM output schema validation (`_parse_action` + `_VALID_COMMAND_VERBS`; malformed verb → CLARIFY); outcome logger |
| `inference/local_inference.py` | `LocalInference` ABC; `OllamaInference` (default, ~190ms warm wall p50 / ~29ms compute, Ollama 0.30.6), `VLLMInference` (verified in Ubuntu WSL2, vLLM 0.21.0; `--backend vllm`; use `--gpu-memory-utilization 0.65` with Whisper running) |
| `adaptive/continuous_trainer.py` | Routing threshold adaptation; few-shot ranking; gesture velocity-floor calibration (p10 observed, −30% pain day); delegates all storage to `AgentDB`; holds `gesture_processor=` ref for live threshold push-back |
| `sensors/lidar_receiver.py` | Decodes depth_frame messages; confidence-map filtering; `get_depth_at()` |
| `adaptive/behavioral_twin_state.py` | Persistent user behaviour model: `TwinSnapshot`, `PreferenceModel`, `PainDayEngine`; AgentDB + ChromaDB backing; feeds `HybridCoordinator` before every gate decision |
| `storage/semantic_memory.py` | ChromaDB vector store (all-MiniLM-L6-v2, cosine space) for semantic few-shot retrieval; query results carry a `score` (=1−distance); Jaccard fallback when chromadb unavailable; time-gated `_available` re-probe (gated on `_started_once`); `stop()` releases WAL file handles on Windows |
| `sensors/one_euro_filter.py` | Casiez 2012 adaptive low-pass filter (1€); used for tilt velocity, tilt position, gaze delta, head tracking — replaces EMA throughout sensor pipelines |
| `calibration/gyro_bias_calibrator.py` | Gyro bias state machine (UNCALIBRATED→COLLECTING→CALIBRATED→FROZEN); stationary detection + lerp-smoothed bias subtraction for tilt velocity pipeline |
| `sensors/gesture_processor.py` | MediaPipe Tasks API (`HandLandmarker`, `hand_landmarker.task`); peace-sign base pose; 13 gestures (swipe/grab/snap/monitor/push-pull/pinch); 500ms rolling buffer; velocity learning; 800ms debounce |
| `core/domain_classifier.py` | Keyword-scoring domain detection: COMMAND/CODE/MATH/VISION/PLAN/GENERAL |
| `inference/model_router.py` | VRAM-aware specialist model selection; domain-tuned prompts; Ollama inference |
| `inference/dev_agent.py` | Plan→execute→reflect agentic loop; 5 dev verbs; session context. Gap A: when the planner declares step deps (`(after: N, M)`), `_run_dag_waves` runs the plan as a dependency DAG — fan-out-safe ready steps (reads/WRITE_FILE/EXPLAIN) run concurrently via `scheduler.fan_out`, barriers (RUN_TERMINAL/git/UI) run solo; first failure/cancel/unmet-dep falls back to the sequential+replan loop. Plain plans stay sequential. R-10: a `max_replans`/`max_steps` halt rolls back (saga), then `_record_escalation` persists the goal to `dev_escalations` for human review (user cancel never escalates); voice "review queue" / "clear review queue" in the coordinator |
| `main.py` | Unified entry point; `--measure-vram`; `--viewer`/`--viewer-only`; startup status table; Ctrl-C shutdown |
| `sensors/sensor_viewer.py` | tkinter desktop window (daemon thread); camera + LiDAR depth side-by-side; hand landmark overlay; freeze-frame; depth-at-cursor readout; always-on-top toggle |
| `sensors/whisper_stream.py` | GPU-accelerated speech: Silero VAD + faster-whisper large-v3; emits `Command(source="voice")` to FusionEngine |
| `storage/db.py` | `AgentDB` (aiosqlite, all pipeline writes; versioned via `PRAGMA user_version`) + `AnalyticsDB` (DuckDB, 3 benchmark tables); MiniLM semantic retrieval; gesture velocity + voice + iPad log tables; **`goal_queue`** durable goal backlog (gap D — enqueue/claim/complete/requeue_stale, idempotency_key); **`dev_escalations`** human-review backlog (R-10 — insert/get_pending/count/resolve); per-domain inference stats + `adaptation_log.domain` (gap H) |
| `tests/test_bridge_client.py` | Simulated iPad client; sends 8 test messages; verifies ack for each |
| `tts/polly_stream.py` | Python TTS client — HTTP to Node.js sidecar; `speak_sync()` for threads, `speak()` async, `speak_stream()` for token-by-token; auto-starts sidecar; `get_client(backend=)` dispatches to Chatterbox when configured |
| `tts/chatterbox_tts.py` | Local GPU TTS backend (RTX 5090); `ChatterboxClient` with same interface as `PollyStreamClient`; emotion exaggeration, paralinguistic tags, zero-shot voice cloning |
| `tts_service/server.js` | Node.js sidecar (port 8766); calls `StartSpeechSynthesisStream` (AWS SDK v3); returns OGG Vorbis; Python decodes with soundfile |
| `approval_hook.py` | Claude Code `PreToolUse` gate; Danielle speaks action description; records iPad mic via WhisperStream signal file or PC mic fallback; yes/no → exit 0/2 |
| `storage/audit_log.py` | Append-only `audit.db` (SQLite WAL); records every MCP tool invocation, session lifecycle event, and security finding; UPDATE/DELETE blocked by triggers |
| `approval_config.json` | Per-tool approval policy (`"approve"` / `"silent"`), voice, mic device (`"Microphone (Realtek USB Audio)"`), timeout, tts_backend |
| `start_agent.bat` | Windows startup script; activates venv and runs `main.py`; logs to `logs/agent_startup.log` |
| `calibration/acoustic_profiler.py` | Per-user VAD threshold + logprob floor from measured RMS/spectral-centroid/Whisper-logprob; passive calibration; drift detection; seasonal re-cal prompt; Signal 5 in PainDayEngine |
| `calibration/voice_calibrator.py` | Guided voice calibration for 3 conditions (good/flare/allergy — svt_attack removed 2026-06-11, SVT no longer a project factor); 20 phrases; voice-triggered or iPad Settings tab; writes to `voice_profile` + `voice_phrases` tables |
| `desktop/vision_grounder.py` | Local qwen3-vl:30b (Ollama) resolves named UI targets to pixel coords; claude-sonnet-4-6 cloud fallback via `core/cloud_backend.py` (Bedrock/Anthropic); confidence ≥0.7; 2s cache; fallback chain: vision → gaze → OCR → CLARIFY |
| `desktop/ui_automation.py` | Win32 UIAutomation BFS tree search; fuzzy name scoring; 0.3s timeout; 1s cache; first fallback in `_resolve_coords` |
| `desktop/action_verifier.py` | Pillow perceptual diff pre/post screenshot; verifies CLICK/OPEN/CLOSE/SCROLL; 2% pixel threshold; 400ms animation delay |
| `desktop/flick_engine.py` | Flick-to-snap gesture handler; maps GRAB_SNAP_* gestures to window snap zones; uses OneEuroFilter for smoothing |
| `desktop/target_cache.py` | `ClickableTargetCache` — daemon thread publishing a lock-protected snapshot of clickable UI targets for magnetic snap + cursor gravity; change-gated COM walk (foreground-hwnd + 1.5 s heartbeat), failure backoff, `CoUninitialize`; started behind `DA_CURSOR_GRAVITY` |
| `inference/kiro_client.py` | WebSocket client for Kiro/VS Code bridge extension on ws://127.0.0.1:8767; wired to DevAgent for code edits |
| `inference/codebase_indexer.py` | ChromaDB RAG index (cosine) over Python/Swift source + docs PDFs; accepts `embedder=`; oversized units sub-split into `_(i/N)` chunks (no 4000-char truncation); per-path debounced file watcher; time-gated `_available` re-probe; fed to DevAgent for context |
| `monitoring/metrics.py` | In-process metrics singleton; VRAM poller; optional `/metrics` HTTP endpoint |
| `monitoring/trace.py` | Cross-layer per-command tracing (enqueue→dispatch→route→inference→execute). **On by default** (2026-06-19 observability batch); opt out with `DA_TRACE=0`. Spans recorded at command boundaries (not the 60 Hz tick), flushed to `command_traces` fire-and-forget after each command; pruned 30 days (`prune_command_traces`). Default-on means every command carries a `trace_id`, so `replay.py` works for all commands |
| `monitoring/replay.py` | **new file** (observability batch); `replay_trace(trace_id)` assembles commands + command_traces + inferences + event_log + audit_events into one ts-sorted timeline. Read-only stdlib sqlite3 (`mode=ro`), never writes. CLI: `python -m monitoring.replay <trace_id>` / `--recent N` / `--json` |
| `monitoring/trends.py` | **new file** (observability batch); cross-session trend report over `session_summaries` — success/cloud/p50/p95/pain-day per session + recent-vs-older deltas (polarity-aware improving/worsening/flat). CLI: `python -m monitoring.trends [--db --limit --json]` |
| `monitoring/cost_ledger.py` | **new file** (observability batch); rolls up cloud (Bedrock) token spend from `inferences` by model/day/session against a `$/MTok` price table (env-overridable `DA_BEDROCK_PRICES` / `DA_BEDROCK_PRICE_MULT`); local models = $0. Cloud *dev* path (Opus) usage now persisted too (CloudDevAgent → coordinator). CLI: `python -m monitoring.cost_ledger [--db --days --json]` |
| `storage/session_analyzer.py` | Post-session DuckDB analytics; route distribution, latency percentiles, error modes; summary persisted to AgentDB |
| `core/chat_server.py` | PC desktop chat UI server (`--chat`, aiohttp :8770, localhost). Chat + live DAG via EventBus keyed by trace_id. **Also hosts the unified observability dashboard** (`/dashboard` + read-only `/api/metrics`, `/api/recent-traces`, `/api/replay/{tid}`, `/api/trends`, `/api/cost`): live "Now" KPIs + activity feed (ops events broadcast to all clients via the event pump), plus Traces/Trends/Cost panels that wrap `monitoring/replay,trends,cost_ledger`. Frontend in `web_client_chat/` (`dashboard.html`/`dashboard.js`) |
| `core/scheduler.py` | `AccessibilityScheduler` — priority queue over `coordinator.route()`; 5 tiers (ACCESSIBILITY/VOICE/GESTURE concurrent, DEV_AGENT/BACKGROUND semaphore-gated); `fan_out()` runs independent sub-steps under a separate N=3 `_subagent_sem` (gap #1, deadlock-free vs `_dev_sem`; records a `fan_out` trace span, gap C); `pause_dev()`/`resume_dev()` flare admission gate (gap #3); bounded queue (256) with priority-aware load-shedding of DEV/BACKGROUND only (gap #4); parked dev tasks capped at 16 during a flare, excess shed (gap F); `is_healthy()`/`restart()` for the Supervisor (gap #2); wired in `FusionEngine._emit()` |
| `core/supervisor.py` | **new file**; one-for-one liveness watchdog (gap #2). Polls `is_healthy()` per subsystem; restarts a dead-but-`enabled()` loop under a bounded policy (≤5 restarts/60s → latch FAILED). On latch, fires `on_failed(name)` (gap E) → main.py speaks a TTS warning and degrades (scheduler FAILED → `fusion.set_scheduler(None)` direct dispatch, accessibility unaffected). Supervises scheduler worker + governor loop; registered last so it stops first |
| `core/crash_marker.py` | Process-level unclean-exit detection: `check_and_mark()` writes `logs/agent.running` at pipeline start (returns True if the previous run crashed → TTS "I restarted after a crash"); `clear()` removes it as the LAST step of graceful shutdown. Complements `scripts/agent_watchdog.ps1` (restart-on-crash loop, 3×/10 min bound, exit-0 = intentional stop) + `scripts/Install-AgentService.ps1` (registers `PersonalDesktopAgent` + `PersonalDesktopAgent-Proxy` logon tasks; `-Uninstall` to remove) |
| `core/circuit_breaker.py` | **new file**; latched failure breaker (gap #4) — closed→open after N consecutive failures, fast-fails for `cooldown_s`, half-open probe on recovery. Injectable clock. Wired into `OllamaInference` so a down backend stops costing a full timeout per call |
| `core/vram.py` | **new file**; single shared GPU-VRAM *signal* (gap #3) — `free_vram_gb()`/`used_vram_gb()`, 999.0 fail-open. `ModelRouter._free_vram_gb` delegates here so the signal can't drift between callers |
| `core/vram_arbiter.py` | **new file**; single source of VRAM admission *policy* (gap B) — `VramArbiter.can_admit(vram_gb, free_gb)` (the former inline `<= free + 2.0` tolerance) + `headroom_gb`, fail-open when unmeasurable. `ModelRouter.select_profile`/`get_status` route through it (admission control before dispatch) |
| `core/slo.py` | **new file**; per-domain routing SLOs (gap H) — `SLOConfig` (per-domain latency budget + success floor; command=600ms), `evaluate(p50,success)→verdict`, `estimate_difficulty()` (AVR-lite). `CoordinatorConfig.slo` + Gate-4 `latency_budget_for(domain)`; `ContinuousTrainer._adapt_per_domain_slo()` logs per-domain breaches to `adaptation_log(domain)` + sets Gate-4 overrides. Learned classifier deferred (data-blocked) |
| `inference/sandbox.py` | **new file**; WSL2 namespace jail for RUN_TERMINAL (sandbox) — `run_sandboxed()` wraps the shell command in bubblewrap/firejail (cwd-jail, `--unshare-net`, RLIMIT_CPU/AS, output cap) when available, else graceful unsandboxed fallback. `DA_SANDBOX` flag (default on). Threat model = mistake-containment behind the G allowlist, not adversarial isolation. Wired into `DevAgent._run_terminal` + `command_executor` RUN_TERMINAL. Needs `apt install bubblewrap` in WSL to engage |
| `storage/memory_manager.py` | `MemoryManager` — syscall façade over AgentDB + SemanticMemory: `read_context`/`write_state`/`search_semantic` + zero-copy `get_pain_day_active/score()`; `_VALID_KEYS` schema validation at write boundaries |
| `core/resource_governor.py` | Pain-aware kernel primitive; polls `MemoryManager.get_pain_day_score()` every 5s; on flare relaxes sensor thresholds, pauses indexer, raises Whisper VAD thread priority, evicts `qwen3-vl:30b` from VRAM, AND pauses scheduler dev/background admission (gap #3 — `set_scheduler()`); reverses on recovery (hysteresis 0.6/0.4); `is_healthy()`/`restart()` for the Supervisor |
| `core/goal_session.py` | Goal-level authorization; one voice approval lets the goal's tool calls / DevAgent steps run silently; atomic-replace signal file `~/.claude/approval/goal_session.json` shared with `approval_hook.py`; `allows_action(tool, input)` adds path-scope + **deny-by-default Bash allowlist** (gap G — every segment must run a known-safe exe; `python -c` inline code, `pytest && rm -rf`, `curl\|sh`, force-push all require explicit approval; high-risk denylist kept as defense-in-depth); voice cancel/status/history. RUN_TERMINAL isolation now handled by `inference/sandbox.py` |
| `core/cluster_health.py` | `ClusterHealthMonitor` — polls laptop service nodes (`laptop_ollama`/`whisper`/`indexer`); synchronous zero-cost `is_healthy()` for hot-path routing; fail-safe to local when unknown/down |
| `core/cluster_config.py` | `ClusterConfig` — laptop compute-node endpoints + offload policy (loads `cluster_config.json`); consumed by `model_router.py` and `cluster_health.py` |
| `core/async_utils.py` | `fire_and_log` — safe fire-and-forget for non-critical background DB writes (strong ref until done, DEBUG-logs exceptions, no-ops without a running loop) |

## Polly TTS Voice

**Current voice: Danielle** (en-US, Generative engine, 24 kHz)

Danielle is the only en-US female voice that supports both the Generative engine (bidirectional streaming sidecar — lowest latency, most natural prosody) and the Long-form engine (batch path — best for multi-paragraph responses).

### Changing the voice

One line in `approval_config.json`:
```json
"voice_id": "Danielle"
```
Takes effect immediately — no restart required. The sidecar reads the voice from each POST request.

### Available voices (en-US, verified 2026-05-15)

| Voice | Gender | Generative | Long-form | Notes |
|-------|--------|-----------|-----------|-------|
| **Danielle** | Female | ✅ | ✅ | Current — most capable, both engines |
| Ruth | Female | ✅ | ✅ | Previous default |
| Joanna | Female | ✅ | — | Professional; Alexa-adjacent |
| Salli | Female | ✅ | — | Upbeat, clear |
| Matthew | Male | ✅ | — | |
| Stephen | Male | ✅ | — | |
| Gregory | Male | — | ✅ | Long-form only (was original default) |

### TTS paths and engines

| Path | Engine | Voice source | When |
|------|--------|-------------|------|
| `tts/polly_stream.py` → `tts_service/server.js` | Generative 24kHz | `approval_config.json` → POST body | CLARIFY questions, DevAgent EXPLAIN |
| `tts/chatterbox_tts.py` (via `polly_stream.get_client()`) | Local GPU | exaggeration/cfg in `approval_config.json` | When `tts_backend == "chatterbox"` |
| `approval_hook.py` `_polly_speak()` | Neural 16kHz | `approval_config.json` `voice_id` | "Approve write to…?" gate |
| `core/command_executor.py` `_polly_speak()` | Neural 16kHz | `_POLLY_VOICE` constant | Sidecar-down fallback |

### iPad mic approval flow

When the bridge is running, Danielle's question plays through PC speakers, then
the next utterance into the **iPad mic** is captured by WhisperStream and routed
to the approval gate via `~/.claude/approval/pending` + `response` signal files.
If the bridge is not running, the PC's **Microphone (Realtek USB Audio)** mic is
used instead (4-second recording window, auto-approve on silence).

## WebSocket Protocol

Gaze and head-pose message types (`gaze`, `gaze_delta`, `gaze_dwell`, `gaze_ray`, `gaze_calibration_sample`, `gaze_calibration_start`, `head_pose`) were removed — the standard iPad has no TrueDepth sensor.

**iPad → PC:**
- *Sensor streams:* `tilt`, `tilt_position`, `tilt_tap`, `tilt_ratchet`, `keyword`, `audio_stream`, `camera_frame`, `depth_frame`
- *Direct control:* `touch_command`, `trackpad`, `handwriting_image`, `dwell_click`, `ping`
- *Settings/UX:* `set_dwell_action`, `set_feature_toggle`, `sensor_switch`, `cursor_pause`, `cursor_resume`, `gesture_assessment`, `pain_day_override`, `flare_profile`, `calibration_start`, `calibration_cancel`, `mic_mute`
- *Diagnostics:* `ipad_log`

**PC → iPad (6 types):** `ack` (every message), `status` (window + cursor after each command), `screenshot` (base64 PNG after SCREENSHOT action), `handwriting_result` (LaTeX + unicode after handwriting_image), `recalibration_request` (voice drift/seasonal re-cal trigger → QuickRecalSheet), `mic_state` (mute/unmute echo so the iPad `MicMuteIndicator` stays in two-way sync)

`touch_command` and `trackpad` bypass FusionEngine directly. `handwriting_image` is handled inline by the bridge. `audio_stream` feeds `WhisperStream` → FusionEngine priority 6. `depth_frame` and `camera_frame` are sent by `LiDARStreamer.swift` (enabled via `lidarEnabled` toggle) and routed to `LiDARReceiver` and `GestureProcessor` respectively. `set_feature_toggle` is still wired but currently has no valid features (all prior toggles were gaze features). `ipad_log` batches structured AppLogger entries; warning+ entries are persisted to `ipad_logs` AgentDB table. The remaining sensor types (tilt, keyword, etc.) are dispatched to FusionEngine.

## Sensor Priority (FusionEngine — `core/fusion_engine.py`)

6-level priority (gaze, head-pose, and mouth-sound control all removed):

1. iPad touch command — bypasses LLM entirely
2. Voice "click" keyword — clicks at the current cursor position (bypass, source `multimodal`)
3. Tilt navigation (Core Motion) — 3a absolute position, 3b legacy velocity
4. Gesture alone
5. On-device voice keyword (Speech Framework)
6. PC-transcribed voice (Whisper large-v3 on GPU)

## Coding Conventions

- All pipeline classes are `async`; blocking I/O uses `asyncio.to_thread`
- Every sensor class must degrade gracefully — wrap hardware imports in `try/except ImportError`, log a warning, never crash
- No global state outside dataclass instances; all state lives in class attributes
- `Command` is the universal DTO — never pass raw dicts across pipeline boundaries
- Log levels: DEBUG per-frame, INFO commands/routing, WARNING sensor failures, ERROR unrecoverable

## Known Gotchas

- **Voice approval gate requires an explicit confirmation word (2026-06-04 hardening).** While `approval_hook.py`'s `~/.claude/approval/pending` file exists, `WhisperStream._handle_approval_gate()` writes a response ONLY when the transcript classifies as a deliberate approve/deny (`core/approval_keywords.classify_confirmation` — single source of truth shared with `approval_hook.py`). Ambient audio / podcast speech / a stray word returns `None` → discarded, gate keeps waiting (it no longer auto-answers with garbage). Deny wins ties; utterances longer than `MAX_ANSWER_WORDS` (6) are treated as ambient. Danielle's spoken "Approve …?" question is flushed by `_check_approval_echo_guard()` (fires once when `pending` first appears → `suppress(1.0s)`) so the TTS echo — which contains the word "approve" — can't self-approve. Timeout/ambiguity/silence **fail safe to DENY**: `approval_config.json` `timeout_action` is now `"reject"` and `approval_hook._parse_response()` defaults to deny. Tests: `tests/test_approval_gate.py` (44).
- `SCREENSHOT` automatically copies the captured image to the Windows clipboard (CF_DIB via `win32clipboard`) so the user can Ctrl+V immediately. Failure to copy is non-fatal — the base64 result is still returned.
- **Domain-classifier learning (`DA_DOMAIN_LEARN`, default OFF — experimental).** With the flag unset, `DomainClassifier` is exactly the static-keyword classifier (the `router_domains` eval baseline holds — verified no-op). When on, `ContinuousTrainer._learn_domain_overlay` learns each domain's DISTINCTIVE vocabulary from its confirmed-correct few-shot examples into `domain_keyword_weights` (43rd `agent.db` table, plain `CREATE TABLE IF NOT EXISTS`, no version bump) and the classifier adds a bounded per-domain nudge (capped at `_MAX_OVERLAY_NUDGE=15`, never overrides the static scores). Rollback: if a domain's misroute rate (from the E1 `get_domain_misroutes` analyzer) rises after a learn pass, that domain's overlay is cleared and the pass marked rolled-back (mirrors the Gate-1 discipline). The `router_domains` eval is the external regression guardrail. Kept experimental/off because the correction→domain signal is indirect. Tests: `test_domain_overlay.py` (7), `test_domain_misroutes.py` (3).
- `pyautogui.typewrite` is ASCII-only — `TYPE` has this limitation and keeps it for backward compat. `DICTATE` was fixed (2026-05-07) to use `keyboard_paste()` (win32clipboard + Ctrl+V) and now supports full unicode including mathematical symbols.
- `pyautogui.FAILSAFE = True` is set globally — moving the mouse to the top-left screen corner raises `FailSafeException`. All tool wrappers inherit this behaviour.
- `find_text_on_screen` matches individual OCR words only; search phrases spanning multiple words won't match.
- Tesseract OCR must be installed system-wide for `find_text_on_screen` to function; the function returns `{"found": false, "error": "pytesseract not installed"}` otherwise.
- mDNS advertisement requires `zeroconf`; the bridge degrades gracefully without it (logs a warning, still accepts connections).
- VRAM measured 2026-05-08: baseline 8.3 GB, Whisper +4.2 GB, ~19 GB free for LLM. `llama3.1:70b` does not fit alongside Whisper.
- Default LLM is `llama3.1:8b` (4.6 GB VRAM) for the command domain. Specialist models: `qwen3-coder:30b` (code+plan, thinking ON), `deepseek-r1:8b` (math, chain-of-thought kept), `qwen3-vl:30b` (vision), `gemma3:27b` (general). `nemotron-mini` scored 25% and was removed. `gpt-oss:20b` scored 0% and was removed. `deepseek-r1:8b` reasoning output is kept for math but is incompatible with verb-first command format.

## MCP Server Registration (Claude Code)

Add to `~/.claude/claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "desktop-agent": {
      "command": "python",
      "args": ["E:/Personal_Desktop_Agent/mcp_server/desktop_mcp_server.py"]
    }
  }
}
```
