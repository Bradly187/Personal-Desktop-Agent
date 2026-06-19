# Sequence Diagrams — iPad-Focused Flows

---

## 1. Touch Command (highest priority — bypasses everything)

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

## 3. Sound Action (cluck → click)

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant iPad as iPadApp (SoundDetector)
    participant WS as WebSocket
    participant Bridge as IPadBridge
    participant Fusion as FusionEngine
    participant Coord as HybridCoordinator
    participant Agent as DesktopAgent

    User->>iPad: makes "cluck" sound
    iPad->>iPad: AVFoundation detects cluck (conf=0.87)
    iPad->>iPad: debounce check (500ms cooldown OK)
    iPad->>WS: {"type":"sound_action","data":{"sound":"cluck","conf":0.87}}
    WS->>Bridge: on_message()
    Bridge->>Fusion: on_sound_action(Command(text="click", source="sound_action"))
    Fusion->>Fusion: tick() — Rule 2: sound action pending
    Fusion->>Coord: route(Command)
    Note over Coord: Sound action → bypass gates, direct local
    Coord->>Agent: execute("CLICK", cmd)
    Agent->>User: click at current cursor position
```

---

## 4. Gaze Dwell Click (look at target for 1 second)

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant iPad as iPadApp (GazeTracker)
    participant WS as WebSocket
    participant Bridge as IPadBridge
    participant Fusion as FusionEngine
    participant Coord as HybridCoordinator
    participant Agent as DesktopAgent

    User->>iPad: looks at "Submit" button area
    loop Every frame (~60Hz)
        iPad->>WS: {"type":"gaze","data":{"x":0.72,"y":0.44,"conf":0.91}}
        WS->>Bridge: on_message()
        Bridge->>Fusion: update gaze point
    end

    iPad->>iPad: dwell timer reaches 1.0s (gaze stable)
    iPad->>WS: {"type":"gaze_dwell","data":{"x":0.72,"y":0.44}}
    WS->>Bridge: on_message()
    Bridge->>Fusion: on_gaze_dwell(Command(_gaze_coords=(979,396), source="gaze_dwell"))
    Fusion->>Fusion: tick() — Rule 3: gaze dwell pending
    Fusion->>Coord: route(Command)
    Coord->>Agent: execute("CLICK", cmd) with _gaze_coords
    Agent->>Agent: pyautogui.moveTo(979, 396)
    Agent->>Agent: pyautogui.click()
    Agent->>User: cursor moves and clicks Submit
```

---

## 5. Gaze + Voice Click

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant iPad as iPadApp
    participant WS as WebSocket
    participant Bridge as IPadBridge
    participant Fusion as FusionEngine
    participant Coord as HybridCoordinator
    participant Agent as DesktopAgent

    User->>iPad: looks at target (ARKit gaze)
    iPad->>WS: {"type":"gaze","data":{"x":0.65,"y":0.30,"conf":0.88}}
    Bridge->>Fusion: update gaze (stable, spread < 4%)

    User->>iPad: says "click"
    iPad->>iPad: Speech Framework matches keyword "click"
    iPad->>WS: {"type":"keyword","data":{"word":"click","conf":0.95}}
    WS->>Bridge: on_message()
    Bridge->>Fusion: on_voice_local(Command(text="click", source="voice_local"))

    Fusion->>Fusion: tick() — Rule 4: gaze stable + "click" keyword
    Fusion->>Fusion: resolve pixels (0.65*W, 0.30*H) = (884, 270)
    Fusion->>Coord: Command(text="click", _gaze_coords=(884,270), source="multimodal")
    Note over Coord: Multimodal → bypass gates
    Coord->>Agent: execute("CLICK", cmd) with _gaze_coords
    Agent->>Agent: pyautogui.moveTo(884, 270); pyautogui.click()
    Agent->>User: clicks at gaze target
```

---

## 6. Tilt Navigation (cursor movement)

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
        iPad->>iPad: Core Motion rotationRate (rx=0.03, ry=0.02)
        iPad->>iPad: dead zone check (|rx| > 0.01 ✓)
        iPad->>WS: {"type":"tilt","data":{"rx":0.03,"ry":0.02}}
        WS->>Bridge: on_message()
        Bridge->>Fusion: on_tilt(TiltVector(rx=0.03, ry=0.02))
        Fusion->>Fusion: tick() — Rule 6: tilt active, no higher priority
        Fusion->>Fusion: map tilt to cursor delta (dx=6, dy=4)
        Fusion->>PyAG: pyautogui.moveRel(6, 4)
        PyAG->>User: cursor moves right and down
    end

    User->>iPad: levels iPad (returns to dead zone)
    iPad->>iPad: rotationRate below dead zone
    Note over iPad: No tilt messages sent — cursor stops
```

---

## 7. Head Tracking (coarse cursor)

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant iPad as iPadApp (HeadTracker)
    participant WS as WebSocket
    participant Bridge as IPadBridge
    participant Fusion as FusionEngine
    participant PyAG as pyautogui

    User->>iPad: tilts head slightly left
    iPad->>iPad: ARKit face anchor → yaw delta = -2.1°
    iPad->>WS: {"type":"head_pose","data":{"pitch":0.0,"yaw":-2.1}}
    WS->>Bridge: on_message()
    Bridge->>Fusion: on_head(HeadPose(pitch=0.0, yaw=-2.1))
    Fusion->>Fusion: tick() — Rule 7: head tracking, no higher priority
    Fusion->>Fusion: map head yaw to cursor dx (smoothed)
    Fusion->>PyAG: pyautogui.moveRel(-8, 0)
    PyAG->>User: cursor moves left
```

---

## 8. Gesture via iPad Camera (PC-side MediaPipe)

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
    Bridge->>Gesture: process_frame(decoded_frame, depth=lidar_data)
    Gesture->>Gesture: MediaPipe hand landmarks
    Gesture->>Gesture: StaticGestureClassifier → ("POINT", 0.82)
    Gesture->>Gesture: GestureDebouncer.allow("POINT") → true
    Gesture->>Fusion: on_gesture(Command(text="POINT", gesture_confidence=0.82, source="gesture"))
    Fusion->>Fusion: tick() — Rule 8: gesture alone (no gaze active)
    Fusion->>Coord: route(Command)
    Note over Coord: Gate 1: gesture_conf 0.82 ≥ min ✓
    Coord->>Agent: execute("CLICK", cmd)
    Agent->>User: click at current cursor
```

---

## 9. On-Device Voice Keyword (fast path)

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
    Fusion->>Fusion: tick() — Rule 9: on-device keyword
    Fusion->>Coord: route(Command)
    Note over Coord: voice_local with known keyword → direct local
    Coord->>Agent: execute("SCROLL DOWN 3", cmd)
    Agent->>User: page scrolls down
```

---

## 10. Full Whisper Transcription (complex command)

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
    Fusion->>Fusion: tick() — Rule 10: PC-transcribed voice
    Fusion->>Coord: route(Command)
    Note over Coord: Gate 1: logprob OK ✓
    Note over Coord: Gate 2: "and then" → complexity → ESCALATE
    Coord->>Bedrock: infer(Command)
    Bedrock->>Coord: "OPEN chrome\nHOTKEY ctrl+l\nDICTATE gmail.com\nHOTKEY Return"
    Coord->>Agent: execute multi-step
    Agent->>User: chrome opens, navigates to gmail
```

---

## 11. Startup and Connection

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
