# Design

## Architecture overview

```
┌──────────────────────────────────────────────────────────────────┐
│                         INPUT LAYER                              │
│                                                                  │
│  MicCapture          CameraCapture       iPad sensors            │
│  (ReSpeaker/FIFINE)  (D455/OAK-D/iPad)  (LiDAR/gaze/touch)     │
└──────────┬───────────────────┬──────────────────┬───────────────┘
           │                   │                  │
           ▼                   ▼                  ▼
┌──────────────────┐ ┌──────────────────┐ ┌──────────────────────┐
│  WhisperStream   │ │  GestureStream   │ │  IPadSensorFusion /  │
│                  │ │                  │ │  TouchInputServer    │
│  Silero VAD      │ │  YOLOv8 pose     │ │                      │
│  Utterance seg.  │ │  MediaPipe hands │ │  BeamGazeTracker     │
│  Whisper CUDA    │ │  3D classifier   │ │  IPadLiDARCapture    │
│  → Command       │ │  Swipe detector  │ │  TouchInputReceiver  │
└──────────┬───────┘ │  → Command       │ └──────────┬───────────┘
           │         └────────┬─────────┘            │
           │                  │                      │
           └──────────────────▼──────────────────────┘
                              │
                    ┌─────────▼──────────┐
                    │   FusionEngine     │
                    │                   │
                    │  Priority rules:  │
                    │  1. Touch         │
                    │  2. Gaze+voice    │
                    │  3. Gaze+gesture  │
                    │  4. Gesture alone │
                    │  5. Voice alone   │
                    └─────────┬─────────┘
                              │  Command
                              ▼
                    ┌─────────────────────┐
                    │  HybridCoordinator  │◄── ContinuousTrainer
                    │                     │    (hooks route())
                    │  Gate 1: confidence │
                    │  Gate 2: complexity │
                    │  Gate 3: VRAM       │
                    │  Gate 4: latency    │
                    └────┬──────────┬─────┘
                         │          │
               ┌─────────▼──┐  ┌───▼──────────────┐
               │ LocalInfer │  │  CloudInference   │
               │ Ollama LLM │  │  AWS Bedrock      │
               │ RTX 5090   │  │  (+ Transcribe,   │
               └─────────┬──┘  │   Lex, Polly)     │
                         │     └───────────┬────────┘
                         └────────┬────────┘
                                  │  action string
                                  ▼
                         ┌────────────────┐
                         │  DesktopAgent  │
                         │                │
                         │  ActionParser  │
                         │  ElementFinder │ ← accessibility tree
                         │  (UIA / ATSPI) │ ← EasyOCR fallback
                         │  pyautogui     │
                         └────────────────┘
```

## Component descriptions

### WhisperStream (`whisper_stream.py`)

Captures mic audio at 16 kHz, segments it using Silero VAD (CPU, 512-sample chunks),
and transcribes complete utterances with `faster-whisper` on CUDA. Exposes
`avg_logprob` from each segment as the confidence signal for Gate 1. Feeds
`Command(source="voice")` objects to the coordinator or FusionEngine.

Key classes: `MicCapture`, `SileroVAD`, `UtteranceSegmenter`, `WhisperTranscriber`, `WhisperStream`

### GestureStream (`gesture_stream.py`)

Reads camera frames, runs MediaPipe hand landmark detection and YOLOv8 pose estimation.
Classifies static postures (open palm, point, pinch, fist, thumb up, two-finger tap)
from landmark geometry and dynamic swipes from palm centroid trajectory.
Produces `Command(source="gesture")` objects.

Key classes: `StaticGestureClassifier`, `DynamicGestureDetector`, `LandmarkSmoother`, `GestureDebouncer`, `GestureStream`

### HybridCoordinator (`hybrid_coordinator.py`)

The central router. Evaluates four gates in order and routes to `LocalInference`
(Ollama) or `CloudInference` (Bedrock). Logs every outcome to `routing_log.jsonl`.
Exposes `update_thresholds()` for live tuning and `status()` for diagnostics.

Key classes: `HybridCoordinator`, `Thresholds`, `VRAMMonitor`, `LatencyTracker`, `OutcomeLogger`

### DesktopAgent (`desktop_agent.py`)

Parses action strings and executes them. `ElementFinder` walks the accessibility
tree (UI Automation on Windows, AT-SPI on Linux) to resolve target names to screen
coordinates. Falls back to EasyOCR on the RTX 5090 for canvas-rendered or
Electron apps. `DICTATE` uses clipboard paste instead of keystrokes for speed.

Key classes: `ActionParser`, `ElementFinder`, `DesktopAgent`

### ContinuousTrainer (`continuous_trainer.py`)

Hooks into `coordinator.route()` at startup and records every outcome in real time.
Runs three background loops: threshold adaptation (5 min), vocabulary rebuild (30 min),
and nightly compaction. `PromptAugmenter` patches `LocalInference.infer()` to prepend
relevant few-shot examples from `FewShotMemory` before each LLM call.

Key classes: `ThresholdTuner`, `VocabularyBuilder`, `GestureCalibrator`, `FewShotMemory`, `PromptAugmenter`, `ContinuousTrainer`

### SensorFusion / BudgetSensorFusion (`sensor_fusion.py`, `budget_sensor_fusion.py`)

Owns all sensor objects, starts them, runs the `FusionEngine` tick loop at 60 Hz,
and feeds `Command` objects to the coordinator. `FusionEngine.tick()` evaluates
the five priority rules on each tick and returns at most one `Command`.

Full stack uses: `ReSpeakerCapture`, `RealSenseCapture`, `UltraleapTracker`, `TobiiGazeTracker`
Budget stack uses: `FIFINECapture`, `OAKDLiteCapture`, `LeapMotionV1Tracker`, `IrisGazeEstimator`

### IPadBridge (`ipad_bridge.py`)

Streams iPad Pro LiDAR depth via the Record3D iOS app and `record3d` Python library.
Reads gaze from Eyeware Beam (TrueDepth on Face ID iPads). Uses the iPad as a USB
webcam for gesture/iris detection. Provides `IPadSensorFusion` as a drop-in
replacement for `BudgetSensorFusion`.

Key classes: `BeamGazeTracker`, `IPadLiDARCapture`, `IPadWebcam`, `IPadSensorFusion`

### TouchInputServer (`ipad_touch.py`)

Runs an `aiohttp` WebSocket + HTTP server. Serves a single-file Safari web app
to the iPad with four panels: command pad (large tap targets), virtual trackpad,
edge swipe strips, and settings (dwell timeout, trackpad speed, palm rejection radius).
Touch events arrive as JSON and are converted to `TouchEvent` objects. The
`TouchInputReceiver` dispatches trackpad moves directly to `pyautogui` (bypassing
the coordinator) and all other events as `Command(source="touch")` objects.

Key classes: `TouchInputServer`, `TouchInputReceiver`

## Data flows

### Voice command (local path)
```
Mic → SileroVAD → UtteranceSegmenter → WhisperTranscriber
  → Command(text, logprob, source="voice")
  → FusionEngine.on_voice()
  → FusionEngine.tick() → Command
  → HybridCoordinator.route() [gates 1-4 pass]
  → LocalInference.infer() [Ollama]
  → "CLICK submit"
  → DesktopAgent.execute()
  → ElementFinder → pyautogui.click(x, y)
```

### Gaze + voice click
```
Tobii/Beam → GazePoint(x=0.72, y=0.44, valid=True)
Whisper → "click"
FusionEngine: gaze stable + "click" keyword → Rule 1
  → Command(text="click here", _gaze_coords=(979, 396), source="multimodal")
  → HybridCoordinator [gate 0, direct local]
  → "CLICK here"
  → DesktopAgent: pyautogui.moveTo(979, 396); pyautogui.click()
```

### iPad touch command
```
iPad Safari → WebSocket JSON: {"type": "tap", "command": "scroll down"}
  → TouchInputReceiver → Command(text="scroll down", source="touch")
  → HybridCoordinator.route() [touch bypasses gates, direct local]
  → "SCROLL DOWN 3"
  → DesktopAgent: pyautogui.scroll(-3)
```

## Persistent files

| File                       | Written by            | Read by                    |
|----------------------------|-----------------------|----------------------------|
| `routing_log.jsonl`        | OutcomeLogger         | ThresholdTuner, VocabBuilder |
| `hotwords.txt`             | VocabularyBuilder     | WhisperTranscriber         |
| `gesture_calibration.json` | GestureCalibrator     | GestureCalibrator (startup) |
| `few_shot_memory.db`       | FewShotMemory         | PromptAugmenter            |

## Sensor fallback matrix

| Sensor absent       | Automatic fallback                          |
|---------------------|---------------------------------------------|
| ReSpeaker           | System default mic                          |
| RealSense D455      | OAK-D Lite or no depth                     |
| OAK-D Lite          | Webcam only (no depth)                      |
| Ultraleap v2        | Leap Motion v1 → MediaPipe 2D              |
| Tobii               | Eyeware Beam → MediaPipe iris → no gaze    |
| iPad LiDAR          | OAK-D Lite or no depth                     |
| iPad Beam           | MediaPipe iris gaze                         |
| Any AWS service     | Local inference only                        |
