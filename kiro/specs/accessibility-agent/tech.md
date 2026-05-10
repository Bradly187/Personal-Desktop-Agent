# Tech stack

## Language and runtime

Python 3.11+ with asyncio throughout. All pipelines are async tasks running
inside a single event loop. Blocking operations (GPU inference, camera I/O,
file reads) are always dispatched via `asyncio.to_thread`.

## Core dependencies

```
faster-whisper       # Whisper large-v3 on CUDA, CTranslate2 backend
ultralytics          # YOLOv8 pose estimation
mediapipe            # Hand landmarks, face mesh iris tracking
opencv-python        # Camera capture, frame processing
sounddevice          # Audio capture (mic stream)
torch                # CUDA tensor ops, Silero VAD
pynvml               # GPU VRAM monitoring
boto3                # AWS SDK (Bedrock, Transcribe, Lex, Lambda fallbacks)
ollama               # Local LLM inference (Llama 3.1 70B)
aiosqlite            # Async SQLite for few-shot memory
aiohttp              # iPad touch WebSocket server
pyautogui            # Mouse / keyboard execution
psutil               # Process management for app launching
```

## Optional hardware SDKs

```
pyrealsense2         # Intel RealSense D455
depthai              # OAK-D Lite (Luxonis DepthAI)
leapc-cffi           # Ultraleap Controller 2
Leap                 # Leap Motion Controller v1 (leap-sdk-py3)
tobii-research       # Tobii Eye Tracker 5
eyeware-beam         # Eyeware Beam gaze tracker
record3d             # iPad Pro LiDAR via Record3D iOS app
```

## AWS services used

| Service            | Role                                        | When used         |
|--------------------|---------------------------------------------|-------------------|
| Amazon Bedrock     | Claude for complex / ambiguous commands     | Cloud fallback    |
| Amazon Transcribe  | Speech-to-text when Whisper confidence low  | Gate 1 fallback   |
| Amazon Lex         | Structured intent recognition               | Cloud fallback    |
| AWS Lambda         | Orchestration, remote access, logging       | Always available  |
| Amazon Polly       | TTS when local Kokoro/Coqui unavailable     | Cloud fallback    |
| Amazon Rekognition | Custom gesture model training               | Offline training  |
| Amazon S3          | Model weight storage and versioning         | Model updates     |

## File structure

```
project/
├── hybrid_coordinator.py     # Routing engine — 4-gate decision logic
├── whisper_stream.py         # Mic → Silero VAD → Whisper → Command
├── gesture_stream.py         # Camera → YOLOv8/MediaPipe → Command
├── desktop_agent.py          # Command string → mouse/keyboard execution
├── continuous_trainer.py     # Usage log analysis, threshold/vocab adaptation
├── sensor_fusion.py          # Full sensor stack (ReSpeaker/D455/Ultraleap/Tobii)
├── budget_sensor_fusion.py   # Budget stack (FIFINE/OAK-D/Leap v1/iris gaze)
├── ipad_bridge.py            # iPad LiDAR (record3d) + Beam gaze + webcam
├── ipad_touch.py             # iPad Safari touch UI WebSocket server
└── .kiro/
    ├── steering/
    │   ├── product.md
    │   ├── tech.md
    │   └── structure.md
    └── specs/
        └── accessibility-agent/
            ├── requirements.md
            ├── design.md
            └── tasks.md
```

## Inference hardware targets

| Model              | Hardware   | Latency target | VRAM     |
|--------------------|------------|----------------|----------|
| Whisper large-v3   | RTX 5090   | < 400 ms       | ~3 GB    |
| YOLOv8-pose        | RTX 5090   | < 15 ms/frame  | ~0.5 GB  |
| MediaPipe hands    | CPU        | < 5 ms/frame   | 0        |
| Ollama Llama 3.1   | RTX 5090   | < 600 ms       | ~24 GB   |
| EasyOCR (fallback) | RTX 5090   | < 200 ms       | ~1 GB    |
| Silero VAD         | CPU        | < 1 ms/chunk   | 0        |

Total budget VRAM: ~28.5 GB of 32 GB. 3.5 GB headroom for OS and browser.

## Coding conventions

- All public async methods named `run()` for pipeline entry points, `start()`/`stop()` for lifecycle
- `Command` dataclass is the universal data transfer object between all pipelines and the coordinator
- Every sensor class degrades gracefully — `ImportError` and connection failures log a warning and
  allow the rest of the system to continue
- No global state outside dataclass instances — all state lives in class attributes
- Log levels: DEBUG for per-frame data, INFO for commands and routing decisions, WARNING for
  sensor failures, ERROR for unrecoverable issues
