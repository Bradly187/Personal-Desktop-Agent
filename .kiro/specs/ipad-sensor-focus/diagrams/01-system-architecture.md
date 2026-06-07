# System Architecture — iPad-Focused

---

## 1. Deployment Context

```mermaid
C4Context
    title iPad Accessibility Agent — Deployment

    Person(user, "User with RA", "Controls desktop via iPad\nmultimodal input")

    System_Boundary(ipad_boundary, "iPad Pro 2020+ (Swift/SwiftUI)") {
        System(ipad_app, "iPadApp", "Native SwiftUI app\nCaptures all sensors\nProvides touch UI")
    }

    System_Boundary(desktop, "Desktop PC — RTX 5090") {
        System(pc_service, "PC_Service", "Python asyncio\nInference + execution")
        SystemDb(storage, "Local Storage", "agent.db (SQLite 14 tables)\naudit.db (append-only)\nanalytics.duckdb\nchroma_db/ (vector store)")
        System(ollama, "Ollama Server", "llama3.1:8b default\n4.6 GB VRAM / ~190 ms warm wall p50 (0.30.6)")
    }

    System_Ext(aws, "AWS Cloud", "Fallback only\nBedrock / Transcribe / Polly")

    Rel(user, ipad_app, "Tilts, gazes, speaks,\nmakes sounds, touches")
    Rel(ipad_app, pc_service, "WebSocket :8765\nSensor data + commands")
    Rel(pc_service, ollama, "HTTP :11434\nChat completions")
    Rel(pc_service, storage, "Read/write state")
    Rel(pc_service, aws, "boto3 HTTPS\nFallback only")
    Rel(pc_service, user, "pyautogui\nMouse & keyboard\non desktop screen")
```

---

## 2. iPad ↔ PC Split

```mermaid
flowchart LR
    subgraph iPad["iPad Pro (Native Swift App)"]
        direction TB
        CM["Core Motion\n(Tilt vectors @ 60Hz)"]
        ARK["ARKit\n(Gaze delta + Head pose + LiDAR)"]
        SPE["Speech Framework\n(Keyword recognition)"]
        AVF["AVFoundation\n(Sound action detection)"]
        CAM["Camera Feed\n(JPEG frames @ 10fps)"]
        TOUCH["SwiftUI Touch\n(Command pad + Trackpad)"]
    end

    subgraph WS["WebSocket :8765 — 15 message types"]
        direction TB
        MSG["JSON: tilt, gaze_delta, head_pose,\ndepth_frame, camera_frame,\naudio_stream, touch_command, …"]
    end

    subgraph PC["Desktop PC (Python asyncio)"]
        direction TB
        BRIDGE["IPadBridge\n(Message router)"]
        FUSION["FusionEngine\n(10-level priority @ 60Hz)"]
        WHISPER["WhisperStream\n(Silero VAD + Whisper large-v3)"]
        TWIN["BehavioralTwinState\n(ChromaDB + AgentDB)"]
        COORD["HybridCoordinator\n(Gate 0 + Gates 1–4)"]
        AGENT["CommandExecutor\n(16 verbs → pyautogui)"]
        TRAINER["ContinuousTrainer\n(Threshold + few-shot adaptation)"]
    end

    CM --> MSG
    ARK --> MSG
    SPE --> MSG
    AVF --> MSG
    CAM --> MSG
    TOUCH --> MSG

    MSG --> BRIDGE
    BRIDGE --> FUSION
    BRIDGE --> WHISPER
    FUSION --> COORD
    TWIN --> COORD
    COORD --> AGENT
    COORD --> TRAINER
    TRAINER --> TWIN
```

---

## 3. WebSocket Protocol

```mermaid
sequenceDiagram
    participant iPad as iPadApp
    participant PC as PC_Service

    Note over iPad,PC: Connection Establishment
    iPad->>PC: WebSocket CONNECT ws://pc-ip:8765
    PC->>iPad: 101 Switching Protocols
    PC->>iPad: {"type":"config","data":{"fusion_priority":[...],"thresholds":{...}}}

    Note over iPad,PC: Sensor Streaming (continuous)
    iPad->>PC: {"type":"tilt","ts":1234,"data":{"rx":0.02,"ry":-0.01}}
    iPad->>PC: {"type":"gaze","ts":1235,"data":{"x":0.72,"y":0.44,"conf":0.91}}
    iPad->>PC: {"type":"head_pose","ts":1235,"data":{"pitch":2.1,"yaw":-1.3}}
    iPad->>PC: {"type":"depth_frame","ts":1236,"data":{"w":256,"h":192,"blob":"<base64>"}}

    Note over iPad,PC: Event Messages (on detection)
    iPad->>PC: {"type":"keyword","ts":1240,"data":{"word":"scroll down","conf":0.94}}
    iPad->>PC: {"type":"sound_action","ts":1241,"data":{"sound":"cluck","conf":0.87}}
    iPad->>PC: {"type":"touch_command","ts":1242,"data":{"command":"open chrome"}}
    iPad->>PC: {"type":"trackpad","ts":1243,"data":{"dx":12,"dy":-5}}
    iPad->>PC: {"type":"audio_stream","ts":1244,"data":{"samples":"<base64 PCM>"}}

    Note over iPad,PC: PC → iPad (feedback)
    PC->>iPad: {"type":"status","data":{"connected_sensors":[...],"active_mode":"trackpad"}}
    PC->>iPad: {"type":"executed","data":{"action":"SCROLL DOWN 3","success":true}}
```

---

## 4. Network Topology

```mermaid
flowchart LR
    subgraph ipad["iPad Pro"]
        app["iPadApp\n(Swift/SwiftUI)\nLiDARStreamer built-in"]
    end

    subgraph desktop["Desktop PC (192.168.18.2)"]
        service["PC_Service\nPort 8765 (WS)"]
        ollama_srv["Ollama\nPort 11434"]
        chroma["ChromaDB\n./chroma_db/"]
        service <--> ollama_srv
        service --> chroma
    end

    subgraph aws["AWS (fallback)"]
        bedrock["Bedrock\nClaude Haiku"]
        transcribe["Transcribe"]
        polly["Polly / Chatterbox"]
    end

    app <-->|"WebSocket :8765 (WiFi)\n15 message types including\ndepth_frame + camera_frame"| service
    service <-->|"boto3 HTTPS\n(Gate 2/3/4 fallback)"| bedrock
    service <-->|"boto3 HTTPS\n(Gate 1 low-confidence)"| transcribe
    service <-->|"HTTP :8766 sidecar"| polly
```

### Single-Connection Architecture

All sensor data — including LiDAR `depth_frame` and `camera_frame` — streams through a **single WebSocket connection** on port 8765. `LiDARStreamer.swift` uses `ARWorldTrackingConfiguration` + `.smoothedSceneDepth` and serialises depth frames directly in the iPadApp message protocol. Record3D is no longer required.
