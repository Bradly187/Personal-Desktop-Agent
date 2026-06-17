# Changelog — Personal Desktop Agent

Dated implementation history, relocated out of `CLAUDE.md` (which stays a tight,
always-loaded reference) to avoid context rot. Newest first. Each entry states the
state **as of its own date** — table counts and tips are historical, not current. The
authoritative current schema fact lives in `CLAUDE.md`; `storage/db.py` is the schema
source of truth. Day-by-day notes live in `docs/daily/`.

**Done (audio/voice pipeline + eval harness + pain-journal skill — 2026-06-14→15, merged):** PRs [#62](https://github.com/Bradly187/Personal_Desktop_Agent/pull/62)–[#71](https://github.com/Bradly187/Personal_Desktop_Agent/pull/71), all merged to `master` (tip `14de926`). Audio/voice: streaming neural VAD utterance segmentation (#62), iPad hardware AEC via Voice Processing I/O (#63), binary WebSocket audio frames (#64), conversational continuity — anaphora + last-action hint (`908c1bc`), bypass-gate hardening vs misrecognitions (`47f582c`). Ops/startup: migrated-column indexes built after `_migrate()` (#65), ensure-Ollama-running-before-inference (#66), logon watchdog tasks launched hidden (#67), local Windows **SAPI TTS** backend + sidecar/config path repairs (#68), `pyproject.toml` editable install (#69). New harness capabilities: **behavioral eval harness** — trajectory + LM-judge + router/gate suites, gold-corpus mining from `agent.db`, baseline-locking regression gate (#70, `evals/` package + `scripts/run_evals.ps1`); **pain-journal voice skill** — local, zero-egress (#71). Cluster offload **disabled** (`cluster_config.json` → `.disabled`, `7077fcc`); dead code removed from `kiro_client` (`d117e7e`).

**Done (audit sprints O/P/Q + hash chain — 2026-06-13, merged):** PRs #58/#59/#60/#61. Sprint O = proactivity restart-durability + idempotent goal/event scheduling (schema **v6→7**, claim-lease). Sprint P = security residuals (realpath path-scope, Google OAuth/AIza/Bearer scrub, shell-quote scan, restore dest validation, pid-aware crash marker). Sprint Q = hot-path latency + concurrency robustness (Gate-3 NVML off-loop, JSON-first replan, DAG same-path serialization, breaker probe-gen, deadline-bounded rate limiter). #61 = tamper-evident `audit.db` per-row SHA-256 hash chain. See `docs/audits/2026-06-14-audit-and-sprint-plan.md`.

**Done (Sprint N — security + hygiene — 2026-06-12):** Branch `feat/sprint-n-security-hygiene` (off `feat/dev-escalation-queue` @ `c5e220b`). Closes the unauthenticated-network-exposure risks from the 2026-06-12 gap/security analysis:
- `chore(git)` — `.gitattributes` (LF normalization; repo was already LF, so preventive); removed stale `.git/index.lock`.
- `fix(security)` **C1** — the iPad bridge now requires a pairing token (`hmac.compare_digest` at the top of `ws_handler`, before `ws.prepare`); token at `~/.claude/ipad_bridge/paired_token` (fail-closed, generated on first run, logged once at startup); accepted via `X-Agent-Token` header or `?token=`. **Locks the iPad out until the Swift app sends the token (required follow-up).** `tests/test_bridge_auth.py`.
- `fix(security)` **M2** — writable-root allowlist on `WRITE_FILE`/`RUN_TERMINAL` cwd inside `CommandExecutor` (reuses `goal_session._path_in_scope`; default `[repo root, system temp]`; configurable via `~/.claude/ipad_bridge/config.json` `writable_roots`). `tests/test_executor_writable_root.py`.
- `fix(security)` **C2** — remote indexer bearer token (`/query/*` gated, `/health` open, service fail-closed without `INDEXER_TOKEN`); RAG results fenced as untrusted DATA + size-capped + remote-only `MCPTrustClassifier` drop on HIGH. `cluster_config.json` `laptop.indexer_token`. `tests/test_remote_indexer_auth.py`. (Supersedes the "Deferred" plaintext-indexer note below.)
- `fix(security)` **H1** — `/metrics` + `/trace` bind `127.0.0.1` (was `0.0.0.0`; endpoint off by default). `tests/test_metrics_bind.py`.
- `docs` — `agent.db` table count pinned to **40** in `database-design.md`; dead `sensor_telemetry.{gaze_*,head_*}` columns tombstoned; deleted stale `.kiro/specs/enhanced-gaze-dwell/` + orphan `quick_check.py`.
- **Remaining (next sprints):** MCP-client skill model → Gmail/Calendar skill → proactivity; the iPad Swift token PR; H2/M3 Windows RUN_TERMINAL containment; diagram gaze/head/sound pruning; the **sole-copy** `kiro/specs/accessibility-agent/` tree (the analysis mislabeled it a duplicate — there is no dotted copy — confirm before deleting).

**Done (RAG/KB audit + remediation — 2026-06-07):** Branch `feat/rag-kb-remediation` (off `feat/gemma4-general-slot`), 5 commits, tip `633164d`, **merged to master**. Full suite **761 passed**.
- `fix(rag)` `5985e80` — `CodebaseIndexer(embedder=…)` kwarg + cosine pin. The missing kwarg had been **silently disabling code-RAG** on the `--index-codebase` path (TypeError swallowed by `main.py` → `indexer=None`).
- `fix(twin,memory)` `3707d22` — **the pain-day fail signal was dead**: `_session_fail_count` was the highest-weighted pain-day signal (0.30) and logged `fail_ratio` but was **never incremented**. Fixed with a separate `BehavioralTwinState.record_failure()` / `ContinuousTrainer.record_failure()` / coordinator failure branch (counters only — must NEVER touch few-shot / `PreferenceModel` / `SemanticMemory`, which are success-biased). Also rewired `MemoryManager._VALID_KEYS`, `read_context` namespacing, DevAgent `_db()` seam; `gesture_conf_delta` / `cmd_rate_delta` were hard-coded 0.0 in the twin log write — now real.
- `refactor(kb)` `3124c47` — `SemanticMemory` L2→cosine (+ `score` key); `TwinSnapshot.command_count_today` → `command_count_session` (was a per-session counter, never per-day); stale docs.
- `feat(kb)` `633164d` — versioned migrations (`AgentDB._migrate` + `PRAGMA user_version`, narrowed except); chunk sub-splitting `_(i/N)` instead of 4000-char truncation; watcher debounce; time-gated `_available` re-probe (gated on `_started_once` so never-started instances stay in fallback). Adds `tests/test_kb_robustness.py` (19).
- `docs(db)` `45c0283` — 9 Mermaid ER diagrams added to `docs/architecture/database-design.md` §11 + §12 two-tier note; fixed stale "embedding always NULL/Jaccard" and table-count claims (agent.db was 29 tables at that commit; **30 after the orchestration `goal_queue` merge** — the `database-design.md` ER diagrams still show 29 and need a goal_queue refresh).
- **chroma_db rebuilt under cosine** (backup at `%TEMP%\chroma_db.bak-pre-cosine`): `codebase`=1937 chunks, `documents`=128 pages, both `hnsw.space=cosine` (verified). `behavioral_memory` recreates under cosine on next `twin.start()`.
- **Deferred (out of scope):** Tranche 4 security — `audit.db` lacks DROP-TABLE / hash-chaining protection (still open, M1). `remote_indexer_service.py` plaintext `0.0.0.0:9000` no-auth was **fixed in Sprint N (C2 — bearer token + untrusted-RAG handling, 2026-06-12)**.

**Done (Gemma 4 general slot — 2026-06-07):** `feat(inference)` `7076c24` — general slot → `gemma4:12b` (fits resident, kills eviction churn), `e4b-it-qat` flare fallback; reusable model-eval suites. Code/plan consolidation **NO-GO** (gemma4 thinking-tax = 4× latency) — kept `qwen3-coder:30b`. See `project/gemma4_general_slot_plan.md`.

**Done (Agent-orchestration hardening + Opus 4.8 dev path — 2026-06-06):**
- `fix(inference)` — `ResourceGovernor` evicts the **router-derived** heavy specialist set on a flare (was hardcoded `qwen3-vl:30b`) and sleeps the vLLM pool; new `set_model_router()` + `ModelRouter.heavy_model_names()`/`sleep_specialists()`.
- `feat(dev-agent)` — destructive plans/ops **fail-safe to DENY** on silence/ambiguity (read-only keeps auto-approve); `plan_and_run` is now closed-loop observe→act→replan (`MAX_REPLANS=2`, one read-only retry); fixed `_parse_plan` args regex that silently dropped every step argument; `agent_runs` gains a status lifecycle (+migration) with crash reconciliation (`mark_interrupted_runs`) + voice-gated `resume_pending_plan()`.
- `feat(scheduler)` — Resource Invariants documented (single-permit `_dev_sem` is the real flare protection); `_dev_inflight` is leak-proof (decrement in `finally`); `stop()` cancels in-flight dispatched tasks; new `scheduler_queue_depth`/`scheduler_dev_inflight` gauges.
- `feat(observability)` — opt-in cross-layer tracing (`monitoring/trace.py`, `DA_TRACE`); `trace_id` rides the `Command` dataclass + a ContextVar through coordinator→router→executor; reconstructs enqueue→dispatch→route_decision→inference→execute; `commands.trace_id` column (+migration); `GET /trace` + `/trace/{id}`. Zero-cost no-op unless `DA_TRACE` is set.
- `chore(cloud)` — `CloudDevAgent` defaults to **`claude-opus-4-8`** (was `claude-sonnet-4-6`); command-path cloud fallback ID normalized to the `claude-haiku-4-5` alias. (Vision fallback in `vision_grounder.py` stays Sonnet 4.6 — local qwen3-vl is primary there.)
- All on PR **#32** (`fix/tilt-tap-click`), pushed and in sync with origin. Working-tree tuning not yet committed: `_gravity_max_pull` 18→22 px, `DA_SNAP_RADIUS_PX` default 200→300 px. See `docs/daily/2026-06-06-daily-review.md`.

**Done (Mouth-sound control removed — 2026-06-05):**
- The mouth-sound pipeline (cluck/pop/hiss via AVFoundation) was removed **entirely** (PC + iPad) — the sounds fired incidentally and were not a reliable control surface. Removed iPad-side: `SoundDetector.swift`, `SoundTrainingSheet.swift`, and the `sound`/`SoundDetector` wiring across `SensorManager.swift`, `OnboardingView.swift`, `SettingsStore.swift`, `SensorActivityBar.swift`, `SensorDashboardView.swift`, `FlareProfileSheet.swift` (Swift source count 41 → 40). Removed PC-side: the `sound_action` handler and the priority-2 sound branch in `core/fusion_engine.py` (now **6-level** priority); the `sound_action` message type is no longer sent. The FusionEngine priority list shrinks 7 → 6 (see Sensor Priority below).

**Done (Magnetic cursor — gravity re-enabled safely + overlay removed — 2026-06-05):**
- Cursor gravity (magnetic-click Phase 3) re-enabled **headless**. The 2026-06-04 attempt destabilised the desktop two ways: `desktop/magnetic_overlay.py` (fullscreen `overrideredirect`/`-topmost`/layered Tk window repainting at 30 Hz) stalled the DWM compositor (soft hang), and `desktop/target_cache.py` blind-walked the whole UIA tree 3×/s rooted at `GetFocusedElement` + walk-up, spamming E_POINTER "Invalid pointer" 3×/s on stale elements.
- `desktop/magnetic_overlay.py` — **deleted** (overlay dropped entirely; gravity needs no window).
- `desktop/target_cache.py` — `_loop` reworked: change-gated walk (`_walk_due` — only on a foreground-window change or a 1.5 s heartbeat; foreground hwnd read via cheap `GetForegroundWindow`, no COM), consecutive-failure backoff (caps at 2 s), `CoUninitialize` in `finally`. Read API (`nearest`/`snapshot`/`is_running`) unchanged.
- `desktop/ui_automation.py` — new `collect_snap_targets_for_window(hwnd)` roots the BFS at `ElementFromHandle(hwnd)` (fresh, foreground-scoped — no stale parent-walk); shared BFS body `_bfs_snap_targets`; bounds tightened (200 results / 0.3 s).
- `main.py` — starts the reworked cache + `fusion.set_target_cache()` behind kill-switch `DA_CURSOR_GRAVITY` (default on; `=0` disables → gravity no-ops); registered for graceful shutdown.
- Gravity math unchanged: `fusion_engine._apply_gravity` (radius 90 px, max pull 18 px) at end of position-mode tick; Phase 1 tilt-tap snap (`command_executor._magnetic_snap`) unchanged, now also reads the healthy cache.
- `tests/test_target_cache.py` — **new**; 24 tests (nearest/tie-break, `_walk_due` gating, backoff grow/reset/cap, UIA-unavailable no-op, `_apply_gravity` no-op/pull/settle). Full suite: **682 passed**. Stability smoke (`main.py --safe-mode`, ~100 s): 0 E_POINTER, 0 walk failures, 0 tracebacks.

**Done (Voice control + goal sessions + mic mute — 2026-06-03):**
- `core/goal_session.py` — **new file**; goal-level authorization: the user authorizes a high-level goal once (via voice) and the constituent Claude Code tool calls / DevAgent steps run silently without per-tool prompts; atomic-replace signal file `~/.claude/approval/goal_session.json` read by `approval_hook.py` and `DevAgent._confirm_destructive_op()`; voice `cancel`/`status`/`history` control
- `inference/dev_agent.py` — `_approve_plan_upfront()`, `_confirm_destructive_op()`, goal-session wiring; `core/hybrid_coordinator.py` — `authorize` phrase + cancel/status/history routing
- Mic mute — iPad toggle + indicator with two-way state sync: `mic_mute` (iPad→PC) hard-mutes `WhisperStream`; PC echoes `mic_state` (PC→iPad) so `MicMuteIndicator.swift` stays in sync; wired in `core/ipad_bridge.py` (`broadcast_mic_state`), `sensors/whisper_stream.py` (`set_muted`, `on_mute_change`)
- Voice pipeline hardening: wake-arming window (a paused wake phrase still works), wake-phrase mishearing correction + system-control keyword shadowing fix, trailing-punctuation strip before exact-match keyword routing (`sensors/whisper_stream.py`, `core/hybrid_coordinator.py`)
- `fix(cloud)`: repaired `_run_cloud` secret-scrub `Command` rebuild + aligned model; `fix(ipad)`: hardened Settings modals against iOS 26 crash (`SettingsView.swift`)
- `core/async_utils.py` — **new file**; `fire_and_log` safe fire-and-forget for non-critical background DB writes (strong ref until done, DEBUG-logs exceptions, no-ops without a loop); adopted in `fusion_engine.py` + `continuous_trainer.py`
- Desktop launcher scripts: `start_desktop.bat`, `Create-DesktopShortcut.ps1`

**Done (Cluster offload — laptop compute node — 2026-06-02):**
- `core/cluster_health.py` — **new file**; `ClusterHealthMonitor` polls laptop service endpoints (`laptop_ollama`/`whisper`/`indexer`); zero-cost synchronous `is_healthy()` for hot-path routing; fail-safe to local when unknown/down (fixed a `_loop` attr that shadowed the poll-loop method → startup crash)
- `core/cluster_config.py` + `cluster_config.json` — **new**; `ClusterConfig` endpoints/policy; lightweight LLM routed to desktop, Whisper + Indexer offloaded to RTX 4070 laptop
- `start_laptop_services.bat` — idempotent (skips running ports), login auto-start hardened; `codebase_indexer.py` excludes `.venv-laptop`/`.venv-wsl`
- Hardened laptop-offload failure paths (C1, H1, H2, M1–M4)

**Done (AIOS alignment — scheduler / memory syscall / resource governor / context namespacing — 2026-06-01):**
- `core/scheduler.py` — **new file**; `AccessibilityScheduler` priority queue over `coordinator.route()`; 5 tiers (ACCESSIBILITY/VOICE/GESTURE concurrent; DEV_AGENT/BACKGROUND semaphore-gated); 60 Hz tick loop untouched
- `storage/memory_manager.py` — **new file**; `MemoryManager` syscall façade over AgentDB + SemanticMemory (`read_context`/`write_state`/`search_semantic` + zero-copy `get_pain_day_active/score()`); `_VALID_KEYS` schema validation; incremental migration (DevAgent + ContinuousTrainer writers)
- `core/resource_governor.py` — **new file**; pain-aware kernel primitive; on flare (score ≥ 0.6) relaxes sensor thresholds, pauses indexer, raises Whisper VAD thread priority, evicts `qwen3-vl:30b` from VRAM (`keep_alive=0`); reverses on recovery (< 0.4 hysteresis)
- Gap 4 — context namespacing in `BehavioralTwinState`: `_session_history` split into `accessibility` (persistent) / `dev_agent` (ephemeral, auto-clears) so dev sessions don't contaminate accessibility few-shot
- 83 tests for AIOS alignment gaps 1–4; full handoff at `docs/daily/2026-06-01-aios-alignment-handoff.md`

**Done (Gaze + head-pose removal — 2026-05-30):** Gaze tracking and head-pose tracking were removed **entirely** (PC + iPad). The standard iPad on hand has no TrueDepth front camera, so `ARFaceTrackingConfiguration.isSupported` is false and both pipelines produced no data. Removed PC-side: all gaze/head logic in `fusion_engine.py` (priority 10→7 levels; `_GazeBuffer`/`HeadStationaryLock`/`head_acceleration_curve`/`_check_edge_scroll`/`_apply_gaze_cursor` deleted; edge-scroll was gaze-gated so removed too), `ipad_bridge.py` (7 message handlers + calibration session), `db.py` (`gaze_monitor_calibration` table + 2 methods; existing DBs keep the orphan table), `whisper_stream.py` ("calibrate monitor" trigger), `main.py` wiring; deleted `calibration/gaze_calibrator.py` + `calibration/calibration_overlay.py`; trimmed `vision_grounder.py`/`session_analyzer.py`/`sensor_viewer.py`. Removed iPad-side: `GazeTracker.swift`, `HeadTracker.swift`, `SharedFaceSession.swift`, `GazeCalibrationSheet.swift`, `MonitorCalibrationSheet.swift`, `CursorConflictBanner.swift` + detangled 15 Swift files (and the now-unused front-camera permission). Voice "click" now clicks at the **current cursor position** (cursor driven by tilt/trackpad/touch). `Command.gaze_coords` is KEPT as the generic explicit-click-coordinate field (vision grounder / voice click). ~13 gaze/head test files deleted.

**Done (Phase 1):** `ipad_bridge.py`, `command_executor.py`, `mcp_server/` (5 tool modules + MCP server), `tests/test_bridge_client.py`, `tests/test_touch_scroll_e2e.py`, `requirements.txt`

**Done (Phase 2):**
- `fusion_engine.py` — 10-level priority sensor fusion at 60 Hz; gaze delta cursor integration (relative eye movement → cursor), sound actions, tilt/head direct-to-pyautogui
- `hybrid_coordinator.py` — 4-gate routing (Gate 0 privacy + Gates 1–4); outcome logging to `agent.db`
- `local_inference.py` — `LocalInference` ABC + `OllamaInference` (default, 100% accuracy, ~190ms warm wall p50 / ~29ms compute on Ollama 0.30.6, RTX 5090, 2026-06-06), `VLLMInference` (verified working in Ubuntu WSL2 — vLLM 0.21.0 + torch 2.11.0+cu128; activate with `--backend vllm`)
- `mcp_server/tools/handwriting.py` — pix2tex LaTeX OCR + unicode conversion
- `iPadApp/DesktopAgent/` — SwiftUI app (41 Swift source files, 15 Swift test files): `SensorManager`, `SharedAudioSession`, `SharedFaceSession`, `ServiceDiscovery` (mDNS), `WebSocketManager`, `ScreenshotStore`; Sensors: `TiltSensor`, `GazeTracker`, `HeadTracker`, `KeywordListener`, `SoundDetector`, `AudioStreamer`, `LiDARStreamer`; UI: `CommandPadView`, `TrackpadView`, `HandwritingCanvasView` (Write tab — Math+Text mode, Click & Send), `ScreenshotOverlayView`, `SettingsView`, `DwellActionToolbar`, `DwellToolbarContainer`, `LiDARDebugView`, `OnboardingView`, `SensorDashboardView`, `SensorActivityBar`, `GazeCalibrationSheet`, `TiltCalibrationSheet`, `SoundTrainingSheet`, `CursorConflictBanner`, `CommandToast`; DesignSystem: `DesignTokens`, `AppTheme`, `DAButton`, `DACard`, `DAConnectionBanner`, `DASectionHeader`; `SettingsStore`, `FeatureToggleSyncer`, `DwellActionSyncer`

**Done (Phase 3):**
- `gesture_processor.py` — MediaPipe Tasks API (`HandLandmarker`); peace-sign base pose; 13-gesture vocabulary (PEACE_SWIPE_*, TWO_FINGER_GRAB/RELEASE, GRAB_SNAP_*, GRAB_NEXT/PREV_MONITOR, OPEN_PUSH/PULL, PINCH); 500ms rolling frame buffer; velocity learning; 800ms debounce
- `lidar_receiver.py` — Decodes `depth_frame` messages; confidence-map filtering; `get_depth_at()`
- `domain_classifier.py` — Keyword-scoring domain detection: COMMAND/CODE/MATH/VISION/PLAN/GENERAL
- `model_router.py` — VRAM-aware specialist model selection; 2 GB tolerance; domain-tuned prompts; fallback chain per domain
- `dev_agent.py` — Plan→execute→reflect agentic loop; 5 dev verbs; session context

**Done (Phase 4):**
- `continuous_trainer.py` — Routing threshold adaptation; few-shot ranking; gesture confidence floors; velocity-floor calibration (p10 of observed samples, −30% on pain days); delegates all storage to `AgentDB`
- `main.py` — Unified entry point; `--measure-vram`; startup status table; Ctrl-C shutdown
- `benchmark_models.py` — Ollama model benchmark; p50/p95 latency; VRAM snapshots; `--vllm` flag for VLLMInference comparison
- `whisper_stream.py` — GPU-accelerated speech; Silero VAD + faster-whisper
- `db.py` — `AgentDB` (aiosqlite, 14 tables) + `AnalyticsDB` (DuckDB); MiniLM semantic few-shot retrieval; +2 tables for gesture velocity learning (gesture_velocity_samples, gesture_velocity_calibration)

**Done (Phase 6 — cloud fallback):**
- `hybrid_coordinator.py` — `_retranscribe()`: phonetic vocabulary correction (6 misrecognitions, 0ms) on low-confidence voice before Gate 2 (Amazon Transcribe Stage 2 removed in the Anthropic migration); Gate 1 route label propagated to executor
- `command_executor.py` — `_polly_speak()`: Amazon Polly TTS (Danielle neural, 16kHz PCM) sidecar-down fallback for CLARIFY; primary path uses `polly_stream.get_client().speak_sync()`; SEARCH_WEB URL-encoded via `urllib.parse`
- Cloud path: Anthropic API (`anthropic` SDK) via `_CloudInference` in `hybrid_coordinator.py`, model `claude-haiku-4-5` (8/8 accuracy on voice misrecognitions); 10s timeout circuit-breaker → CLARIFY. The dev-domain cloud path (`CloudDevAgent`) defaults to `claude-opus-4-8` (Opus 4.8 access was granted on the Bedrock account in use; a fresh per-model grant can lag the runtime a few minutes, during which the dev path CLARIFYs). The dev model is overridable at runtime with **`DA_CLOUD_DEV_MODEL`** (no code change) — e.g. set it to `claude-sonnet-4-6` to fall back while an Opus grant propagates, then unset it to return to Opus.
- **Cloud backend selection (`core/cloud_backend.py`).** Both cloud consumers build their client through `resolve_backend()` + `make_client()`, which pick the backend in one place: **direct Anthropic** (`ANTHROPIC_API_KEY`) by default, or **Amazon Bedrock** when an Amazon Bedrock API key (`AWS_BEARER_TOKEN_BEDROCK`) is set. Bedrock uses the `anthropic` SDK's `AnthropicBedrock`/`AsyncAnthropicBedrock` client (classic `bedrock-runtime` InvokeModel), which reads the key from `AWS_BEARER_TOKEN_BEDROCK` and signs for `aws_region`; the request shape (`messages.create`/`.stream`) is identical to the first-party client. Model ids are remapped to **cross-region inference-profile** ids — `us.anthropic.claude-haiku-4-5-20251001-v1:0`, `us.anthropic.claude-opus-4-8` (newer models dropped the date/version suffix; Haiku 4.5 keeps one). Prefix via `DA_BEDROCK_PROFILE_PREFIX` (default `us`; `global` = no regional premium). Region: `DA_BEDROCK_REGION` → `AWS_REGION` → `us-east-1`. Force a backend with `DA_CLOUD_BACKEND=bedrock|anthropic`. A missing credential or a model the account lacks access to degrades to a clear CLARIFY, not a raw SDK traceback. (The newer "Claude in Amazon Bedrock" Mantle Messages endpoint exists but a standard Bedrock API key returns "not available for this account" there, so this uses the classic InvokeModel path. Per-model Bedrock access is account-gated: Haiku 4.5, Sonnet 4.6, and Opus 4.8 are all granted on the account in use. The command path uses Haiku 4.5 (fast/cheap, latency-critical accessibility path) and the dev path uses Opus 4.8 (most capable for code/reasoning); Sonnet 4.6 remains the vision-grounding fallback.)

**Done (LiDAR gesture depth + Settings UI + housekeeping — 2026-05-16):**
- `LiDARStreamer.swift` — ARWorldTrackingConfiguration + `.smoothedSceneDepth`; 5 fps depth / 10 fps camera; serialises `depth_frame` (float32 + uint8 conf) and `camera_frame` (JPEG 480px) matching PC bridge protocol; publishes UIImages for debug view
- `LiDARDebugView.swift` — Sensors tab: camera top, depth heatmap bottom (blue=near → red=far, 0–4 m), stats bar, Start/Stop button
- `lidar_receiver.py` bug fix: `is_fresh()` compared `time.monotonic()` vs Unix timestamp (always True after first frame); fixed to use `_recv_mono`
- `gesture_processor.py`: `pinch_dist_mm` renamed `pinch_z_delta_mm` (Z-axis delta only, not 3D Euclidean)
- `chatterbox_tts.py` — local GPU TTS backend; `ChatterboxClient` mirrors `PollyStreamClient` interface; emotion exaggeration, paralinguistic tags, zero-shot voice cloning via audio prompt; dispatched from `polly_stream.get_client()` when `tts_backend == "chatterbox"` in `approval_config.json`
- `start_agent.bat` — Windows startup script; launches `main.py` with rolling log to `logs/agent_startup.log`
- Settings UI: keyword list, sound mappings, command pad editor all migrated from read-only `Text` to editable `TextField` bindings
- Approval hook bug fix: `log` was undefined (NameError on PC-mic fallback); fixed with `import logging` + logger instance
- `command_executor.py`: `sd.get_stream().active` lacked None guard → `AttributeError`; fixed to `sd.get_stream() and sd.get_stream().active`
- `approval_config.json`: `"device"` narrowed from `"Realtek USB Audio"` (matched 3 devices, threw sounddevice exception → silent auto-approve) to `"Microphone (Realtek USB Audio)"`

**Done (iPad UX + gaze refactor + sensor viewer — 2026-05-17):**
- `sensor_viewer.py` — tkinter desktop window showing camera + LiDAR depth feeds in real time; hand landmark overlay from GestureProcessor; gaze cursor overlay on depth panel; freeze-frame (Space); snapshot to disk (Ctrl+S); always-on-top toggle; wired into `main.py --viewer`
- `GazeTracker.swift` — refactored to delta-based cursor movement (removing dwell-click); configurable stability threshold for glasses users
- `OnboardingView.swift` — first-run wizard (6 steps: welcome, tilt, gaze, voice, touch, summary)
- `SensorDashboardView.swift` — all-sensor status dashboard (replaces LiDAR-only Sensors tab); per-sensor activity, conflict detection
- `SensorActivityBar.swift` — compact horizontal sensor-activity indicator strip
- `GazeCalibrationSheet.swift`, `TiltCalibrationSheet.swift`, `SoundTrainingSheet.swift` — per-sensor calibration UX
- `CursorConflictBanner.swift` — banner shown when multiple cursor sources are active simultaneously
- `CommandToast.swift` — transient action feedback toast; success state (blue icon, 2 s) and error state (orange warning icon, 4 s) driven by `wsManager.commandFeed` and `wsManager.errorFeed` respectively
- `ContentView.swift` — swipe-to-switch tabs; parent-driven scroll disable; custom tab bar always on top
- CI: Xcode 16.4 + iOS 18.5 SDK on `macos-15`; `upload-artifact v7`; TestFlight upload made non-fatal (SDK version gate)

**Done (Touch-debug fix — 2026-05-16):**
- `DwellToolbarContainer.swift` — outer ZStack `.allowsHitTesting(false)` with toolbar `.allowsHitTesting(true)`; removed `.frame(maxWidth: .infinity)` in top/bottom modes; bottom mode uses VStack + `Color.clear.frame(height:56).allowsHitTesting(false)` spacer; floating mode `.contentShape(RoundedRectangle(...))` before `.gesture(DragGesture())`
- `DAConnectionBanner.swift` — added `.allowsHitTesting(isDisconnected)`; removed `.contentShape(Rectangle())`
- Tests: `OverlayTouchInterceptionTests.swift` (bug condition geometry), `OverlayPreservationTests.swift` (17 preservation property tests)

**Done (Minority Report gestures + dead code removal — 2026-05-19):**
- `gesture_processor.py` — complete rewrite: static-pose classifier → two-finger spatial motion detection. Base pose is peace sign (index+middle extended). 13-gesture vocabulary; 500ms rolling frame buffer; axis-dominance debounce; LiDAR-validated grab depth; `compute_peace_jitter()` inflammation signal; `drain_velocity_samples()` for ContinuousTrainer
- `db.py` — +2 tables: `gesture_velocity_samples`, `gesture_velocity_calibration`; +4 methods: `record_gesture_velocity`, `get_recent_gesture_velocities`, `update_gesture_velocity_calibration`, `get_gesture_velocity_floor`
- `continuous_trainer.py` — `gesture_processor=` param; `record_success()` drains velocity queue; `_update_gesture_velocity_calibration()`: velocity_floor = p10(observed), pain_day → ×0.70; calibrated thresholds pushed back to GestureProcessor
- `HandwritingCanvasView.swift` — enhanced Write tab (replaces Keypad tab): Math mode (pix2tex), Text mode (on-device VNRecognizeTextRequest); Click & Send action; editable result field; tabs reduced from 6→5
- Dead code deleted: `migrate.py` (migration already run), `health_viz.py` (zero accessibility value), `agentcore_fallback/` (deployment deferred, CLI missing), `NemotronInference` class (25% accuracy)
- `approval_config.json` — gate narrowed: Bash/PowerShell/Agent → voice approval; Edit/Write/Read/Glob/Grep/WebSearch/WebFetch → silent
- CI: `.github/workflows/build-ipad-app.yml` — `continue-on-error: true` on artifact upload (transient ECONNRESET)

**Done (FusionEngine bug fixes + pain-day adaptation — 2026-05-20):**
- `fusion_engine.py` — 4 bug fixes: tilt/head starvation (moved `return` inside `if dx or dy:`); gyro-suppression starvation (wrapped pipeline in `if not _suppressed:`); double cursor movement (`_apply_gaze_cursor` returns bool, gates gaze_delta); silent click drop (CLARIFY emitted when gaze click has no target)
- `fusion_engine.py` — `apply_pain_day()` method: 6 thresholds relaxed on pain days; wired through `HybridCoordinator` via `BehavioralTwinState`
- `tests/test_fusion_fixes.py` — 24 new tests covering all 4 bug fixes and pain-day config propagation

**Done (Voice pipeline improvements — 2026-05-20):**
- `whisper_stream.py` — wake phrase `"hey agent"` / `"agent"` with punctuation normalisation; lecture mode (`ambient_transcripts` table); hallucination filter (`no_speech_prob > 0.5` + `avg_logprob < -0.8`); CLARIFY echo suppression (pre-suppress before TTS + 1.5s post-suppress); pending clarification context prepended to LLM prompt; awaiting-clarification gate blocks long non-answer transcripts
- `local_inference.py` — known-app voice corrections applied pre-gate so `"cairo"` → `"kiro"` always fires before LLM sees text

**Done (Sprint A — Acoustic Profiler — 2026-05-20):**
- `acoustic_profiler.py` — measures RMS amplitude, spectral centroid, Whisper logprob per utterance; derives per-user `vad_threshold` and `logprob_floor`; scales both on flare days; Voice clarity as Signal 5 in PainDayEngine; passive calibration (calibrated after 15 samples); drift detection (every 20 samples, >30% drop → recal callback); seasonal prompt (every 50 commands, >30 days since last cal)
- `db.py` — +6 tables: `voice_calibration`, `voice_profile`, `voice_phrases`, `sensor_rom`, `flare_profile`, `ambient_transcripts` (total: 20 AgentDB tables)
- `tests/test_acoustic_profiler.py` — 18 new tests

**Done (Sprint B — iPad Accessibility Onboarding UI — 2026-05-20):**
- `VoiceProfilingSheet.swift` — 10 phrases × 3 repeats, 4s countdown; iPad streams mic while AcousticProfiler captures samples passively
- `GestureAssessmentSheet.swift` — rates 4 gestures (POINT/PINCH/OPEN_PALM/FIST) as Easy/Hard/Can't; disabled gestures synced to `GestureProcessor.set_disabled_gestures()`
- `FlareProfileSheet.swift` — which sensors degrade (voice/gesture/tilt/sound), voice volume fraction slider, manual pain day toggle (manual toggle syncs via `pain_day_override` in <100ms; degrade flags sync via debounced `flare_profile` message → `AgentDB.upsert_flare_profile` + `BehavioralTwinState.set_flare_profile`)
- `QuickRecalSheet.swift` — 3 phrases × 3 repeats (~90s); shown automatically when PC detects voice drift or seasonal prompt fires; wired into `ContentView` via `wsManager.recalibrationFeed`
- `OnboardingView.swift` — expanded 7 → 10 steps with the 3 new calibration sheets (all skippable)

**Done (Sprint C — Continuous Recalibration — 2026-05-20):**
- `voice_calibrator.py` — guided voice calibration for good_day / flare_day / allergy_day conditions (svt_attack shipped here, removed 2026-06-11); 20-phrase full session; voice-triggered (`"hey agent run voice calibration"`) and iPad-triggered (Settings → Voice Calibration tab)
- `ipad_bridge.py` — `pain_day_override` message type handler → `BehavioralTwinState.set_manual_pain_day()` → `AcousticProfiler.get_vad_threshold(pain_day=True)` → `WhisperStream._silence_thresh` relaxed immediately
- After every 20 voice samples: drift check → `bridge.send_recalibration_request()` → `QuickRecalSheet`; after every 50 commands: seasonal prompt (same path)

**Done (Sprint 5 — Vision Grounding — 2026-05-20):**
- `vision_grounder.py` — `claude-sonnet-4-6` vision resolves named UI targets to pixel coords; confidence gate ≥0.7; 2s cache per target; fallback chain: vision → gaze_coords → Tesseract OCR → cursor + CLARIFY; hooked into `HybridCoordinator._execute_action` for CLICK with named target; expected CLICK success 42% → ~78%
- `tests/test_vision_grounder.py` — 11 new tests

**Done (Sprint 6 — UIAutomation — 2026-05-20):**
- `ui_automation.py` — Win32 UIAutomation BFS tree search; fuzzy name scoring (exact → contains → word-overlap → value match); 0.3s timeout; 1s cache per (target, app); targets VS Code, Chrome, Edge, Kiro, Windows Terminal, Notepad, Acrobat, Zotero; first fallback in `_resolve_coords` before vision grounder; expected CLICK success ~78% → ~88%

**Done (Sprint 7 — Action Verification — 2026-05-20):**
- `action_verifier.py` — Pillow perceptual diff pre/post screenshot; verifies CLICK, OPEN, CLOSE, SCROLL; 2% pixel change threshold = success; 400ms delay for animations; pre-snapshot taken before dispatch, post-snapshot after; result in execute() response; expected CLICK success ~88% → ~92%

**Done (Commercial roadmap + diagrams — 2026-05-20):**
- `docs/diagrams/domain-model.{png,svg}` — class diagram: User/Subscription/Device/Session + pipeline hierarchy
- `docs/diagrams/database-schema.{png,svg}` — ERD: 12 tables (4 new commercial: USERS, SUBSCRIPTIONS, DEVICES, INFERENCE_COSTS + 8 existing extended with user_id FK)
- `docs/diagrams/user-stories.{png,svg}` — mindmap: 5 epics (Setup, Daily Control, Coding/Dev, Pain Day, Subscription)
- 7-phase commercial roadmap: May 2026 hardening → Jul 2027 launch at 100 subscribers / $1K MRR; cloud inference via `claude-haiku-4-5` at <$0.10/user/day; $9.99/month StoreKit subscription

**Done (Test coverage + tilt snapshot — 2026-05-21):**
- `tests/test_ui_automation.py` — 29 new tests: `UIElement`, `_detect_app`, `_score` (all 5 tiers), `UIAutomationProvider` (cache hit/miss/expiry, exception path, status)
- `tests/test_action_verifier.py` — 22 new tests: `VerifyResult`, all skip paths, post-snapshot error, `_diff()` (identical/different/size-mismatch/noise-floor), `verify()` end-to-end for all 4 verifiable verbs
- `engineering/tilt_implementation.md` (memory) — full working-state snapshot: two modes, axis mapping, all FusionConfig defaults, pain-day deltas, fall-through guarantee, stationary lock

**Done (Sprint G1–G4 — Gaze monitor calibration — 2026-05-21):**
- `gaze_calibrator.py` — angular affine mapping: 5-point `add_sample()` → `solve()` (numpy lstsq, az/el tangent plane) → `project(ray_dir) → (px_x, px_y)`; `gaze_calibration.json` sidecar persistence; `save_to_db()` for history
- `calibration_overlay.py` — tkinter full-screen translucent overlay; 5 dots (top-left, top-right, center, bottom-left, bottom-right, 5% padding); cyan 40px dot + crosshair; daemon thread; advances via `advance()`, closes via `finish()`/`cancel()`
- `db.py` — +1 table: `gaze_monitor_calibration` (total: **21 AgentDB tables**); +2 methods: `upsert_gaze_calibration()`, `get_gaze_calibration()`
- `GazeTracker.swift` — `currentWorldRay` property; world-space extraction from `faceAnchor.transform * eyeTransform`; 10 Hz `gaze_ray` WebSocket send (rate-limited, every 6th frame)
- `WebSocketManager.swift` — `sendGazeRay(dx:dy:dz:confidence:)`
- `ipad_bridge.py` — `gaze_ray` handler (stores ray + timestamp); `gaze_dwell` handler attaches fresh ray (< 300ms) to FusionEngine call; `gaze_calibration_sample` handler; `set_gaze_calibrator()` wiring
- `fusion_engine.py` — `set_gaze_calibrator()`; `on_gaze_dwell()` extended with `ray_dir` param → calibrator override of (x, y) when calibrated
- `main.py` — `GazeCalibrator` load at startup; startup status table "Gaze monitor calibration" row; wired to bridge and fusion
- `tests/test_gaze_calibrator.py` — 22 new tests: sample management, solve (success/failure/collinear), project (center, all samples, bounds clamp, zero ray, type), JSON round-trip, DB persistence
- **Remaining:** voice command trigger (`"hey agent calibrate monitor"` → overlay → solve → TTS report) and `MonitorCalibrationSheet.swift` iPad UI

**Done (iPad structured log forwarding — 2026-05-22):**
- `ipad_bridge.py` — `ipad_log` message handler: routes each AppLogger entry to `ipad.<subsystem>` Python logger; warning+ entries persisted to DB
- `db.py` — +1 table: `ipad_logs`; +1 method: `log_ipad_events(session_id, entries)`; AgentDB is now **30 tables** (the 3 `benchmark_*` tables in `db.py` belong to the DuckDB `AnalyticsDB`, not `agent.db`; the most recent additions are `goal_queue` from gap D)
- `iPadApp/DesktopAgent/AppLogger.swift` — structured log forwarding over WebSocket (subsystem + level + msg batching)
- Multiple Swift sensor files updated to use AppLogger for structured output: `SharedAudioSession`, `AudioStreamer`, `GazeTracker`, `HeadTracker`, `KeywordListener`, `LiDARStreamer`, `SharedFaceSession`, `TiltSensor`, `SensorManager`, `DesktopAgentApp`
- `fusion_engine.py` — `set_gaze_calibrator()` wiring path also updated

**Test suite (2026-06-07):** 714 pytest test functions across 62 `tests/test_*.py` files (761 passed when run, incl. parametrization) + 15 Swift XCTest files
