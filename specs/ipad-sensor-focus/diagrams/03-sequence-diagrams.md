# Sequence Diagrams — iPad-Focused Flows

> Eye-gaze, head-pose, and mouth-sound control were **removed** (the standard
> iPad lacks the required TrueDepth sensor). The flows below reflect the current
> **6-level** FusionEngine priority. Source of truth: `core/fusion_engine.py`.

---

## 1. Touch Command (Rule 1 — highest priority, bypasses everything)

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant iPad as iPadApp (SwiftUI)
    participant WS as WebSocket
    participant Bridge as IPadBridge
    participant Fusion as FusionEngine
    participant Coord as HybridCoordinator
    participant Agent as DesktopAgent

    User->>iPad: taps "Scroll Down" button
    iPad->>WS: {"type":"touch_command","data":{"command":"scroll down"}}
    WS->>Bridge: on_message()
    Bridge->>Fusion: on_touch(Command(text="scroll down", source="touch"))
    Fusion->>Fusion: tick() — Rule 1: touch command pending
    Fusion->>Coord: route(Command)
    Note over Coord: Touch source → bypass all gates
    Coord->>Agent: execute("SCROLL DOWN 3", cmd)
    Agent->>User: page scrolls down
```

---

## 2. Full-Screen Trackpad (direct path — no LLM)

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant iPad as iPadApp (TrackpadView)
    participant WS as WebSocket
    participant Bridge as IPadBridge
    participant PyAG as pyautogui

    User->>iPad: drags finger across screen (Δx=30, Δy=-15)
    iPad->>iPad: palm rejection check (radius OK)
    iPad->>WS: {"type":"trackpad","data":{"dx":30,"dy":-15,"fingers":1,"gesture":"drag"}}
    WS->>Bridge: on_message()
    Bridge->>Bridge: _dispatch_trackpad — bypasses FusionEngine entirely
    Bridge->>PyAG: pyautogui.moveRel(30 * speed, -15 * speed)
    PyAG->>User: cursor moves proportionally

    User->>iPad: single tap
    iPad->>WS: {"type":"trackpad","data":{"dx":0,"dy":0,"fingers":1,"gesture":"tap"}}
    WS->>Bridge: on_message()
    Bridge->>PyAG: pyautogui.click()
    PyAG->>User: left click at cursor position
```

---

## 3. Voice "click" Keyword (Rule 2 — click at current cursor)

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant iPad as iPadApp (KeywordListener)
    participant WS as WebSocket
    participant Bridge as IPadBridge
    participant Fusion as FusionEngine
    participant Coord as HybridCoordinator
    participant Agent as DesktopAgent

    User->>iPad: positions cursor (tilt/trackpad), then says "click"
    iPad->>iPad: Speech Framework matches keyword "click" (conf=0.95)
    iPad->>WS: {"type":"keyword","data":{"word":"click","conf":0.95}}
    WS->>Bridge: on_message()
    Bridge->>Fusion: voice "click" keyword pending
    Fusion->>Fusion: tick() — Rule 2: voice "click" → click at current cursor
    Fusion->>Coord: Command(text="click", source="multimodal")
    Note over Coord: multimodal → bypass gates
    Coord->>Agent: execute("CLICK", cmd)
    Agent->>User: click at current cursor position
```

---

## 4. Tilt Navigation (Rule 3 — cursor movement)

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant iPad as iPadApp (TiltSensor)
    participant WS as WebSocket
    participant Bridge as IPadBridge
    participant Fusion as FusionEngine
    participant PyAG as pyautogui

    User->>iPad: tilts iPad slightly right and down
    loop Every 16ms (60Hz)
        iPad->>iPad: Core Motion → tilt vector / absolute position
        iPad->>iPad: dead zone check (|rx| > 0.01 ✓)
        iPad->>WS: {"type":"tilt_position","data":{"x":0.62,"y":0.55}}
        WS->>Bridge: on_message()
        Bridge->>Fusion: on_tilt(...)
        Fusion->>Fusion: tick() — Rule 3: tilt active, no higher priority
        Fusion->>Fusion: map tilt to cursor pos / delta (3a abs, 3b legacy velocity)
        Fusion->>PyAG: pyautogui.moveTo / moveRel
        PyAG->>User: cursor moves right and down
    end

    User->>iPad: levels iPad (returns to dead zone)
    iPad->>iPad: below dead zone
    Note over iPad: No tilt messages sent — cursor stops
```

---

## 5. Gesture via iPad Camera (Rule 4 — PC-side MediaPipe)

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant iPad as iPadApp (CameraStreamer)
    participant WS as WebSocket
    participant Bridge as IPadBridge
    participant Gesture as GestureProcessor
    participant Fusion as FusionEngine
    participant Coord as HybridCoordinator
    participant Agent as DesktopAgent

    User->>iPad: holds up hand showing POINT gesture
    iPad->>iPad: CameraStreamer captures frame (10fps, 320×240)
    iPad->>iPad: JPEG encode (quality 60, ~3ms)
    iPad->>WS: {"type":"camera_frame","data":{"frame":"<base64 JPEG>","width":320,"height":240}}
    WS->>Bridge: on_message()
    Bridge->>Bridge: JPEG decode (~2ms)
    Bridge->>Gesture: process_frame(decoded_frame, depth=depth_data)
    Gesture->>Gesture: MediaPipe hand landmarks
    Gesture->>Gesture: StaticGestureClassifier → ("POINT", 0.82)
    Gesture->>Gesture: GestureDebouncer.allow("POINT") → true
    Gesture->>Fusion: on_gesture(Command(text="POINT", gesture_confidence=0.82, source="gesture"))
    Fusion->>Fusion: tick() — Rule 4: gesture alone
    Fusion->>Coord: route(Command)
    Note over Coord: Gate 1: gesture_conf 0.82 ≥ min ✓
    Coord->>Agent: execute("CLICK", cmd)
    Agent->>User: click at current cursor
```

---

## 6. On-Device Voice Keyword (Rule 5 — fast path)

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant iPad as iPadApp (KeywordListener)
    participant WS as WebSocket
    participant Bridge as IPadBridge
    participant Fusion as FusionEngine
    participant Coord as HybridCoordinator
    participant Agent as DesktopAgent

    User->>iPad: says "scroll down"
    iPad->>iPad: Speech Framework matches "scroll down" (conf=0.92)
    iPad->>WS: {"type":"keyword","data":{"word":"scroll down","conf":0.92}}
    WS->>Bridge: on_message()
    Bridge->>Fusion: on_voice_local(Command(text="scroll down", source="voice_local"))
    Fusion->>Fusion: tick() — Rule 5: on-device keyword
    Fusion->>Coord: route(Command)
    Note over Coord: voice_local with known keyword → direct local
    Coord->>Agent: execute("SCROLL DOWN 3", cmd)
    Agent->>User: page scrolls down
```

---

## 7. Full Whisper Transcription (Rule 6 — complex command)

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant iPad as iPadApp (KeywordListener)
    participant WS as WebSocket
    participant Bridge as IPadBridge
    participant Whisper as WhisperStream
    participant Fusion as FusionEngine
    participant Coord as HybridCoordinator
    participant Bedrock as CloudInference
    participant Agent as DesktopAgent

    User->>iPad: says "open chrome and then go to gmail"
    iPad->>iPad: Speech Framework — no keyword match
    iPad->>WS: {"type":"audio_stream","data":{"samples":"<base64 PCM>"}}
    WS->>Bridge: on_message()
    Bridge->>Whisper: process audio
    Whisper->>Whisper: Silero VAD → utterance complete
    Whisper->>Whisper: faster-whisper GPU → "open chrome and then go to gmail" (logprob=-0.09)
    Whisper->>Fusion: on_voice(Command(text="open chrome and then go to gmail", source="voice"))
    Fusion->>Fusion: tick() — Rule 6: PC-transcribed voice
    Fusion->>Coord: route(Command)
    Note over Coord: Gate 1: logprob OK ✓
    Note over Coord: Gate 2: "and then" → complexity → ESCALATE
    Coord->>Bedrock: infer(Command)
    Bedrock->>Coord: "OPEN chrome\nHOTKEY ctrl+l\nDICTATE gmail.com\nHOTKEY Return"
    Coord->>Agent: execute multi-step
    Agent->>User: chrome opens, navigates to gmail
```

---

## 8. Startup and Connection

```mermaid
sequenceDiagram
    autonumber
    participant PC as PC_Service
    participant Bridge as IPadBridge
    participant Whisper as WhisperStream
    participant Gesture as GestureProcessor
    participant Fusion as FusionEngine
    participant Coord as HybridCoordinator
    participant Trainer as ContinuousTrainer
    participant iPad as iPadApp

    PC->>Bridge: start(port=8765)
    PC->>Whisper: start()
    PC->>Gesture: initialize()
    PC->>Fusion: start(tick_rate=60)
    PC->>Coord: initialize(thresholds)
    PC->>Trainer: start(coordinator)
    Trainer->>Trainer: load gesture_calibration.json
    Trainer->>Trainer: patch LocalInference with PromptAugmenter

    PC->>PC: print status table + QR code

    iPad->>iPad: launch app
    iPad->>iPad: discover PC via Bonjour/mDNS
    iPad->>Bridge: WebSocket CONNECT
    Bridge->>iPad: {"type":"config","data":{...}}
    iPad->>iPad: start all sensors
    iPad->>Bridge: sensor streams begin

    PC->>PC: print "iPad connected — all sensors active"
```
