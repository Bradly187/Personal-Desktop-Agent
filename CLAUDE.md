# Personal Desktop Agent

Multimodal accessibility desktop control for a single user with rheumatoid arthritis. An iPad Pro (2020+) is the sensor hub and primary touch surface; a Windows PC with RTX 5090 runs inference and executes desktop actions.

## What This Is

The user controls a Windows desktop through voice, eye gaze, head pose, hand gesture, iPad tilt, mouth sounds, and direct touch — all mapped to a 16-verb action vocabulary (11 accessibility + 5 dev-agent). Sensor data streams over WebSocket from a native Swift iPad app to a Python backend on the PC. The PC runs local LLM inference (Ollama → vLLM in production) and executes commands via pyautogui/Win32.

- Full requirements (17): `.kiro/specs/ipad-sensor-focus/requirements.md`
- Architecture diagrams (13): `.kiro/specs/ipad-sensor-focus/diagrams/00-index.md`
- Tech stack: `.kiro/steering/tech.md`
- Open tasks: `.kiro/specs/ipad-sensor-focus/tasks.md`
- Daily reviews: `docs/daily/`

## Current Status — Phases 1–6 complete + Sprints A–C + 5–7 + G1–G5 + iPad logging + 2026-05-24 fixes (2026-05-24)

**Done (Phase 1):** `ipad_bridge.py`, `command_executor.py`, `mcp_server/` (5 tool modules + MCP server), `tests/test_bridge_client.py`, `tests/test_touch_scroll_e2e.py`, `requirements.txt`

**Done (Phase 2):**
- `fusion_engine.py` — 10-level priority sensor fusion at 60 Hz; gaze delta cursor integration (relative eye movement → cursor), sound actions, tilt/head direct-to-pyautogui
- `hybrid_coordinator.py` — 4-gate routing (Gate 0 privacy + Gates 1–4); outcome logging to `agent.db`
- `local_inference.py` — `LocalInference` ABC + `OllamaInference` (default, 100% accuracy, 373ms warm p50), `VLLMInference` (verified working in Ubuntu WSL2 — vLLM 0.21.0 + torch 2.11.0+cu128; activate with `--backend vllm`)
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
- `whisper_stream.py` — GPU-accelerated speech; Silero VAD + faster-whisper; preserves audio bytes in `Command.params` for Gate 1 Transcribe re-transcription
- `db.py` — `AgentDB` (aiosqlite, 14 tables) + `AnalyticsDB` (DuckDB); MiniLM semantic few-shot retrieval; +2 tables for gesture velocity learning (gesture_velocity_samples, gesture_velocity_calibration)

**Done (Phase 6 — cloud fallback):**
- `hybrid_coordinator.py` — `_retranscribe()`: Stage 1 phonetic vocabulary correction (6 misrecognitions, 0ms), Stage 2 Amazon Transcribe streaming (activates when `pip install amazon-transcribe`); Gate 1 route label propagated to executor
- `command_executor.py` — `_polly_speak()`: Amazon Polly TTS (Danielle neural, 16kHz PCM) sidecar-down fallback for CLARIFY; primary path uses `polly_stream.get_client().speak_sync()`; SEARCH_WEB URL-encoded via `urllib.parse`
- Cloud path: raw Bedrock `us.anthropic.claude-haiku-4-5-20251001-v1:0` (8/8 accuracy on voice misrecognitions); AgentCore deployment deferred and source deleted — raw Bedrock is the active cloud path

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
- `FlareProfileSheet.swift` — which sensors degrade, voice volume fraction slider, manual pain day toggle (syncs to PC via `pain_day_override` WebSocket message in <100ms)
- `QuickRecalSheet.swift` — 3 phrases × 3 repeats (~90s); shown automatically when PC detects voice drift or seasonal prompt fires; wired into `ContentView` via `wsManager.recalibrationFeed`
- `OnboardingView.swift` — expanded 7 → 10 steps with the 3 new calibration sheets (all skippable)

**Done (Sprint C — Continuous Recalibration — 2026-05-20):**
- `voice_calibrator.py` — guided voice calibration for good_day / flare_day / allergy_day / svt_attack conditions; 20-phrase full session; voice-triggered (`"hey agent run voice calibration"`) and iPad-triggered (Settings → Voice Calibration tab)
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
- `db.py` — +1 table: `ipad_logs`; +1 method: `log_ipad_events(session_id, entries)`; total is now **27 AgentDB tables** (previous Sprint C tables were undercounted)
- `iPadApp/DesktopAgent/AppLogger.swift` — structured log forwarding over WebSocket (subsystem + level + msg batching)
- Multiple Swift sensor files updated to use AppLogger for structured output: `SharedAudioSession`, `AudioStreamer`, `GazeTracker`, `HeadTracker`, `KeywordListener`, `LiDARStreamer`, `SharedFaceSession`, `TiltSensor`, `SensorManager`, `DesktopAgentApp`
- `fusion_engine.py` — `set_gaze_calibrator()` wiring path also updated

**Test suite (2026-05-21):** 388 pytest tests + 31 standalone integration scripts + 15 Swift XCTest files = 434 total

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
| `core/ipad_bridge.py` | aiohttp WebSocket server on :8765; routes 15 incoming message types; sends `ack`, `status`, `screenshot`, `handwriting_result` replies |
| `core/command_executor.py` | Maps 16 action verbs to mcp_server tool calls; `_resolve_coords` falls back to screen centre; SCREENSHOT defaults to active window and copies to Windows clipboard |
| `mcp_server/desktop_mcp_server.py` | MCP stdio server; 14 tools; `SAFE_MODE` env var |
| `mcp_server/tools/mouse.py` | move, click, double_click, scroll, drag |
| `mcp_server/tools/keyboard.py` | type, hotkey, press, paste (unicode via clipboard) |
| `mcp_server/tools/screen.py` | screenshot (base64 PNG), get_screen_size, find_text_on_screen (OCR) |
| `mcp_server/tools/windows.py` | get_active_window, list_windows, focus_window (win32gui + psutil) |
| `mcp_server/tools/handwriting.py` | pix2tex LaTeX OCR; latex_to_unicode fallback converter |
| `core/fusion_engine.py` | 60 Hz tick loop; 10-level sensor priority; direct pyautogui for tilt/head |
| `core/hybrid_coordinator.py` | 4-gate routing (Gate 0 privacy + Gates 1–4); AWS Bedrock fallback; outcome logger |
| `inference/local_inference.py` | `LocalInference` ABC; `OllamaInference` (default, 373ms warm p50), `VLLMInference` (verified in Ubuntu WSL2, vLLM 0.21.0; `--backend vllm`; use `--gpu-memory-utilization 0.65` with Whisper running) |
| `adaptive/continuous_trainer.py` | Routing threshold adaptation; few-shot ranking; gesture velocity-floor calibration (p10 observed, −30% pain day); delegates all storage to `AgentDB`; holds `gesture_processor=` ref for live threshold push-back |
| `sensors/lidar_receiver.py` | Decodes depth_frame messages; confidence-map filtering; `get_depth_at()` |
| `adaptive/behavioral_twin_state.py` | Persistent user behaviour model: `TwinSnapshot`, `PreferenceModel`, `PainDayEngine`; AgentDB + ChromaDB backing; feeds `HybridCoordinator` before every gate decision |
| `storage/semantic_memory.py` | ChromaDB vector store (all-MiniLM-L6-v2) for semantic few-shot retrieval; Jaccard fallback when chromadb unavailable; `stop()` releases WAL file handles on Windows |
| `sensors/one_euro_filter.py` | Casiez 2012 adaptive low-pass filter (1€); used for tilt velocity, tilt position, gaze delta, head tracking — replaces EMA throughout sensor pipelines |
| `calibration/gyro_bias_calibrator.py` | Gyro bias state machine (UNCALIBRATED→COLLECTING→CALIBRATED→FROZEN); stationary detection + lerp-smoothed bias subtraction for tilt velocity pipeline |
| `sensors/gesture_processor.py` | MediaPipe Tasks API (`HandLandmarker`, `hand_landmarker.task`); peace-sign base pose; 13 gestures (swipe/grab/snap/monitor/push-pull/pinch); 500ms rolling buffer; velocity learning; 800ms debounce |
| `core/domain_classifier.py` | Keyword-scoring domain detection: COMMAND/CODE/MATH/VISION/PLAN/GENERAL |
| `inference/model_router.py` | VRAM-aware specialist model selection; domain-tuned prompts; Ollama inference |
| `inference/dev_agent.py` | Plan→execute→reflect agentic loop; 5 dev verbs; session context |
| `main.py` | Unified entry point; `--measure-vram`; `--viewer`/`--viewer-only`; startup status table; Ctrl-C shutdown |
| `sensors/sensor_viewer.py` | tkinter desktop window (daemon thread); camera + LiDAR depth side-by-side; hand landmark overlay; gaze cursor overlay; freeze-frame; depth-at-cursor readout; always-on-top toggle |
| `sensors/whisper_stream.py` | GPU-accelerated speech: Silero VAD + faster-whisper large-v3; emits `Command(source="voice")` to FusionEngine |
| `storage/db.py` | `AgentDB` (aiosqlite, 27 tables, all pipeline writes) + `AnalyticsDB` (DuckDB, benchmark history); MiniLM semantic retrieval; gesture velocity + voice + gaze monitor calibration + iPad log tables |
| `calibration/gaze_calibrator.py` | Angular affine mapping from world-space gaze ray → screen pixel; `add_sample()`/`solve()`/`project()`; numpy lstsq; `gaze_calibration.json` + AgentDB persistence |
| `calibration/calibration_overlay.py` | Tkinter full-screen 5-dot calibration overlay; daemon thread; advances/closes via method calls |
| `tests/test_bridge_client.py` | Simulated iPad client; sends 8 test messages; verifies ack for each |
| `tts/polly_stream.py` | Python TTS client — HTTP to Node.js sidecar; `speak_sync()` for threads, `speak()` async, `speak_stream()` for token-by-token; auto-starts sidecar; `get_client(backend=)` dispatches to Chatterbox when configured |
| `tts/chatterbox_tts.py` | Local GPU TTS backend (RTX 5090); `ChatterboxClient` with same interface as `PollyStreamClient`; emotion exaggeration, paralinguistic tags, zero-shot voice cloning |
| `tts_service/server.js` | Node.js sidecar (port 8766); calls `StartSpeechSynthesisStream` (AWS SDK v3); returns OGG Vorbis; Python decodes with soundfile |
| `approval_hook.py` | Claude Code `PreToolUse` gate; Danielle speaks action description; records iPad mic via WhisperStream signal file or PC mic fallback; yes/no → exit 0/2 |
| `storage/audit_log.py` | Append-only `audit.db` (SQLite WAL); records every MCP tool invocation, session lifecycle event, and security finding; UPDATE/DELETE blocked by triggers |
| `approval_config.json` | Per-tool approval policy (`"approve"` / `"silent"`), voice, mic device (`"Microphone (Realtek USB Audio)"`), timeout, tts_backend |
| `start_agent.bat` | Windows startup script; activates venv and runs `main.py`; logs to `logs/agent_startup.log` |
| `calibration/acoustic_profiler.py` | Per-user VAD threshold + logprob floor from measured RMS/spectral-centroid/Whisper-logprob; passive calibration; drift detection; seasonal re-cal prompt; Signal 5 in PainDayEngine |
| `calibration/voice_calibrator.py` | Guided voice calibration for 4 conditions (good/flare/allergy/SVT); 20 phrases; voice-triggered or iPad Settings tab; writes to `voice_profile` + `voice_phrases` tables |
| `desktop/vision_grounder.py` | Local qwen3-vl:30b (Ollama) resolves named UI targets to pixel coords; claude-sonnet-4-6 as fallback; confidence ≥0.7; 2s cache; fallback chain: vision → gaze → OCR → CLARIFY |
| `desktop/ui_automation.py` | Win32 UIAutomation BFS tree search; fuzzy name scoring; 0.3s timeout; 1s cache; first fallback in `_resolve_coords` |
| `desktop/action_verifier.py` | Pillow perceptual diff pre/post screenshot; verifies CLICK/OPEN/CLOSE/SCROLL; 2% pixel threshold; 400ms animation delay |
| `desktop/flick_engine.py` | Flick-to-snap gesture handler; maps GRAB_SNAP_* gestures to window snap zones; uses OneEuroFilter for smoothing |
| `inference/kiro_client.py` | WebSocket client for Kiro/VS Code bridge extension on ws://127.0.0.1:8767; wired to DevAgent for code edits |
| `inference/codebase_indexer.py` | ChromaDB RAG index over Python/Swift source + docs PDFs; incremental file watcher; fed to DevAgent for context |
| `monitoring/metrics.py` | In-process metrics singleton; VRAM poller; optional `/metrics` HTTP endpoint |
| `storage/session_analyzer.py` | Post-session DuckDB analytics; route distribution, latency percentiles, error modes; summary persisted to AgentDB |

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

**iPad → PC (28 types):**
- *Sensor streams:* `tilt`, `tilt_position`, `tilt_tap`, `tilt_ratchet`, `gaze`, `gaze_delta`, `gaze_dwell`, `gaze_ray`, `gaze_calibration_sample`, `head_pose`, `keyword`, `sound_action`, `audio_stream`, `camera_frame`, `depth_frame`
- *Direct control:* `touch_command`, `trackpad`, `handwriting_image`, `ping`
- *Settings/UX:* `set_dwell_action`, `set_feature_toggle`, `sensor_switch`, `cursor_pause`, `cursor_resume`, `gesture_assessment`, `pain_day_override`, `calibration_start`, `calibration_cancel`
- *Diagnostics:* `ipad_log`

**PC → iPad (5 types):** `ack` (every message), `status` (window + cursor after each command), `screenshot` (base64 PNG after SCREENSHOT action), `handwriting_result` (LaTeX + unicode after handwriting_image), `recalibration_request` (drift/seasonal re-cal trigger → QuickRecalSheet)

`touch_command` and `trackpad` bypass FusionEngine directly. `handwriting_image` is handled inline by the bridge. `audio_stream` feeds `WhisperStream` → FusionEngine priority 10. `depth_frame` and `camera_frame` are sent by `LiDARStreamer.swift` (enabled via `lidarEnabled` toggle) and routed to `LiDARReceiver` and `GestureProcessor` respectively. `gaze_ray` carries a world-space unit vector `{dx,dy,dz}` at ~10 Hz; `ipad_bridge` stores it and attaches it to the next `gaze_dwell` event so `FusionEngine` can use `GazeCalibrator.project()` for absolute pixel positioning. `gaze_calibration_sample` delivers a dot_index + known pixel + ray during calibration sessions. `ipad_log` batches structured AppLogger entries; warning+ entries are persisted to `ipad_logs` AgentDB table. The remaining sensor types (gaze, head_pose, keyword, etc.) are dispatched to FusionEngine.

## Sensor Priority (FusionEngine — `core/fusion_engine.py`)

1. iPad touch command — bypasses LLM entirely
2. Sound action (mouth sounds via AVFoundation)
3. Gaze delta cursor — relative eye movement drives cursor (no dwell)
4. Gaze + voice "click"
5. Gaze + gesture POINT
6. Tilt navigation (Core Motion)
7. Head tracking (ARKit face anchor)
8. Gesture alone
9. On-device voice keyword (Speech Framework)
10. PC-transcribed voice (Whisper large-v3 on GPU)

## Coding Conventions

- All pipeline classes are `async`; blocking I/O uses `asyncio.to_thread`
- Every sensor class must degrade gracefully — wrap hardware imports in `try/except ImportError`, log a warning, never crash
- No global state outside dataclass instances; all state lives in class attributes
- `Command` is the universal DTO — never pass raw dicts across pipeline boundaries
- Log levels: DEBUG per-frame, INFO commands/routing, WARNING sensor failures, ERROR unrecoverable

## Known Gotchas

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
