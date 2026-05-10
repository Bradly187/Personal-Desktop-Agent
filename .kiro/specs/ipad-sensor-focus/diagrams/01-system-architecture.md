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
        System(pc_service, "PC_Service", "Python 3.11 asyncio\nInference + execution")
        SystemDb(storage, "Local Storage", "few_shot_memory.db\nrouting_log.jsonl\ngesture_calibration.json\nhotwords.txt")
        System(ollama, "Ollama Server", "Llama 3.1 70B\n~24 GB VRAM")
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
        ARK["ARKit\n(Gaze direction + Head pose)"]
        SPE["Speech Framework\n(Keyword recognition)"]
        AVF["AVFoundation\n(Sound action detection)"]
        CAM["Camera Feed\n(Frames for gesture)"]
        TOUCH["SwiftUI Touch\n(Command pad + Trackpad)"]
        R3D["Record3D\n(LiDAR depth frames)"]
    end

    subgraph WS["WebSocket :8765"]
        direction TB
        MSG["JSON messages\nper sensor type"]
    end

    subgraph PC["Desktop PC (Python)"]
        direction TB
        BRIDGE["IPadBridge\n(Receives all streams)"]
        FUSION["FusionEngine\n(10-level priority @ 60Hz)"]
        WHISPER["WhisperStream\n(GPU transcription)"]
        COORD["HybridCoordinator\n(4-gate routing)"]
        AGENT["DesktopAgent\n(pyautogui execution)"]
        TRAINER["ContinuousTrainer\n(Background learning)"]
    end

    CM --> MSG
    ARK --> MSG
    SPE --> MSG
    AVF --> MSG
    CAM --> MSG
    TOUCH --> MSG
    R3D --> MSG

    MSG --> BRIDGE
    BRIDGE --> FUSION
    BRIDGE --> WHISPER
    FUSION --> COORD
    COORD --> AGENT
    TRAINER --> COORD
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
        app["iPadApp\n(Swift/SwiftUI)"]
        r3d["Record3D\n(separate iOS app)"]
    end

    subgraph desktop["Desktop PC (192.168.x.x)"]
        service["PC_Service\nPort 8765 (WS)"]
        r3d_lib["LiDARReceiver\n(record3d Python lib)"]
        ollama_srv["Ollama\nPort 11434"]
        service <--> ollama_srv
        r3d_lib --> service
    end

    subgraph aws["AWS (fallback)"]
        bedrock["Bedrock"]
        transcribe["Transcribe"]
        polly["Polly"]
    end

    app <-->|"WebSocket :8765\n(WiFi or USB)\nAll sensor data + touch"| service
    r3d -->|"record3d lib\n(USB or WiFi)\nSeparate connection\nDepth frames only"| r3d_lib
    service <-->|"boto3 HTTPS\n(fallback only)"| bedrock
    service <-->|"boto3 HTTPS"| transcribe
    service <-->|"boto3 HTTPS"| polly
```

### Dual-Connection Note

The iPad runs **two independent connections** to the PC:

1. **iPadApp WebSocket** (port 8765) — carries all sensor data (tilt, gaze, head, keywords, sounds, touch, camera frames, audio)
2. **Record3D USB/WiFi stream** — carries LiDAR depth frames via the `record3d` Python library on a separate channel

This is intentional: Record3D is a third-party app with its own optimized streaming protocol. Routing depth frames through the iPadApp WebSocket would add unnecessary encoding overhead and latency. The `LiDARReceiver` on the PC merges depth data into the `GestureProcessor` pipeline alongside camera frames from the WebSocket.

If Record3D is not running, the system degrades gracefully — gesture recognition falls back to 2D MediaPipe classification without depth.
