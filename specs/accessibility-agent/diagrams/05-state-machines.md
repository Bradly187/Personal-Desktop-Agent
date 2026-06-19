# State Machine Diagrams

---

## 1. UtteranceSegmenter — Voice Activity State Machine

```mermaid
stateDiagram-v2
    [*] --> IDLE

    IDLE --> CAPTURING : VAD detects speech\non_chunk(is_speech=True)

    CAPTURING --> CAPTURING : VAD detects speech\nbuffer.append(chunk)

    CAPTURING --> SILENCE_WAIT : VAD detects silence\n(first silent chunk)

    SILENCE_WAIT --> CAPTURING : VAD detects speech\n(utterance continues)

    SILENCE_WAIT --> EMIT : silence_frames ≥ threshold\n(utterance complete)

    EMIT --> IDLE : yield audio_buffer\nbuffer.clear()

    note right of IDLE
        No audio buffered.
        Waiting for speech onset.
    end note

    note right of CAPTURING
        Buffering audio chunks.
        Max buffer: 30 seconds.
    end note

    note right of SILENCE_WAIT
        Post-utterance silence window.
        Default: 400ms (8 frames @ 50ms)
    end note

    note right of EMIT
        Passes utterance to WhisperTranscriber.
        Async task dispatched via asyncio.to_thread.
    end note
```

---

## 2. HybridCoordinator — Routing Gate State Machine

```mermaid
stateDiagram-v2
    [*] --> RECEIVE_COMMAND

    RECEIVE_COMMAND --> TOUCH_BYPASS : source == "touch"
    RECEIVE_COMMAND --> MULTIMODAL_DIRECT : source == "multimodal"\n(gaze+click rule fired)
    RECEIVE_COMMAND --> GATE_1 : source == "voice" or "gesture"

    TOUCH_BYPASS --> LOCAL_INFER : Execute directly (no gates)
    MULTIMODAL_DIRECT --> LOCAL_INFER : Execute directly (no gates)

    GATE_1 --> GATE_2 : logprob ≥ whisper_logprob_min\nAND gesture_conf ≥ gesture_confidence_min
    GATE_1 --> CLOUD_TRANSCRIBE : logprob < whisper_logprob_min

    GATE_2 --> GATE_3 : token_count ≤ max_local_tokens\nAND no complexity keywords
    GATE_2 --> CLOUD_BEDROCK : token_count > max_local_tokens\nOR complexity keyword found

    GATE_3 --> GATE_4 : vram_free_gb ≥ vram_free_min_gb
    GATE_3 --> CLOUD_BEDROCK : vram_free_gb < vram_free_min_gb

    GATE_4 --> LOCAL_INFER : latency_ema_ms ≤ latency_budget_ms
    GATE_4 --> CLOUD_BEDROCK : latency_ema_ms > latency_budget_ms

    LOCAL_INFER --> LOG_AND_EXECUTE : action string returned
    CLOUD_BEDROCK --> LOG_AND_EXECUTE : action string returned
    CLOUD_TRANSCRIBE --> GATE_2 : re-transcribed text (high conf)

    LOG_AND_EXECUTE --> [*] : OutcomeLogger.record()\nDesktopAgent.execute()

    note right of GATE_1
        Confidence gate.
        Prevents noisy voice/gesture
        from reaching LLM.
    end note

    note right of GATE_2
        Complexity gate.
        Multi-step commands need
        Bedrock's reasoning.
    end note

    note right of GATE_3
        VRAM gate.
        Protects against OOM when
        GPU is under pressure.
    end note

    note right of GATE_4
        Latency gate.
        Falls back to cloud if local
        EMA > budget (default 800ms).
    end note
```

---

## 3. TouchInputServer — Connection State Machine

```mermaid
stateDiagram-v2
    [*] --> STARTING

    STARTING --> LISTENING : aiohttp server bound\nto 0.0.0.0:8765

    LISTENING --> HTTP_REQUEST : GET / from Safari
    HTTP_REQUEST --> LISTENING : Send single-file HTML app

    LISTENING --> WS_HANDSHAKE : WebSocket upgrade request
    WS_HANDSHAKE --> WS_CONNECTED : Handshake complete

    WS_CONNECTED --> PROCESSING_EVENT : JSON message received
    PROCESSING_EVENT --> WS_CONNECTED : Event dispatched to TouchInputReceiver

    WS_CONNECTED --> LISTENING : Client disconnected\n(iPad sleeps/navigates away)

    LISTENING --> SHUTDOWN : Ctrl-C / SIGTERM
    SHUTDOWN --> [*] : Flush, close sockets

    note right of LISTENING
        QR code printed to terminal
        at startup for easy iPad connection.
    end note

    note right of PROCESSING_EVENT
        Trackpad events → pyautogui directly.
        All other events → HybridCoordinator.
    end note
```

---

## 4. Sensor Degradation State Machine

```mermaid
stateDiagram-v2
    [*] --> FULL_STACK

    state FULL_STACK {
        [*] --> RESPEAKER_MIC
        [*] --> REALSENSE_DEPTH
        [*] --> ULTRALEAP_HAND
        [*] --> TOBII_GAZE
    }

    FULL_STACK --> BUDGET_STACK : Any full-stack sensor\nfails to connect

    state BUDGET_STACK {
        [*] --> FIFINE_MIC
        [*] --> OAKD_DEPTH
        [*] --> LEAP_V1_HAND
        [*] --> IRIS_GAZE
    }

    BUDGET_STACK --> IPAD_STACK : OAK-D absent\nAND iPad available

    state IPAD_STACK {
        [*] --> FIFINE_MIC
        [*] --> IPAD_LIDAR_DEPTH
        [*] --> MEDIAPIPE_HAND
        [*] --> BEAM_GAZE
    }

    IPAD_STACK --> VOICE_ONLY : All sensors except mic fail

    state VOICE_ONLY {
        [*] --> SYSTEM_MIC
        note "Whisper still processes\nvoice commands. No gaze,\nno gesture, no depth."
    }

    VOICE_ONLY --> TOUCH_ONLY : Mic also fails

    state TOUCH_ONLY {
        [*] --> IPAD_TOUCH_PAD
        note "Minimum viable mode.\nAll commands via iPad\ncommand pad only."
    }

    note right of FULL_STACK
        ~$820 hardware
        Best accuracy, 3D gesture,
        hardware gaze tracking
    end note

    note right of BUDGET_STACK
        ~$251 hardware
        Good accuracy, iris gaze,
        2D/3D gesture fallback
    end note

    note right of IPAD_STACK
        iPad Pro 2020+ replaces
        OAK-D and webcam.
        Adds touch command pad.
    end note
```

---

## 5. GestureDebouncer State Machine

```mermaid
stateDiagram-v2
    [*] --> READY

    READY --> FIRED : gesture detected\nconfidence ≥ floor

    FIRED --> COOLING : dispatch Command\nstart 800ms timer

    COOLING --> COOLING : same gesture detected\n(suppressed — no Command)

    COOLING --> READY : 800ms elapsed\nor different gesture detected

    note right of FIRED
        Command sent to FusionEngine.
        Prevents rapid-fire from
        sustained gesture hold.
    end note

    note right of COOLING
        Protects against arthritis-related
        tremor causing repeated triggers.
    end note
```
