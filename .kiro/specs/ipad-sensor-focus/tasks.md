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

- [ ] **1.6 Integration test: touch command "scroll down" executes end-to-end**

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
  - Manual IP entry in SettingsView; mDNS discovery deferred to integration test phase

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

- [ ] **2.11 Integration test: gaze dwell fires click on desktop target**

- [ ] **2.12 Integration test: tilt navigation moves cursor proportionally**

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

- [~] **2.13 Benchmark `OllamaInference` vs `VLLMInference` vs `NemotronInference` on RTX 5090**
  - Partial: benchmarked 5 Ollama models (2026-05-08) via `benchmark_models.py`
  - **Results:** `llama3.2:3b` and `llama3.1:8b` both 100% accuracy; `nemotron-mini` 25% (wrong format);
    `deepseek-r1:8b` and `gpt-oss:20b` 0% (reasoning models incompatible with structured output)
  - **Default updated to `llama3.2:3b`** (2.0 GB, 100% accuracy, smallest footprint)
  - Observed p50 ~2.2 s for all models — believed to be Ollama `stream=False` overhead,
    not GPU inference time. Actual time-to-first-token on RTX 5090 expected <50 ms.
  - **Still open:** vLLM backend full implementation + latency profiling with streaming;
    `nemotron` 70B with RAM offload; production p95 target <350 ms validation
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

- [ ] **N.5 Analyse `routing_log.jsonl` after 1-week soak to tune gate thresholds**
  - Parse log; count decisions per `gate_that_decided` label
  - If >20% of commands hit `gate2_complexity` → lower `max_local_tokens` or loosen keyword list
  - If `gate3_vram` never fires → consider raising `vram_free_min_gb` further
  - If `gate4_latency` fires often → investigate inference backend, not the budget number

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
