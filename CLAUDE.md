# Personal Desktop Agent

Multimodal accessibility desktop control for a single user with rheumatoid arthritis. An iPad Pro (2020+) is the sensor hub and primary touch surface; a Windows PC with RTX 5090 runs inference and executes desktop actions.

## What This Is

The user controls a Windows desktop through voice, hand gesture, iPad tilt, and direct touch — all mapped to a 16-verb action vocabulary (11 accessibility + 5 dev-agent). (Eye-gaze and head-pose control were removed — the standard iPad lacks the required TrueDepth sensor.) Sensor data streams over WebSocket from a native Swift iPad app to a Python backend on the PC. The PC runs local LLM inference (Ollama → vLLM in production) and executes commands via pyautogui/Win32.

- Full requirements (17): `.kiro/specs/ipad-sensor-focus/requirements.md`
- Architecture diagrams (13): `.kiro/specs/ipad-sensor-focus/diagrams/00-index.md`
- Tech stack: `.kiro/steering/tech.md`
- Open tasks: `.kiro/specs/ipad-sensor-focus/tasks.md`
- Daily reviews: `docs/daily/`

## Current Status — Phases 1–6 + Sprints A–C/5–7/G1–G5 + gaze removal + AIOS alignment + cluster offload + goal sessions + mic mute + magnetic cursor/gravity + mouth-sound removal + agent-orchestration hardening + gemma4 general slot + RAG/KB remediation + Sprint N security/hygiene (2026-06-12)

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
- Cloud path: Anthropic API (`anthropic` SDK) via `_CloudInference` in `hybrid_coordinator.py`, model `claude-haiku-4-5` (8/8 accuracy on voice misrecognitions); 10s timeout circuit-breaker → CLARIFY. The dev-domain cloud path (`CloudDevAgent`) uses `claude-opus-4-8`. (Migrated off AWS Bedrock; AgentCore deployment deferred and source deleted.)

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
| `core/hybrid_coordinator.py` | 4-gate routing (Gate 0 privacy + Gates 1–4); Anthropic API cloud fallback (10s timeout circuit-breaker); local-inference circuit-breaker (`local_timeout_s`, default 15s → CLARIFY); LLM output schema validation (`_parse_action` + `_VALID_COMMAND_VERBS`; malformed verb → CLARIFY); outcome logger |
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
| `desktop/vision_grounder.py` | Local qwen3-vl:30b (Ollama) resolves named UI targets to pixel coords; claude-sonnet-4-6 as fallback; confidence ≥0.7; 2s cache; fallback chain: vision → gaze → OCR → CLARIFY |
| `desktop/ui_automation.py` | Win32 UIAutomation BFS tree search; fuzzy name scoring; 0.3s timeout; 1s cache; first fallback in `_resolve_coords` |
| `desktop/action_verifier.py` | Pillow perceptual diff pre/post screenshot; verifies CLICK/OPEN/CLOSE/SCROLL; 2% pixel threshold; 400ms animation delay |
| `desktop/flick_engine.py` | Flick-to-snap gesture handler; maps GRAB_SNAP_* gestures to window snap zones; uses OneEuroFilter for smoothing |
| `desktop/target_cache.py` | `ClickableTargetCache` — daemon thread publishing a lock-protected snapshot of clickable UI targets for magnetic snap + cursor gravity; change-gated COM walk (foreground-hwnd + 1.5 s heartbeat), failure backoff, `CoUninitialize`; started behind `DA_CURSOR_GRAVITY` |
| `inference/kiro_client.py` | WebSocket client for Kiro/VS Code bridge extension on ws://127.0.0.1:8767; wired to DevAgent for code edits |
| `inference/codebase_indexer.py` | ChromaDB RAG index (cosine) over Python/Swift source + docs PDFs; accepts `embedder=`; oversized units sub-split into `_(i/N)` chunks (no 4000-char truncation); per-path debounced file watcher; time-gated `_available` re-probe; fed to DevAgent for context |
| `monitoring/metrics.py` | In-process metrics singleton; VRAM poller; optional `/metrics` HTTP endpoint |
| `storage/session_analyzer.py` | Post-session DuckDB analytics; route distribution, latency percentiles, error modes; summary persisted to AgentDB |
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
