# Tasks

## Phase 1 — Core pipeline (voice + coordinator + desktop agent)

- [ ] 1.1 Set up project structure and install core dependencies
      (`faster-whisper`, `sounddevice`, `torch`, `pynvml`, `boto3`, `ollama`, `pyautogui`)

- [ ] 1.2 Implement `hybrid_coordinator.py`
      - `Thresholds` dataclass with all four gate parameters
      - `VRAMMonitor` using `pynvml`
      - `LatencyTracker` with EMA and p95
      - `OutcomeLogger` writing to `routing_log.jsonl`
      - `LocalInference` wrapping Ollama chat with system prompt
      - `CloudInference` wrapping Amazon Bedrock `invoke_model`
      - `HybridCoordinator` with `route()`, `status()`, `update_thresholds()`

- [ ] 1.3 Implement `whisper_stream.py`
      - `SileroVAD` loading from `torch.hub` with 512-sample chunk processing
      - `AudioBuffer` async ring buffer
      - `UtteranceSegmenter` state machine (IDLE → CAPTURING → emit)
      - `WhisperTranscriber` with `faster-whisper`, `float16`, `large-v3`
      - `MicCapture` bridging `sounddevice` callback to async queue
      - `WhisperStream` with concurrent capture + dispatch tasks

- [ ] 1.4 Implement `desktop_agent.py`
      - `ActionParser` splitting verb + target
      - `ElementFinder` with accessibility tree (pywinauto on Windows, pyatspi on Linux)
      - EasyOCR fallback in `ElementFinder._find_via_ocr()`
      - All action handlers: `_click`, `_scroll`, `_type`, `_open`, `_close`, `_hotkey`, `_dictate`, `_clarify`
      - `run_full_pipeline()` wiring coordinator → agent

- [ ] 1.5 Integration test: voice command "open chrome" executes correctly end-to-end

---

## Phase 2 — Gesture pipeline

- [ ] 2.1 Install gesture dependencies (`mediapipe`, `ultralytics`, `opencv-python`)

- [ ] 2.2 Implement `gesture_stream.py`
      - `StaticGestureClassifier` with all 10 gesture rules
      - `DynamicGestureDetector` tracking palm centroid over 500 ms window
      - `LandmarkSmoother` median filter
      - `GestureDebouncer`
      - `CameraCapture` with NVDEC backend and graceful fallback
      - `GestureStream` with per-frame processing in thread

- [ ] 2.3 Integration test: SWIPE_DOWN gesture executes "scroll down" correctly

---

## Phase 3 — Sensor fusion (budget stack)

- [ ] 3.1 Implement `budget_sensor_fusion.py`
      - `FIFINECapture` with device name matching and software noise gate
      - `OAKDLiteCapture` with DepthAI stereo pipeline and post-processing
      - `LeapMotionV1Tracker` with Leap v1 Python SDK
      - `IrisGazeEstimator` with MediaPipe Face Mesh iris landmarks
      - `Gesture3DClassifier` using real mm distances from HandFrame
      - `Swipe3DDetector` using 3D palm velocity
      - `BudgetFusionEngine` with five priority rules
      - `CameraLoop` with OAK-D colour fallback to webcam
      - `BudgetSensorFusion` main runner with graceful sensor degradation

- [ ] 3.2 Implement 9-point gaze calibration routine in `budget_sensor_fusion.py`

- [ ] 3.3 Integration test: point gesture with stable iris gaze executes click at correct screen location

---

## Phase 4 — Continuous training

- [ ] 4.1 Implement `continuous_trainer.py`
      - `LogReader` parsing `routing_log.jsonl`
      - `ThresholdTuner` with gate-specific adaptation rules and bounds
      - `VocabularyBuilder` mining successful transcriptions, writing `hotwords.txt`
      - `GestureCalibrator` tracking per-gesture confidence distributions
      - `FewShotMemory` SQLite with `record_success()` and `retrieve()` by token overlap
      - `PromptAugmenter` patching `LocalInference.infer()` at startup
      - `ContinuousTrainer` with outcome hook and three scheduled loops

- [ ] 4.2 Verify that after 50 interactions, `few_shot_memory.db` contains correct entries
      and augmented prompts include relevant examples

---

## Phase 5 — Full sensor stack

- [ ] 5.1 Implement `sensor_fusion.py`
      - `ReSpeakerCapture` with DOA polling loop
      - `RealSenseCapture` with post-processing filters
      - `UltraleapTracker` with `leapc-cffi` and 200 fps poll loop
      - `TobiiGazeTracker` with SDK callback and 5-frame smoothing
      - `Gesture3DClassifier` (full stack version with Ultraleap HandFrame)
      - `Swipe3DDetector` (3D velocity)
      - `FusionEngine` with five priority rules and gaze stability check
      - `SensorFusion` main runner

- [ ] 5.2 Test that each sensor can be removed and the system degrades to the next fallback

---

## Phase 6 — iPad integration

- [ ] 6.1 Implement `ipad_bridge.py`
      - `BeamGazeTracker` wrapping `eyeware.client.TrackerClient`
      - `IPadLiDARCapture` wrapping `record3d.Record3DSession` with confidence map filtering
      - `IPadWebcam` with IPAD_CAMERA_INDEX env var override
      - `IPadSensorFusion` as drop-in replacement for `BudgetSensorFusion`

- [ ] 6.2 Implement `ipad_touch.py`
      - `TouchInputServer` with `aiohttp` WebSocket + HTTP server
      - `_build_html()` generating the complete single-file Safari web app
      - Command pad with configurable button grid
      - Virtual trackpad with tap and drag detection
      - Dwell activation with CSS ring animation
      - Palm rejection using `radiusX`/`radiusY` touch properties
      - Settings panel persisting to `localStorage`
      - `TouchInputReceiver` routing trackpad moves directly to `pyautogui`
        and other events through the coordinator

- [ ] 6.3 Implement local IP detection and QR code printing in `TouchInputServer`
      (`pip install qrcode` optional)

- [ ] 6.4 Integration test: iPad command pad button "scroll down" executes on desktop

- [ ] 6.5 Integration test: iPad trackpad drag moves cursor proportionally

- [ ] 6.6 Integration test: dwell activation fires after configured timeout without tap

---

## Phase 7 — AWS cloud services wiring

- [ ] 7.1 Configure AWS credentials and region

- [ ] 7.2 Test `CloudInference` routes to Bedrock correctly when Gate 2 fires
      (multi-step command)

- [ ] 7.3 Test `CloudInference` routes to Transcribe when Gate 1 fires
      (low Whisper confidence)

- [ ] 7.4 Add Amazon Polly TTS fallback in `DesktopAgent._clarify()` for cloud path

- [ ] 7.5 Verify that cloud latency is logged in `routing_log.jsonl` and feeds
      the threshold tuner

---

## Phase 8 — Hardening and polish

- [ ] 8.1 Add `--calibrate` flag to `budget_sensor_fusion.py` for 9-point gaze calibration

- [ ] 8.2 Add `--full` flag to `desktop_agent.py` to launch the complete pipeline

- [ ] 8.3 Write a `requirements.txt` with pinned versions for all dependencies

- [ ] 8.4 Add startup status summary table printed to terminal showing which sensors
      are connected vs falling back

- [ ] 8.5 Ensure graceful Ctrl-C shutdown: save `gesture_calibration.json`,
      flush `routing_log.jsonl`, stop all sensor streams

- [ ] 8.6 Add `IPAD_CAMERA_INDEX` environment variable documentation to README

- [ ] 8.7 Write `README.md` with hardware setup checklist, dependency install commands,
      and quick-start for each stack variant (full / budget / iPad)
