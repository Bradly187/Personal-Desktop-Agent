# State Machine Diagrams — iPad-Focused Architecture

---

## 1. iPadApp — Top-Level Application State

```mermaid
stateDiagram-v2
    [*] --> LAUNCHING

    LAUNCHING --> SEARCHING : App did finish launching\nStart Bonjour/mDNS discovery

    SEARCHING --> CONNECTING : PC_Service found\nor manual IP entered

    CONNECTING --> CONNECTED : WebSocket handshake\ncomplete (101)

    CONNECTING --> SEARCHING : Timeout (10s)\nretry with backoff

    CONNECTED --> SENSOR_INIT : Receive config message\nfrom PC_Service

    SENSOR_INIT --> ACTIVE : All available sensors\nstarted successfully

    ACTIVE --> RECONNECTING : WebSocket drops

    RECONNECTING --> CONNECTING : Exponential backoff\n(1s, 2s, 4s, 8s…)

    ACTIVE --> ACTIVE : Normal operation\n(sensor streaming, touch events)

    ACTIVE --> [*] : App background / terminate\nstop all sensors

    note right of CONNECTED
        PC sends config:
        fusion priority,
        thresholds, port
    end note

    note right of RECONNECTING
        Visual indicator shown to user.
        Sensors keep running locally
        but output is buffered.
    end note
```

---

## 2. GazeTracker (ARKit) — Dwell State Machine

```mermaid
stateDiagram-v2
    [*] --> IDLE

    IDLE --> TRACKING : ARFaceAnchor detected\nand eye tracking valid

    TRACKING --> DWELL_STARTED : Gaze stable\n(spread < 4% of screen)\nfor ≥ 100ms

    TRACKING --> IDLE : Face anchor lost\nor confidence < 0.55

    DWELL_STARTED --> DWELL_PENDING : Timer started\n(configured duration, default 1s)

    DWELL_PENDING --> DWELL_PENDING : Gaze still stable\non same target region

    DWELL_PENDING --> TRACKING : Gaze moved to\nnew region (reset timer)

    DWELL_PENDING --> FIRED : Timer expires\ngaze still stable

    FIRED --> TRACKING : Send gaze_dwell event\nto WebSocket

    TRACKING --> IDLE : Face lost or\nconfidence drops

    note right of DWELL_PENDING
        SwiftUI shows ring animation
        countdown on iPad screen.
        User can cancel by moving gaze.
    end note

    note right of FIRED
        Sends:
        {"type":"gaze_dwell",
         "data":{"x":0.72,"y":0.44}}
        Reset dwell timer immediately.
    end note
```

---

## 3. TiltSensor (Core Motion) — Navigation State Machine

```mermaid
stateDiagram-v2
    [*] --> STOPPED

    STOPPED --> MONITORING : start() called\nCMMotionManager starts\nat 60Hz

    MONITORING --> IN_DEAD_ZONE : |rx| < dead_zone\nAND |ry| < dead_zone

    MONITORING --> TILTING : |rx| or |ry|\nexceeds dead_zone

    IN_DEAD_ZONE --> TILTING : Tilt exceeds dead_zone

    TILTING --> IN_DEAD_ZONE : Returns inside dead_zone\n(stop sending tilt messages)

    TILTING --> TILTING : Send tilt vector\nevery 16ms (60Hz)

    MONITORING --> TAP_DETECTED : Accelerometer detects\nsharp impulse (table tap)

    TAP_DETECTED --> MONITORING : Send tap event\nto WebSocket

    MONITORING --> STOPPED : stop() called

    note right of IN_DEAD_ZONE
        No messages sent to PC.
        Cursor stops moving.
        Prevents drift from
        slight tremor.
    end note

    note right of TAP_DETECTED
        Sends:
        {"type":"tilt_tap"}
        PC interprets as click
        at current cursor position.
    end note
```

---

## 4. KeywordListener (Speech Framework) — Recognition State Machine

```mermaid
stateDiagram-v2
    [*] --> STOPPED

    STOPPED --> LISTENING : start() called\nSFSpeechRecognizer ready\nAVAudioEngine running

    LISTENING --> RECOGNIZING : Speech detected\n(audio level above threshold)

    RECOGNIZING --> KEYWORD_MATCH : On-device model\nmatches a keyword\nwith conf ≥ threshold

    RECOGNIZING --> NO_MATCH : Recognition completes\nno keyword matched

    KEYWORD_MATCH --> LISTENING : Send keyword event\nto WebSocket\n{"type":"keyword",...}

    NO_MATCH --> STREAMING_AUDIO : Buffer accumulated audio\nstream to PC for Whisper

    STREAMING_AUDIO --> LISTENING : Audio sent\nvia WebSocket\n{"type":"audio_stream",...}

    LISTENING --> STOPPED : stop() called

    note right of KEYWORD_MATCH
        Fast path — no PC round-trip.
        Executes in < 50ms typically.
        Bypasses WhisperStream GPU.
    end note

    note right of STREAMING_AUDIO
        Sends PCM audio as base64.
        WhisperStream on PC processes
        with Whisper large-v3 on GPU.
    end note
```

---

## 5. SoundDetector (AVFoundation) — Detection State Machine

```mermaid
stateDiagram-v2
    [*] --> STOPPED

    STOPPED --> MONITORING : start() called\nAVAudioEngine tap installed

    MONITORING --> ANALYZING : Audio buffer received\n(every ~23ms at 44.1kHz)

    ANALYZING --> SOUND_DETECTED : Pattern matches\ncluck / pop / hiss\nwith conf ≥ threshold

    ANALYZING --> MONITORING : No match\nor below threshold

    SOUND_DETECTED --> COOLING_DOWN : Send sound_action event\nStart debounce timer\n(default 500ms)

    COOLING_DOWN --> COOLING_DOWN : Additional sound detected\n(suppressed — debounce active)

    COOLING_DOWN --> MONITORING : Debounce timer\nexpired

    MONITORING --> STOPPED : stop() called

    note right of SOUND_DETECTED
        Sends:
        {"type":"sound_action",
         "data":{"sound":"cluck",
                 "conf":0.87}}
    end note

    note right of COOLING_DOWN
        Prevents accidental double-fire
        from RA-related difficulty
        controlling sound duration.
    end note
```

---

## 6. FusionEngine (PC-side) — 10-Level Priority Evaluation

```mermaid
stateDiagram-v2
    [*] --> WAITING

    WAITING --> TICK : 60Hz timer fires

    state TICK {
        [*] --> CHECK_TOUCH
        CHECK_TOUCH --> EMIT_TOUCH : Touch command pending
        CHECK_TOUCH --> CHECK_SOUND : No touch command

        CHECK_SOUND --> EMIT_SOUND : Sound action pending
        CHECK_SOUND --> CHECK_GAZE_DWELL : No sound action

        CHECK_GAZE_DWELL --> EMIT_GAZE_DWELL : Gaze dwell fired
        CHECK_GAZE_DWELL --> CHECK_GAZE_VOICE : No dwell

        CHECK_GAZE_VOICE --> EMIT_GAZE_VOICE : Gaze stable AND voice="click"
        CHECK_GAZE_VOICE --> CHECK_GAZE_GESTURE : No gaze+voice

        CHECK_GAZE_GESTURE --> EMIT_GAZE_GESTURE : Gaze stable AND POINT gesture
        CHECK_GAZE_GESTURE --> CHECK_TILT : No gaze+gesture

        CHECK_TILT --> EMIT_TILT : Tilt vector active\n(outside dead zone)
        CHECK_TILT --> CHECK_HEAD : No tilt

        CHECK_HEAD --> EMIT_HEAD : Head pose delta active
        CHECK_HEAD --> CHECK_GESTURE : No head tracking

        CHECK_GESTURE --> EMIT_GESTURE : Gesture command pending
        CHECK_GESTURE --> CHECK_VOICE_LOCAL : No gesture

        CHECK_VOICE_LOCAL --> EMIT_VOICE_LOCAL : On-device keyword pending
        CHECK_VOICE_LOCAL --> CHECK_VOICE_PC : No local keyword

        CHECK_VOICE_PC --> EMIT_VOICE_PC : PC-transcribed voice pending
        CHECK_VOICE_PC --> EMIT_NONE : Nothing pending
    }

    TICK --> WAITING : Emit (or nothing)

    note right of EMIT_TILT
        Tilt/head emit directly to
        pyautogui — cursor movement,
        not a Command to the LLM.
    end note
```

---

## 7. AppMode — UI View State Machine (SwiftUI)

```mermaid
stateDiagram-v2
    [*] --> COMMAND_PAD

    COMMAND_PAD --> FULL_TRACKPAD : Edge swipe left\nor mode button tap

    FULL_TRACKPAD --> COMMAND_PAD : Edge swipe right\nor mode button tap

    COMMAND_PAD --> SETTINGS : Settings button tap

    FULL_TRACKPAD --> SETTINGS : Settings button tap\n(revealed by edge swipe up)

    SETTINGS --> COMMAND_PAD : Done / back

    state COMMAND_PAD {
        [*] --> NORMAL
        NORMAL --> DWELL_COUNTDOWN : Finger rests on button\n(dwell enabled)
        DWELL_COUNTDOWN --> NORMAL : Finger lifted\nbefore timer
        DWELL_COUNTDOWN --> TRIGGERED : Timer expires
        TRIGGERED --> NORMAL : Command sent
    }

    state FULL_TRACKPAD {
        [*] --> IDLE_PAD
        IDLE_PAD --> DRAGGING : Touch down
        DRAGGING --> IDLE_PAD : Touch up
        IDLE_PAD --> TAPPED : Single tap
        TAPPED --> IDLE_PAD : Left click sent
    }

    note right of FULL_TRACKPAD
        Full screen = mouse.
        Palm rejection active.
        iPad flat on desk use case.
    end note
```

---

## 8. WebSocketManager — Connection State Machine (Swift)

```mermaid
stateDiagram-v2
    [*] --> IDLE

    IDLE --> DISCOVERING : App enters foreground\nBonjour/mDNS scan starts

    DISCOVERING --> CONNECTING : PC found on network\nor manual IP set

    CONNECTING --> CONNECTED : 101 Switching Protocols\nhandshake complete

    CONNECTING --> BACKING_OFF : Timeout (10s)\nor refused

    BACKING_OFF --> CONNECTING : Backoff delay elapsed\n(1s → 2s → 4s → 8s → 30s max)

    CONNECTED --> SENDING : send(SensorMessage) called
    SENDING --> CONNECTED : Frame sent

    CONNECTED --> BACKING_OFF : Connection error\nor server closed

    CONNECTED --> IDLE : User disconnects manually

    IDLE --> [*] : App terminates

    note right of CONNECTED
        Status indicator:
        green = connected
        yellow = reconnecting
        red = disconnected
    end note
```
