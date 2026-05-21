# Personal Desktop Agent

Multimodal accessibility desktop control for a single user with rheumatoid arthritis. An iPad Pro (2020+) is the sensor hub and primary touch surface; a Windows PC with RTX 5090 runs inference and executes desktop actions.

## What This Is

The user controls a Windows desktop through voice, eye gaze, head pose, hand gesture, iPad tilt, mouth sounds, and direct touch — all mapped to a 16-verb action vocabulary (11 accessibility + 5 dev-agent). Sensor data streams over WebSocket from a native Swift iPad app to a Python backend on the PC. The PC runs local LLM inference (Ollama → vLLM in production) and executes commands via pyautogui/Win32.

- Full requirements (17): `.kiro/specs/ipad-sensor-focus/requirements.md`
- Architecture diagrams (13): `.kiro/specs/ipad-sensor-focus/diagrams/00-index.md`
- Tech stack: `.kiro/steering/tech.md`
- Open tasks: `.kiro/specs/ipad-sensor-focus/tasks.md`
- Daily reviews: `docs/`

## Current Status — Phases 1–6 complete + sensor-refinement + gesture-rewrite (2026-05-19)

**Done (Phase 1):** `ipad_bridge.py`, `command_executor.py`, `mcp_server/` (5 tool modules + MCP server), `tests/test_bridge_client.py`, `tests/test_touch_scroll_e2e.py`, `requirements.txt`

**Done (Phase 2):**
- `fusion_engine.py` — 10-level priority sensor fusion at 60 Hz; gaze delta cursor integration (relative eye movement → cursor), sound actions, tilt/head direct-to-pyautogui
- `hybrid_coordinator.py` — 4-gate routing (Gate 0 privacy + Gates 1–4); outcome logging to `agent.db`
- `local_inference.py` — `LocalInference` ABC + `OllamaInference` (default, 100% accuracy, 373ms warm p50), `VLLMInference` (production-ready code; needs CUDA 13.x torch wheels to activate on RTX 5090)
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

**Test suite (2026-05-19):** 262 pytest tests + 30 standalone integration scripts + 15 Swift XCTest files = 307 total

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
| `ipad_bridge.py` | aiohttp WebSocket server on :8765; routes 15 incoming message types; sends `ack`, `status`, `screenshot`, `handwriting_result` replies |
| `command_executor.py` | Maps 16 action verbs to mcp_server tool calls; `_resolve_coords` falls back to screen centre; SCREENSHOT defaults to active window and copies to Windows clipboard |
| `mcp_server/desktop_mcp_server.py` | MCP stdio server; 14 tools; `SAFE_MODE` env var |
| `mcp_server/tools/mouse.py` | move, click, double_click, scroll, drag |
| `mcp_server/tools/keyboard.py` | type, hotkey, press, paste (unicode via clipboard) |
| `mcp_server/tools/screen.py` | screenshot (base64 PNG), get_screen_size, find_text_on_screen (OCR) |
| `mcp_server/tools/windows.py` | get_active_window, list_windows, focus_window (win32gui + psutil) |
| `mcp_server/tools/handwriting.py` | pix2tex LaTeX OCR; latex_to_unicode fallback converter |
| `fusion_engine.py` | 60 Hz tick loop; 10-level sensor priority; direct pyautogui for tilt/head |
| `hybrid_coordinator.py` | 4-gate routing (Gate 0 privacy + Gates 1–4); AWS Bedrock fallback; outcome logger |
| `local_inference.py` | `LocalInference` ABC; `OllamaInference` (default, 373ms warm p50), `VLLMInference` (complete; needs CUDA 13.x torch to activate) |
| `continuous_trainer.py` | Routing threshold adaptation; few-shot ranking; gesture velocity-floor calibration (p10 observed, −30% pain day); delegates all storage to `AgentDB`; holds `gesture_processor=` ref for live threshold push-back |
| `lidar_receiver.py` | Decodes depth_frame messages; confidence-map filtering; `get_depth_at()` |
| `behavioral_twin_state.py` | Persistent user behaviour model: `TwinSnapshot`, `PreferenceModel`, `PainDayEngine`; AgentDB + ChromaDB backing; feeds `HybridCoordinator` before every gate decision |
| `semantic_memory.py` | ChromaDB vector store (all-MiniLM-L6-v2) for semantic few-shot retrieval; Jaccard fallback when chromadb unavailable; `stop()` releases WAL file handles on Windows |
| `one_euro_filter.py` | Casiez 2012 adaptive low-pass filter (1€); used for tilt velocity, tilt position, gaze delta, head tracking — replaces EMA throughout sensor pipelines |
| `gyro_bias_calibrator.py` | Gyro bias state machine (UNCALIBRATED→COLLECTING→CALIBRATED→FROZEN); stationary detection + lerp-smoothed bias subtraction for tilt velocity pipeline |
| `gesture_processor.py` | MediaPipe Tasks API (`HandLandmarker`, `hand_landmarker.task`); peace-sign base pose; 13 gestures (swipe/grab/snap/monitor/push-pull/pinch); 500ms rolling buffer; velocity learning; 800ms debounce |
| `domain_classifier.py` | Keyword-scoring domain detection: COMMAND/CODE/MATH/VISION/PLAN/GENERAL |
| `model_router.py` | VRAM-aware specialist model selection; domain-tuned prompts; Ollama inference |
| `dev_agent.py` | Plan→execute→reflect agentic loop; 5 dev verbs; session context |
| `main.py` | Unified entry point; `--measure-vram`; `--viewer`/`--viewer-only`; startup status table; Ctrl-C shutdown |
| `sensor_viewer.py` | tkinter desktop window (daemon thread); camera + LiDAR depth side-by-side; hand landmark overlay; gaze cursor overlay; freeze-frame; depth-at-cursor readout; always-on-top toggle |
| `whisper_stream.py` | GPU-accelerated speech: Silero VAD + faster-whisper large-v3; emits `Command(source="voice")` to FusionEngine |
| `db.py` | `AgentDB` (aiosqlite, 14 tables, all pipeline writes) + `AnalyticsDB` (DuckDB, benchmark history); MiniLM semantic retrieval; gesture velocity tables for continuous calibration |
| `tests/test_bridge_client.py` | Simulated iPad client; sends 8 test messages; verifies ack for each |
| `polly_stream.py` | Python TTS client — HTTP to Node.js sidecar; `speak_sync()` for threads, `speak()` async, `speak_stream()` for token-by-token; auto-starts sidecar; `get_client(backend=)` dispatches to Chatterbox when configured |
| `chatterbox_tts.py` | Local GPU TTS backend (RTX 5090); `ChatterboxClient` with same interface as `PollyStreamClient`; emotion exaggeration, paralinguistic tags, zero-shot voice cloning |
| `tts_service/server.js` | Node.js sidecar (port 8766); calls `StartSpeechSynthesisStream` (AWS SDK v3); returns OGG Vorbis; Python decodes with soundfile |
| `approval_hook.py` | Claude Code `PreToolUse` gate; Danielle speaks action description; records iPad mic via WhisperStream signal file or PC mic fallback; yes/no → exit 0/2 |
| `audit_log.py` | Append-only `audit.db` (SQLite WAL); records every MCP tool invocation, session lifecycle event, and security finding; UPDATE/DELETE blocked by triggers |
| `approval_config.json` | Per-tool approval policy (`"approve"` / `"silent"`), voice, mic device (`"Microphone (Realtek USB Audio)"`), timeout, tts_backend |
| `start_agent.bat` | Windows startup script; activates venv and runs `main.py`; logs to `logs/agent_startup.log` |

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
| `polly_stream.py` → `tts_service/server.js` | Generative 24kHz | `approval_config.json` → POST body | CLARIFY questions, DevAgent EXPLAIN |
| `chatterbox_tts.py` (via `polly_stream.get_client()`) | Local GPU | exaggeration/cfg in `approval_config.json` | When `tts_backend == "chatterbox"` |
| `approval_hook.py` `_polly_speak()` | Neural 16kHz | `approval_config.json` `voice_id` | "Approve write to…?" gate |
| `command_executor.py` `_polly_speak()` | Neural 16kHz | `_POLLY_VOICE` constant | Sidecar-down fallback |

### iPad mic approval flow

When the bridge is running, Danielle's question plays through PC speakers, then
the next utterance into the **iPad mic** is captured by WhisperStream and routed
to the approval gate via `~/.claude/approval/pending` + `response` signal files.
If the bridge is not running, the PC's **Microphone (Realtek USB Audio)** mic is
used instead (4-second recording window, auto-approve on silence).

## WebSocket Protocol

**iPad → PC (15 types):** `tilt`, `tilt_position`, `gaze`, `gaze_delta`, `gaze_dwell`, `head_pose`, `keyword`, `sound_action`, `touch_command`, `trackpad`, `audio_stream`, `camera_frame`, `depth_frame`, `handwriting_image`, `tilt_tap`

**PC → iPad (4 types):** `ack` (every message), `status` (window + cursor after each command), `screenshot` (base64 PNG after SCREENSHOT action), `handwriting_result` (LaTeX + unicode after handwriting_image)

`touch_command` and `trackpad` bypass FusionEngine directly. `handwriting_image` is handled inline by the bridge. `audio_stream` feeds `WhisperStream` → FusionEngine priority 10. `depth_frame` and `camera_frame` are sent by `LiDARStreamer.swift` (enabled via `lidarEnabled` toggle) and routed to `LiDARReceiver` and `GestureProcessor` respectively. The remaining sensor types (gaze, head_pose, keyword, etc.) are dispatched to FusionEngine.

## Sensor Priority (FusionEngine — `fusion_engine.py`)

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
- Default LLM is `llama3.1:8b` (4.6 GB VRAM) for command, plan, and general domains. `nemotron-mini` scored 25% and is not suitable without fine-tuning. `deepseek-r1:8b` produces reasoning output incompatible with the verb-first format. `gpt-oss:20b` removed from primary profiles (retained in fallback chains only).

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
