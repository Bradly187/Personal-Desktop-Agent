# Tasks — iPad Sensor Focus Spec

---

## Phase 1 — Core infrastructure (PC-side)

- [x] **1.1 Implement `LocalInference` ABC**
  - Create abstract base class with `async infer(cmd: Command) -> str` and `get_status() -> dict`
  - Rename current Ollama implementation to `OllamaInference(LocalInference)`
  - Wire `HybridCoordinator` to `LocalInference` interface (not the concrete class)
  - Benefit: swap backends without touching coordinator code

- [x] **1.2 Measure actual VRAM consumption on RTX 5090** (2026-05-08)
  - Tool: `python main.py --measure-vram` (pynvml / nvidia-ml-py snapshots)
  - Baseline: 8.3 GB (OS + desktop + GPU drivers — 4.8 GB more than estimated)
  - Whisper large-v3: +4.2 GB (was estimated 3 GB)
  - Ollama qwen3-vl:30b (18.2 GB): fills GPU to 31.7 GB alongside Whisper
  - YOLOv8-pose: not measured (ultralytics not installed — Task 4.5 comment)
  - **Key finding:** llama3.1:70b (~40 GB) cannot co-reside with Whisper on 31.8 GB VRAM
  - Tables updated in `diagrams/05-data-flow.md` §6 and `local-inference-comparison.md`
  - Default model in OllamaInference updated to `llama3.1:8b` (pulled, fits with Whisper)

- [x] **1.3 Implement `IPadBridge`**
  - `aiohttp` WebSocket server on port 8765
  - Dispatcher for all 12 message types including `tilt_tap` and `handwriting_image`
  - Trackpad drag/tap events route directly to `pyautogui` (bypass `FusionEngine`)
  - All sensor events dispatched to `FusionEngine`; stubs for audio_stream/camera_frame/depth_frame
  - Bonjour/mDNS advertisement + QR code printed to terminal on start

- [x] **1.4 Implement `FusionEngine` (10-level priority)**
  - Tick loop at 60 Hz via `asyncio`
  - All 10 priority rules as documented in `diagrams/06-fusion-routing.md`
  - Tilt and head-pose deltas sent directly to `pyautogui` (no `Command` emitted)
  - Gaze stability buffer (spread < 4% of screen diagonal) feeding dwell timer
  - Unit tests: each rule fires correctly when higher-priority sources are absent

- [x] **1.5 Implement updated `HybridCoordinator`**
  - Accept `LocalInference` ABC (not `OllamaInference` directly)
  - Recognise new `source` tags: `sound_action`, `gaze_dwell`, `tilt`, `head_track`, `voice_local`
  - `touch`, `sound_action`, `gaze_dwell`, `multimodal` bypass all four gates
  - `voice_local` skips Gate 1 only
  - 4-gate routing: confidence → complexity → VRAM (pynvml) → latency EMA → local or Bedrock
  - Outcome logging to `routing_log.jsonl`

- [x] **1.6 Integration test: touch command "scroll down" executes end-to-end**
  - `tests/test_touch_scroll_e2e.py` — standalone async test (no pytest)
  - Sends `touch_command` SCROLL/CLICK over WebSocket → verifies `mouse_scroll`/`mouse_click` called
  - Mocks desktop tool functions; validates direction, clicks, coordinates
  - Also tests SCROLL up, default params, and CLICK with explicit coords

---

## Phase 2 — iPad native app (Swift/SwiftUI)

- [x] **2.1 Set up Xcode project**
  - SwiftUI app targeting iPadOS 17+
  - Capabilities: Background Modes (audio), Speech Recognition, Camera, Motion
  - Source files written; see `iPadApp/SETUP.md` for Xcode project creation steps

- [x] **2.2 Implement `WebSocketManager`**
  - `URLSessionWebSocketTask` persistent connection
  - Exponential backoff reconnect (1s → 2s → 4s → 8s → 30s max)
  - Connection status indicator (green/yellow/red) via `ConnectionState` enum + `ConnectionBanner`
  - Manual IP entry in SettingsView; mDNS discovery implemented in iPad App Hardening spec (ServiceDiscovery.swift)

- [x] **2.3 Implement `TiltSensor`**
  - `CMMotionManager.startDeviceMotionUpdates` at 60 Hz
  - Dead-zone filtering before sending `tilt` WebSocket messages
  - Accelerometer impulse detection for table-tap → `tilt_tap` event

- [x] **2.4 Implement `GazeTracker`**
  - `ARSession` with `ARFaceTrackingConfiguration`
  - Stream `gaze` messages at ARKit frame rate
  - Dwell timer on-device: send `gaze_dwell` when gaze stable ≥ configured duration
  - `DwellRingView` SwiftUI overlay with progress animation

- [x] **2.5 Implement `HeadTracker`**
  - Extract pitch/yaw from `ARFaceAnchor.transform`
  - Stream `head_pose` delta messages (not absolute angles)
  - Smoothing factor configurable via `SettingsStore`

- [x] **2.6 Implement `KeywordListener`**
  - `SFSpeechAudioBufferRecognitionRequest` continuous on-device recognition
  - Match against configurable keyword list from `SettingsStore`
  - Send `keyword` message on match

- [x] **2.7 Implement `SoundDetector`**
  - `AVAudioEngine` tap on input bus + FFT via vDSP
  - Pattern classifiers for cluck, pop, hiss (onset + spectral shape heuristics)
  - 500 ms debounce; configurable `soundMappings` from `SettingsStore`

- [x] **2.8 Implement `CommandPadView`**
  - Configurable button grid, minimum 80×80 pt targets with `.contentShape(Rectangle())`
  - Flash animation on tap; edit/reorder/delete via `CommandPadEditorView`
  - Dwell via gaze pipeline (server-side)
  - Palm rejection via `UITouch.majorRadius` threshold

- [x] **2.9 Implement `TrackpadView` (including full-screen mode)**
  - Single-finger drag → `trackpad` move
  - Single-tap → left click; two-finger tap → right click
  - Two-finger drag → scroll
  - Full-screen toggle; palm rejection via `UITouch.majorRadius` threshold

- [x] **2.10 Implement `SettingsStore`**
  - Persist to `UserDefaults`: all sensor preferences, keyword list, sound mappings, command buttons
  - `SettingsView` with Dynamic Type Form layout

- [x] **2.11 Integration test: gaze dwell fires click on desktop target**
  - `tests/test_gaze_dwell_click.py` — standalone async test (no pytest)
  - Full pipeline: WebSocket → IPadBridge → FusionEngine Rule 3 → HybridCoordinator bypass → CommandExecutor
  - Mocks LLM (returns "CLICK") and `mouse_click`; verifies gaze coords map to screen pixels
  - Tests center, top-left, bottom-right positions + gate bypass verification

- [x] **2.12 Integration test: tilt navigation moves cursor proportionally**

- [x] **2.15 Implement `HandwritingCanvasView`**
  - `PKCanvasView` with `.pencilOnly` policy — finger touches pan, Pencil draws
  - Render `PKDrawing` to PNG on Recognise tap → base64 → `handwriting_image` WebSocket message
  - Receive `handwriting_result`; display LaTeX + editable unicode field; send via DICTATE
  - Clear and Undo controls; Recognise button disabled while empty/in-progress
  - Tab entry in ContentView alongside CommandPad, Trackpad, Keypad, Settings

- [x] **2.14 Implement `ScientificKeypadView`**
  - Scrollable monospace expression display with live NSExpression preview
  - Basic mode: digits, operators, parentheses
  - Scientific mode: trig + inverses, log/ln/log₂, √, ^, π, e, abs, !, mod, EE, ±, ANS
  - All buttons minimum 64×64 pt with `.contentShape(Rectangle())`
  - Send button emits DICTATE with raw expression string

---

## Phase 2 (PC) — vLLM / Nemotron evaluation

- [x] **2.13 Benchmark `OllamaInference` vs `VLLMInference` vs `NemotronInference` on RTX 5090**
  - Benchmarked 10 Ollama models (2026-05-13) via `benchmark_models.py --runs 2`
  - **Results (2026-05-13):** `llama3.1:8b`, `llama3.2:3b`, `qwen3-coder:30b` → 100% accuracy;
    `qwen2.5-coder` 83%; `nemotron-mini` 25%; `gpt-oss:20b` 0%
  - **Latency (2026-05-15 — definitive):** via `OllamaInference.infer()` aiohttp after fresh
    model load (7 prompts, cold then warm, RTX 5090):
      cold start (model load + 1st request): 2556ms | warm p50: 373ms | warm p95: 411ms
    Note: benchmark_models.py uses urllib (creates new connection per request) which adds
    ~1800ms overhead vs. aiohttp — benchmark numbers are useful for MODEL COMPARISON but not
    absolute latency. Real production latency is via OllamaInference → 373ms p50 warm.
  - **Promotion decision:** Ollama warm p95=411ms — 17% above 350ms target, acceptable for a
    single-user accessibility app (touch/gaze/sound bypass LLM; only voice hits this path).
    Ollama remains default. VLLMInference ready to activate when CUDA 13.x torch wheels publish
    (current RTX 5090 driver uses CUDA 13.2; CUDA 12.x wheels do not install vllm._C on this GPU).
  - **VLLMInference fully implemented** (`local_inference.py:179–285`) — async lazy load via
    `asyncio.to_thread`, `asyncio.Lock` double-check, UUID request IDs, per-request 15s timeout,
    `gpu_memory_utilization=0.50` (leaves room for Whisper alongside), graceful `ImportError` path.
    Activate by setting `VLLMInference()` as the `local=` arg in `HybridCoordinator`.
  - **benchmark_models.py** extended with `--vllm <HF_MODEL>` flag to compare backends side-by-side
  - See `local-inference-comparison.md` for full benchmark table

---

## Phase 2 (PC) — NemoClaw integration (from NVIDIA GTC 2026 review)

- [x] **N.1 Add `gate_that_decided` to `routing_log.jsonl`**
  - Replaced `gate_failed: int | None` with `gate_that_decided: str` in `_OutcomeLogger`
  - Labels: `"bypass"`, `"gate0_privacy"`, `"gate2_complexity"`, `"gate3_vram"`, `"gate4_latency"`, `"all_pass"`, `"discard"`
  - Enables log analysis: filter by label to tune individual gate thresholds

- [x] **N.2 Add Gate 0 (privacy/sensitivity check) to `HybridCoordinator`**
  - Runs before bypass and Gate 1; forces local routing when command text matches sensitive patterns
  - Patterns: password, token, api key, credit card, SSN, routing number, private key, SSH key, etc.
  - Configurable via `CoordinatorConfig.gate0_enabled` and `gate0_sensitive_patterns`
  - Rationale: NemoClaw privacy router concept — sensitive data must never leave the device

- [x] **N.3 Add `NemotronInference(LocalInference)` to `local_inference.py`**
  - Thin subclass of `OllamaInference` with Nemotron model defaults
  - Default model: `nemotron-mini` (4B, ~4 GB VRAM — leaves 28 GB free on RTX 5090)
  - Also supports `nemotron` (70B, needs RAM offload via llama.cpp on this machine's 192 GB RAM)
  - Install: `ollama pull nemotron-mini`

- [x] **N.4 Raise `CoordinatorConfig.vram_free_min_gb` from 4.0 → 8.0 GB**
  - 4 GB floor was conservative; with 32 GB VRAM, 8 GB free is still 75% utilisation headroom
  - Comment in config explains this is tuned for RTX 5090; lower for smaller GPUs

- [x] **N.5 Analyse routing data (agent.db) — 2026-05-15 review**
  - **Data:** 22 commands across 7 sessions (mostly integration test artefacts); too sparse
    for production threshold changes but healthy distribution confirmed.
  - **Gate distribution:** bypass 91% (20) | gate2_complexity 9% (2) | gates 0/1/3/4: 0%
  - **Breakdown:**
    - 14 gaze_dwell/bypass → all CLICKs, <2ms routing ← integration tests (2026-05-11)
    - 2 voice/gate2_complexity → "close window and open notepad" → CLOSE ← multi-step voice
    - 6 touch/bypass → CLARIFY (2285ms) ← bridge_client SCREENSHOT+DICTATE test artefacts
  - **Threshold decisions (2026-05-15):**
    - gate2_complexity 9% < 20% threshold → NO CHANGE to max_local_tokens or keyword list
    - gate3_vram never fired → VRAM floor (8.0 GB) is fine; RTX 5090 headroom adequate
    - gate4_latency never fired → 350ms budget holds; no backend investigation needed
    - gate0_privacy never fired → no sensitive commands issued (expected)
  - **Action:** Revisit after 200+ real-world voice commands for meaningful tuning.
    The 6 CLARIFY failures are SCREENSHOT+DICTATE test artefacts from 2026-05-07
    (CommandExecutor win32 clipboard ops slow at cold start) — not a routing problem.

---

## Phase 6 — Cloud fallback (agentcore_fallback/)

- [x] **6.1 Raw Bedrock path — activate and verify (2026-05-15)**
  - Updated model: `anthropic.claude-3-5-haiku-20241022-v1:0` (legacy) →
    `us.anthropic.claude-haiku-4-5-20251001-v1:0` (active cross-region profile)
  - `_CloudInference.infer()` now uses proper system/messages format for Claude 4.x
  - `_CLOUD_SYSTEM_PROMPT` extends base vocab with voice misrecognition guidance
  - 8/8 accuracy on disambiguation test (clothes→CLOSE, scroll done→SCROLL down, etc.)
  - `agentcore_enabled: False` by default — raw Bedrock is active cloud tier

- [x] **6.2 Gate 1 re-transcription — replace stub (2026-05-15)**
  - Stage 1: `_apply_vocabulary_corrections()` — instant phonetic fix for 6 common
    misrecognitions (no deps, 0ms overhead)
  - Stage 2: Amazon Transcribe streaming — activated when `pip install amazon-transcribe`;
    3s timeout; falls back to Stage 1 gracefully
  - `WhisperStream.preserve_audio=True` (default) stores int16 audio bytes +
    sample_rate in `Command.params` for Transcribe re-use
  - `dataclasses.replace()` resets `whisper_logprob=0.0` after correction so command
    clears Gate 1 on retry

- [x] **6.3 Cloud-path integration tests (2026-05-15)** — `tests/test_cloud_path.py`
  - 8 tests: real Bedrock call, Gate 2 routing, misrecognition, bad-creds degradation,
    Gate 0 privacy block, AgentCore→Bedrock fall-through, Gate 1 vocab + retranscribe

- [ ] **6.4 AgentCore Tier 1 deployment — source deleted, permanently deferred**
  - `agentcore_fallback/` directory deleted 2026-05-19 (dead code removal sprint)
  - Raw Bedrock (6.1) is the active and permanent cloud path
  - AgentCore LTM memory partially overlaps `ContinuousTrainer` + `semantic_memory.py`; net value too low to re-implement

- [ ] **N.6 Evaluate Nemotron-4 340B with RAM offload (stretch goal)**
  - 192 GB RAM + 32 GB VRAM on this machine makes llama.cpp offloaded 340B feasible
  - Adds a third inference tier: fast-small (VRAM-only ≤8B) → slow-large (RAM-offloaded 340B) → cloud
  - Requires restructuring `HybridCoordinator` to support a ternary local tier choice
  - Only pursue if nemotron-mini quality proves insufficient for command classification

---

## Phase 3 — LiDAR depth integration

- [x] **3.1 Implement `LiDARReceiver` on PC** (`lidar_receiver.py`)
  - Decodes float32 depth + uint8 confidence from base64 WebSocket message
  - Confidence-map filtering (NaN-masks pixels below `conf_min`; default=1/medium+)
  - `get_depth_at(nx, ny)` — bilinear sample at normalised coords, returns metres or None
  - `is_fresh(max_age_s)` — tells GestureProcessor whether depth data is current
  - Wired into IPadBridge `depth_frame` handler

- [x] **3.2 Implement `GestureProcessor`** (`gesture_processor.py`)
  - MediaPipe Hands (single hand, dynamic mode); degrades gracefully without mediapipe/opencv
  - Classifies: POINT, PINCH, OPEN_PALM, FIST from landmark geometry
  - Confidence gate (≥0.65); 800 ms per-gesture debounce
  - LiDAR integration: when `LiDARReceiver.is_fresh()`, uses real mm pinch distance;
    rejects PINCH if 3D distance > 30 mm threshold
  - 2D normalised-coord fallback when depth unavailable
  - Emits `Command(source="gesture", params={"gesture": name, ...})` to FusionEngine
  - Wired into IPadBridge `camera_frame` handler

---

## Phase 4 — Continuous learning + hardening

- [x] **4.1 Implement `ContinuousTrainer`**
  - SQLite (aiosqlite) few-shot DB: record_success, get_few_shot_examples
  - Token-overlap × recency × log(usage) ranking
  - Gate 1 threshold auto-relaxation (cloud rate > 30% + local failure < 10%)
  - Whisper hotwords promotion (≥3 successful uses)
  - Gesture confidence floor calibration (p10 - 0.05, saved to gesture_calibration.json)
  - Wired into HybridCoordinator (few-shot injection, success recording)

- [x] **4.2 Add `--measure-vram` flag to main entry point** (`main.py`)
  - Loads Whisper large-v3, YOLOv8-pose, triggers Ollama llama3.1:70b
  - Prints pynvml snapshot after each model load; exits
  - Used to produce the measurement required in task 1.2

- [x] **4.3 Graceful shutdown (Ctrl-C)** (`main.py` `_ShutdownController`)
  - SIGINT/SIGTERM handler sets asyncio Event; main loop awaits it
  - Saves `gesture_calibration.json`, stops trainer (flushes DB)
  - Cancels bridge and FusionEngine tasks, then exits cleanly

- [x] **4.4 Startup status table** (`main.py` `_print_startup_table`)
  - Checks and reports: GPU/VRAM, Ollama, Whisper, Tesseract, pix2tex, mDNS, SAFE_MODE
  - Printed before bridge starts; suppressible with `--quiet`

- [x] **4.5 Pin `requirements.txt`** with installed versions; added `pynvml>=11.5.0` and
  `aiosqlite>=0.19.0`; not-yet-installed packages documented as comments

---

## Sprint A — Acoustic Profiler + per-user VAD (2026-05-20)

- [x] **A.1 Implement `acoustic_profiler.py`** — passive per-user calibration
  - Measures RMS amplitude, spectral centroid, Whisper logprob per utterance
  - Derives per-user `vad_threshold` and `logprob_floor`; scales on flare days
  - Voice clarity as Signal 5 in `PainDayEngine`; drift detection; seasonal re-cal prompt
  - 6 new AgentDB tables: `voice_calibration`, `voice_profile`, `voice_phrases`, `sensor_rom`, `flare_profile`, `ambient_transcripts`

- [x] **A.2 Implement `voice_calibrator.py`** — guided condition-aware calibration
  - 4 conditions (good_day / flare_day / allergy_day / svt_attack); 20 full phrases
  - Voice-triggered (`"hey agent run voice calibration"`) + iPad Settings tab
  - Quick session: 5 phrases, ~90s (`"hey agent quick calibration"`)

- [x] **A.3 Wire AcousticProfiler into WhisperStream + HybridCoordinator**
  - `whisper_stream.set_acoustic_profiler()` → per-utterance sample recording
  - `_silence_thresh` updated live from profiler on pain day override

---

## Sprint B — iPad Accessibility Onboarding UI (2026-05-20)

- [x] **B.1 `VoiceProfilingSheet.swift`** — 10 phrases × 3 repeats; 4s countdown; passive profiling
- [x] **B.2 `GestureAssessmentSheet.swift`** — rates 4 gestures; disabled list synced to PC
- [x] **B.3 `FlareProfileSheet.swift`** — sensor degradation config + manual pain day toggle; syncs via `pain_day_override` WebSocket in <100ms
- [x] **B.4 `QuickRecalSheet.swift`** — 3 phrases × 3 repeats; auto-shown on drift/seasonal prompt via `wsManager.recalibrationFeed`
- [x] **B.5 `OnboardingView.swift`** — expanded 7 → 10 steps (all 3 new sheets optional/skippable)

---

## Sprint C — Continuous Recalibration (2026-05-20)

- [x] **C.1 Drift detection** — every 20 samples: clarity drop ≥30% → `bridge.send_recalibration_request()` → `QuickRecalSheet`
- [x] **C.2 Seasonal prompt** — every 50 commands: >30 days since last cal → same path (`reason="seasonal"`)
- [x] **C.3 `ipad_bridge.py` `pain_day_override` handler** — routes to `BehavioralTwinState` → `AcousticProfiler` → `WhisperStream` in single sync chain

---

## Sprint 5 — Vision Grounding (2026-05-20)

- [x] **5.1 Implement `vision_grounder.py`** — Claude vision API; confidence ≥0.7; 2s cache; fallback chain
  - Hooked into `HybridCoordinator._execute_action` for CLICK with named target
  - Expected CLICK success: 42% → ~78%
  - 11 new tests in `tests/test_vision_grounder.py`

---

## Sprint 6 — UIAutomation (2026-05-20)

- [x] **6.A Implement `ui_automation.py`** — Win32 UIAutomation BFS; fuzzy scoring; 0.3s timeout; 1s cache
  - First fallback in `command_executor._resolve_coords` before vision grounder
  - Expected CLICK success: ~78% → ~88%

---

## Sprint 7 — Action Verification (2026-05-20)

- [x] **7.1 Implement `action_verifier.py`** — Pillow perceptual diff; 2% threshold; 400ms animation delay
  - Wraps CLICK/OPEN/CLOSE/SCROLL in `command_executor.execute()`
  - Expected CLICK success: ~88% → ~92%

---

## Sprint #1 — Test coverage for Sprint 6 + 7 (2026-05-21)

- [x] **#1.1 `tests/test_ui_automation.py`** — 29 tests
  - `UIElement.center()/width()/height()`; `_detect_app()` (6 cases); `_score()` (all 5 tiers); `UIAutomationProvider` cache hit/miss/expiry, exception path, successful caching, status
- [x] **#1.2 `tests/test_action_verifier.py`** — 22 tests
  - `VerifyResult` fields; skip paths (TYPE/HOTKEY/DICTATE/empty/unavailable); error path (failed snapshot); `_diff()` (identical/different/size-mismatch/noise floor); `verify()` end-to-end for CLICK/OPEN/SCROLL/CLOSE; status dict

---

## Sprint G1–G4 — Gaze-to-monitor absolute positioning (2026-05-21)

Physical setup: iPad in landscape mode, front camera resting on rolltop desk, ~6 inches below monitor center. Chair height is fixed — calibration is permanent (one-time setup).

Approach: angular affine mapping (azimuth/elevation offsets from reference gaze direction → screen pixel via numpy least squares). 5-point calibration: top-left, top-right, center, bottom-left, bottom-right.

- [x] **G1 World-space gaze ray extraction (Swift)**
  - `GazeTracker.swift` — `currentWorldRay: (origin, dir)` property; `faceAnchor.transform * eyeTransform` world-space extraction; 10 Hz `gaze_ray` send (every 6th frame)
  - `WebSocketManager.swift` — `sendGazeRay(dx:dy:dz:confidence:)` → `{"type": "gaze_ray", ...}`

- [x] **G2 PC-side gaze calibrator**
  - `gaze_calibrator.py` — `GazeCalibrator`: `add_sample()`, `solve()` (numpy lstsq, tangent plane projection), `project()`, `load()`/`save_to_db()`
  - `db.py` — `gaze_monitor_calibration` table (21st AgentDB table); `upsert_gaze_calibration()`, `get_gaze_calibration()`

- [x] **G3 Calibration protocol + PC overlay**
  - `calibration_overlay.py` — tkinter full-screen overlay; 5 dots with advance/finish/cancel API; daemon thread
  - `ipad_bridge.py` — `gaze_ray` handler (stores ray + timestamp); `gaze_dwell` attaches fresh ray (< 300ms); `gaze_calibration_sample` handler; `set_gaze_calibrator()` wiring

- [x] **G4 Runtime absolute dwell positioning**
  - `fusion_engine.py` — `set_gaze_calibrator()`; `on_gaze_dwell(ray_dir=)` overrides normalised coords with `calibrator.project()`
  - `main.py` — `GazeCalibrator` load at startup; startup table row; wired to bridge + fusion
  - `tests/test_gaze_calibrator.py` — 22 tests (sample management, solve, project, persistence, DB)

- [ ] **G5 Voice trigger + calibration UX (next session)**
  - Voice command `"hey agent calibrate monitor"` → TTS guidance → `CalibrationOverlay` → 5-dot dwell flow → `solve()` → TTS residual report
  - `MonitorCalibrationSheet.swift` — iPad Settings UI: calibration status, "Calibrate Monitor" button, progress view
  - Wire into `OnboardingView` as optional step 11
