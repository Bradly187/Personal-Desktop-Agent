# Data Flow Diagrams — iPad-Focused Architecture

---

## 1. iPad Sensor Data Flows (end-to-end)

```mermaid
flowchart TD
    subgraph iPad_HW["iPad Pro Hardware"]
        accel["Accelerometer\n(tilt, table tap)"]
        gyro["Gyroscope\n(rotation rate)"]
        truedepth["TrueDepth Camera\n(face, eyes)"]
        lidar_hw["LiDAR Scanner"]
        mic_hw["Microphone"]
        front_cam_hw["Front/Back Camera"]
        touch_hw["Multi-touch Display"]
    end

    subgraph Swift_Frameworks["Swift Frameworks (on-device)"]
        cm["Core Motion\nCMMotionManager"]
        arkit["ARKit\nARSession + ARFaceAnchor"]
        speech["Speech Framework\nSFSpeechRecognizer"]
        avfound["AVFoundation\nAVAudioEngine"]
        uikit["SwiftUI / UIKit\nTouch events"]
        record3d_app["Record3D App\n(separate process)"]
    end

    subgraph Swift_Classes["iPadApp Swift Classes"]
        tilt_sensor["TiltSensor"]
        gaze_tracker["GazeTracker"]
        head_tracker["HeadTracker"]
        kw_listener["KeywordListener"]
        sound_det["SoundDetector"]
        cam_stream["CameraStreamer"]
        touch_ui["CommandPadView\nTrackpadView"]
        ws_mgr["WebSocketManager"]
    end

    subgraph WS["WebSocket :8765 (JSON)"]
        ws_wire["JSON messages\n{'type':..., 'ts':..., 'data':{...}}"]
    end

    subgraph PC_Bridge["PC — IPadBridge (Python)"]
        dispatch["Message dispatcher\n_dispatch_*(data)"]
    end

    subgraph PC_Processing["PC — Processing"]
        fusion["FusionEngine\n10-level priority @ 60Hz"]
        whisper_str["WhisperStream\nSileroVAD + faster-whisper"]
        gesture_proc["GestureProcessor\nMediaPipe + YOLOv8"]
        lidar_recv["LiDARReceiver\nRecord3D depth frames"]
        pyag["pyautogui\n(tilt/head direct path)"]
    end

    subgraph PC_Intelligence["PC — Intelligence"]
        coord["HybridCoordinator\n4-gate routing"]
        local_llm["LocalInference ABC\n(OllamaInference default\nVLLMInference Phase 2)"]
        cloud_llm["CloudInference\nAWS Bedrock"]
    end

    subgraph PC_Execution["PC — Execution"]
        agent["DesktopAgent\nActionParser + ElementFinder"]
        desktop["Windows Desktop\npyautogui + accessibility tree"]
    end

    subgraph PC_Learning["PC — Learning"]
        trainer["ContinuousTrainer"]
        fsm["few_shot_memory.db"]
        rlog["routing_log.jsonl"]
        gcal["gesture_calibration.json"]
        hot["hotwords.txt"]
    end

    %% Hardware → Frameworks
    accel --> cm
    gyro --> cm
    truedepth --> arkit
    mic_hw --> speech
    mic_hw --> avfound
    front_cam_hw --> cam_stream
    touch_hw --> uikit
    lidar_hw --> record3d_app

    %% Frameworks → Swift Classes
    cm --> tilt_sensor
    arkit --> gaze_tracker
    arkit --> head_tracker
    speech --> kw_listener
    avfound --> sound_det
    uikit --> touch_ui

    %% Swift Classes → WebSocketManager
    tilt_sensor -->|"TiltVector\n{rx, ry}"| ws_mgr
    gaze_tracker -->|"GazePoint\n{x, y, conf}"| ws_mgr
    gaze_tracker -->|"gaze_dwell event"| ws_mgr
    head_tracker -->|"HeadPose\n{pitch, yaw}"| ws_mgr
    kw_listener -->|"keyword match\nor PCM audio"| ws_mgr
    sound_det -->|"sound_action\n{sound, conf}"| ws_mgr
    cam_stream -->|"JPEG frames"| ws_mgr
    touch_ui -->|"touch_command\nor trackpad deltas"| ws_mgr
    record3d_app -->|"record3d lib\nRGBD frames"| lidar_recv

    %% WebSocket wire
    ws_mgr --> ws_wire
    ws_wire --> dispatch

    %% Bridge dispatch → PC processing
    dispatch -->|"tilt/head"| fusion
    dispatch -->|"gaze, gaze_dwell\nsound, keyword\ntouch, gesture cmd"| fusion
    dispatch -->|"PCM audio"| whisper_str
    dispatch -->|"camera frames"| gesture_proc
    lidar_recv -->|"depth ndarray"| gesture_proc

    %% Processing → Fusion
    whisper_str -->|"Command(source=voice)"| fusion
    gesture_proc -->|"Command(source=gesture)"| fusion

    %% Fusion → Coordinator or Direct
    fusion -->|"tilt/head cursor deltas"| pyag
    fusion -->|"Command"| coord

    %% Coordinator → Inference
    coord -->|"local path"| local_llm
    coord -->|"cloud fallback"| cloud_llm
    local_llm -->|"action string"| agent
    cloud_llm -->|"action string"| agent

    %% Agent → Desktop
    agent --> desktop

    %% Learning feedback
    coord -->|"every outcome"| rlog
    coord -->|"success pairs"| fsm
    trainer --> coord
    rlog --> trainer
    fsm --> local_llm
    trainer -->|"adapted floors"| gcal
    trainer -->|"new hotwords"| hot
    hot --> whisper_str
```

---

## 2. WebSocket Message Schema

```mermaid
erDiagram
    WEBSOCKET_MESSAGE {
        STRING type     "Sensor or event type (see table)"
        INT    ts       "Unix timestamp in milliseconds"
        OBJECT data     "Type-specific payload"
    }

    TILT_DATA {
        FLOAT  rx       "Rotation rate around X axis (rad/s)"
        FLOAT  ry       "Rotation rate around Y axis (rad/s)"
    }

    GAZE_DATA {
        FLOAT  x        "Normalized screen X [0.0, 1.0]"
        FLOAT  y        "Normalized screen Y [0.0, 1.0]"
        FLOAT  conf     "ARKit confidence [0.0, 1.0]"
    }

    GAZE_DWELL_DATA {
        FLOAT  x        "Gaze X at dwell trigger"
        FLOAT  y        "Gaze Y at dwell trigger"
    }

    HEAD_POSE_DATA {
        FLOAT  pitch    "Head pitch in degrees"
        FLOAT  yaw      "Head yaw in degrees"
    }

    KEYWORD_DATA {
        STRING word     "Matched keyword text"
        FLOAT  conf     "Speech recognition confidence"
    }

    SOUND_ACTION_DATA {
        STRING sound    "Detected sound (cluck|pop|hiss)"
        FLOAT  conf     "Detection confidence"
    }

    TOUCH_COMMAND_DATA {
        STRING command  "Command text (e.g. 'scroll down')"
    }

    TRACKPAD_DATA {
        FLOAT  dx       "X delta in screen points (0 for taps)"
        FLOAT  dy       "Y delta in screen points (0 for taps)"
        INT    fingers  "Number of touch contacts (1 or 2)"
        STRING gesture  "drag | tap | scroll (distinguishes intent)"
    }

    AUDIO_STREAM_DATA {
        STRING samples  "Base64-encoded 16kHz PCM"
        INT    frames   "Number of samples"
    }

    CAMERA_FRAME_DATA {
        STRING frame    "Base64-encoded JPEG (quality 60, ~30-50KB per frame)"
        INT    width    "Frame width (320px — downscaled for streaming)"
        INT    height   "Frame height (240px — downscaled for streaming)"
    }

    DEPTH_FRAME_DATA {
        INT    w        "Frame width in pixels"
        INT    h        "Frame height in pixels"
        STRING blob     "Base64-encoded float32 depth (mm)"
        STRING conf_map "Base64-encoded uint8 confidence"
    }

    WEBSOCKET_MESSAGE ||--o| TILT_DATA         : "type=tilt"
    WEBSOCKET_MESSAGE ||--o| GAZE_DATA         : "type=gaze"
    WEBSOCKET_MESSAGE ||--o| GAZE_DWELL_DATA   : "type=gaze_dwell"
    WEBSOCKET_MESSAGE ||--o| HEAD_POSE_DATA    : "type=head_pose"
    WEBSOCKET_MESSAGE ||--o| KEYWORD_DATA      : "type=keyword"
    WEBSOCKET_MESSAGE ||--o| SOUND_ACTION_DATA : "type=sound_action"
    WEBSOCKET_MESSAGE ||--o| TOUCH_COMMAND_DATA: "type=touch_command"
    WEBSOCKET_MESSAGE ||--o| TRACKPAD_DATA     : "type=trackpad"
    WEBSOCKET_MESSAGE ||--o| AUDIO_STREAM_DATA : "type=audio_stream"
    WEBSOCKET_MESSAGE ||--o| CAMERA_FRAME_DATA : "type=camera_frame"
    WEBSOCKET_MESSAGE ||--o| DEPTH_FRAME_DATA  : "type=depth_frame"
```

### Camera Streaming Notes

- **Throttled to 10 fps** by default (configurable in iPadApp settings). MediaPipe hand landmarks work well at low frame rates.
- **Downscaled to 320×240** on-device before JPEG encoding. Full resolution is unnecessary for landmark detection.
- **JPEG quality 60** keeps each frame under 50 KB → ~500 KB/s bandwidth at 10 fps.
- **Encoding latency**: ~3 ms on iPad (hardware JPEG encoder), ~2 ms decode on PC.
- If gesture recognition is not needed (e.g., user relies on tilt + gaze + voice), camera streaming can be disabled entirely to save bandwidth.

---

## 3. Command Dataclass — Source Tags and Confidence Semantics

```mermaid
flowchart LR
    subgraph Sources["Command.source values"]
        touch["touch\n(CommandPadView tap)"]
        sound["sound_action\n(AVFoundation cluck/pop/hiss)"]
        gaze_dwell["gaze_dwell\n(ARKit stable gaze ≥ dwell_timeout)"]
        multimodal["multimodal\n(gaze + voice_local or gesture)"]
        tilt["tilt\n(Core Motion tilt — direct to pyautogui)"]
        head_track["head_track\n(ARKit head pose — direct to pyautogui)"]
        gesture["gesture\n(MediaPipe on PC from camera feed)"]
        voice_local["voice_local\n(Speech Framework keyword)"]
        voice["voice\n(Whisper large-v3 on GPU)"]
    end

    subgraph Confidence["Confidence field semantics"]
        wlp["whisper_logprob:\n0.0 for non-voice sources\nactual logprob for voice"]
        gc["gesture_confidence:\n1.0 for non-gesture sources\nMediaPipe score for gesture"]
    end

    subgraph GateBehavior["Gate 1 behavior per source"]
        bypass["touch, sound_action,\ngaze_dwell, multimodal,\ntilt, head_track\n→ BYPASS all gates"]
        check_kw["voice_local\n→ skip Gate 1\n(already high-conf on-device)"]
        full_check["gesture, voice\n→ full 4-gate check"]
    end

    touch --> bypass
    sound --> bypass
    gaze_dwell --> bypass
    multimodal --> bypass
    tilt --> bypass
    head_track --> bypass
    voice_local --> check_kw
    gesture --> full_check
    voice --> full_check
```

---

## 4. Persistent Storage Schema

> **Updated 2026-05-11:** The project now uses two databases (`agent.db` + `analytics.duckdb`) instead of JSONL and JSON files.
> Full ER diagrams and design rationale: [14-database-schema.md](14-database-schema.md) and `docs/database-design.md`.

```mermaid
flowchart LR
    subgraph agentdb["agent.db (SQLite — 11 tables)"]
        direction TB
        sessions
        commands
        inferences
        few_shot_examples
        gesture_samples
        gesture_calibration
        sensor_events
        agent_runs
        agent_steps
        word_counts
        hotwords
        settings_versions
    end

    subgraph analyticsdb["analytics.duckdb (DuckDB)"]
        direction TB
        benchmark_runs
        benchmark_results
        benchmark_prompts
        note["Attaches agent.db as 'ops'\nfor cross-database OLAP"]
    end

    sessions --> commands --> inferences
    commands --> few_shot_examples
    commands --> gesture_samples
    commands --> sensor_events
    commands --> agent_runs --> agent_steps
    word_counts -.->|"promotes when count≥3"| hotwords
    gesture_samples -.->|"p10 every 5 min"| gesture_calibration
    benchmark_runs --> benchmark_results --> benchmark_prompts
```

---

## 5. Continuous Learning Data Cycle

```mermaid
flowchart TD
    EXE([Command executed\noutcome = success])
    EXE --> CMD["agent.db: commands\n(HybridCoordinator.route)"]
    EXE --> FSE["agent.db: few_shot_examples\n(ContinuousTrainer.record_success)"]
    EXE --> GS["agent.db: gesture_samples\n(when source=gesture)"]

    CMD --> ADAPT["ContinuousTrainer\nadaptation loop every 5 min\n(queries commands table)"]

    ADAPT -->|"cloud_rate > 30%\nlocal_fail < 10%\n→ relax logprob_min -0.05"| COORD["HybridCoordinator\nCoordinatorConfig"]

    ADAPT -->|"word count ≥ 3\n→ INSERT OR IGNORE"| HW["agent.db: hotwords"]
    HW -->|reload| WHISPER["WhisperStream"]

    GS --> GCAL["agent.db: gesture_calibration\n(p10 − 0.05, append-only)"]
    GCAL -->|"SELECT MAX(ts) per gesture"| GFLOOR["GestureProcessor\nconfidence floor"]

    FSE -->|"SELECT domain= ORDER BY ts DESC LIMIT 1000\nrank by Jaccard × recency × log(usage)"| PA["few-shot examples\n(top-5 for prompt)"]
    PA -->|"prepend before each LLM call"| LOCAL["LocalInference\nOllama"]
```

---

## 6. VRAM Budget — RTX 5090 (31.8 GB measured 2026-05-08)

> **Measured values** — captured via `python main.py --measure-vram` on the target machine.
> All prior estimates were extrapolated from Ada Lovelace; these are Blackwell actuals.

| Component | Estimated | **Measured** | Delta |
|-----------|-----------|-------------|-------|
| OS + drivers + desktop (baseline) | 3.5 GB | **8.3 GB** | +4.8 GB |
| Whisper large-v3 (float16) | 3.0 GB | **4.2 GB** | +1.2 GB |
| LLM (30B-class model via Ollama) | n/a | **~19 GB** | — |
| YOLOv8-pose | 0.5 GB | *not measured* | — |
| EasyOCR (on-demand) | 1.0 GB | *not measured* | — |

**Key finding:** Whisper + a 30B LLM + baseline fills all 31.8 GB.
`llama3.1:70b` (~40 GB weights) **cannot co-reside with Whisper** on this GPU.

**Revised model strategy:**
- Use `nemotron-mini` (4B, ~4 GB) or `llama3.1:8b` (4.9 GB) alongside Whisper
- Total usable VRAM after baseline + Whisper: **~19 GB** (enough for a 14–18B model)
- For 70B inference: unload Whisper first, or use RAM-offload via llama.cpp

```mermaid
pie title RTX 5090 VRAM Allocation (Measured 2026-05-08)
    "OS + drivers + desktop" : 8.3
    "Whisper large-v3" : 4.2
    "LLM headroom (19 GB available)" : 19.0
    "Unmeasured (YOLOv8, EasyOCR, reserve)" : 0.3
```

---

## 7. Latency Budget per Modality

```mermaid
gantt
    title End-to-End Latency (ms) — iPad-Focused
    dateFormat  X
    axisFormat  %L ms

    section Touch Command
    WS recv + route + pyautogui  :0, 20

    section Sound Action
    AVFoundation detect          :0, 30
    WS + route + execute         :30, 60

    section Gaze Dwell
    ARKit continuous gaze        :0, 1000
    Dwell fires + execute        :1000, 1020

    section Gaze + Voice (keyword)
    ARKit gaze streaming         :0, 5
    Speech Framework match       :5, 55
    WS + FusionEngine tick       :55, 72
    pyautogui click              :72, 85

    section Gesture (camera frame)
    iPad JPEG encode (320x240)   :0, 3
    WS transmit (~50KB)          :3, 8
    PC JPEG decode               :8, 10
    MediaPipe landmarks          :10, 15
    Classify + debounce          :15, 20
    FusionEngine + route         :20, 35

    section On-Device Keyword → Local LLM
    Speech Framework match       :0, 50
    WS + gate evaluation         :50, 60
    Ollama inference             :60, 660
    ElementFinder + execute      :660, 710

    section PC Whisper → Cloud LLM
    Audio stream over WS         :0, 50
    Silero VAD + Whisper GPU     :50, 450
    Gate eval + Bedrock          :450, 2450
    Execute                      :2450, 2500
```
