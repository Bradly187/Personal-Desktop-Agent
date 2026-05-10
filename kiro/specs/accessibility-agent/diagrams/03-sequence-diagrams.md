# Sequence Diagrams — Key Interaction Flows

---

## 1. Voice Command — Local Path (happy path)

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant Mic as MicCapture
    participant VAD as SileroVAD
    participant Seg as UtteranceSegmenter
    participant ASR as WhisperTranscriber
    participant Fusion as FusionEngine
    participant Coord as HybridCoordinator
    participant Local as LocalInference (Ollama)
    participant Trainer as ContinuousTrainer
    participant Agent as DesktopAgent
    participant Finder as ElementFinder

    User->>Mic: speaks "open chrome"
    Mic->>VAD: audio chunk (512 samples @ 16kHz)
    VAD->>Seg: is_speech=true
    Seg->>Seg: state IDLE → CAPTURING
    Mic->>VAD: audio chunk (silence)
    VAD->>Seg: is_speech=false
    Seg->>ASR: emit utterance buffer
    ASR->>Fusion: Command(text="open chrome", logprob=-0.12, source="voice")

    Fusion->>Fusion: tick() — no active gaze+click rule
    Fusion->>Coord: route(Command)

    Note over Coord: Gate 1: logprob -0.12 ≥ min (-0.6) ✓
    Note over Coord: Gate 2: 2 tokens ≤ max (12), no complexity ✓
    Note over Coord: Gate 3: 3.8 GB free ≥ floor (2.0) ✓
    Note over Coord: Gate 4: EMA 295ms ≤ budget (800ms) ✓

    Coord->>Local: infer(Command)
    Local->>Coord: "OPEN chrome"
    Coord->>Trainer: outcome_hook(cmd, "OPEN chrome", pending)
    Coord->>Agent: execute("OPEN chrome", cmd)
    Agent->>Agent: _open("chrome")
    Agent->>User: chrome launches
    Agent->>Trainer: outcome_hook(cmd, "OPEN chrome", success)
    Trainer->>Trainer: record to routing_log.jsonl
    Trainer->>Trainer: FewShotMemory.record_success()
```

---

## 2. Gaze + Voice Click

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant Tobii as TobiiGazeTracker
    participant Mic as WhisperStream
    participant Fusion as FusionEngine
    participant Coord as HybridCoordinator
    participant Agent as DesktopAgent

    User->>Tobii: looks at "Submit" button
    Tobii->>Fusion: GazePoint(x=0.72, y=0.44, valid=true, conf=0.91)

    User->>Mic: says "click"
    Mic->>Fusion: Command(text="click", logprob=-0.05, source="voice")

    Fusion->>Fusion: tick() — gaze stable (spread < 4%) AND "click" keyword
    Note over Fusion: Rule 2 fires: gaze+voice click
    Fusion->>Fusion: resolve screen pixels (0.72 * W, 0.44 * H) = (979, 396)
    Fusion->>Coord: Command(text="click here", _gaze_coords=(979,396), source="multimodal")

    Note over Coord: Gate 0: multimodal gaze+click → direct local, skip gates
    Coord->>Agent: execute("CLICK here", cmd) with _gaze_coords
    Agent->>Agent: _click — pyautogui.moveTo(979, 396)
    Agent->>Agent: pyautogui.click()
    Agent->>User: cursor moves and clicks Submit
```

---

## 3. iPad Touch Command

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant Safari as iPad Safari
    participant WS as TouchInputServer (aiohttp WS)
    participant Recv as TouchInputReceiver
    participant Coord as HybridCoordinator
    participant Agent as DesktopAgent

    User->>Safari: taps "Scroll Down" button
    Safari->>WS: WebSocket JSON {"type":"tap","command":"scroll down"}
    WS->>Recv: on_event({"type":"tap","command":"scroll down"})
    Recv->>Recv: _handle_command → Command(text="scroll down", source="touch")

    Note over Coord: Touch source bypasses all gates — direct local
    Recv->>Coord: route(Command)
    Coord->>Agent: execute("SCROLL DOWN 3", cmd)
    Agent->>Agent: _scroll("DOWN", 3) → pyautogui.scroll(-3)
    Agent->>User: page scrolls down
```

---

## 4. iPad Trackpad Drag (direct path — no LLM)

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant Safari as iPad Safari
    participant WS as TouchInputServer
    participant Recv as TouchInputReceiver
    participant PyAG as pyautogui

    User->>Safari: drags finger on trackpad panel (Δx=40, Δy=20)
    Safari->>WS: WebSocket JSON {"type":"trackpad","dx":40,"dy":20}
    WS->>Recv: on_event({"type":"trackpad","dx":40,"dy":20})
    Recv->>Recv: _handle_trackpad — bypasses coordinator entirely
    Recv->>PyAG: pyautogui.moveRel(40 * speed, 20 * speed)
    PyAG->>User: cursor moves proportionally
```

---

## 5. Cloud Fallback — Complex Multi-Step Command

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant ASR as WhisperTranscriber
    participant Fusion as FusionEngine
    participant Coord as HybridCoordinator
    participant Bedrock as CloudInference (AWS Bedrock)
    participant Trainer as ContinuousTrainer
    participant Agent as DesktopAgent

    User->>ASR: "open chrome and then navigate to gmail"
    ASR->>Fusion: Command(text="open chrome and then navigate to gmail", logprob=-0.09, source="voice")
    Fusion->>Coord: route(Command)

    Note over Coord: Gate 1: logprob OK ✓
    Note over Coord: Gate 2: complexity keyword 'and then' → ESCALATE ✗
    Coord->>Bedrock: infer(Command)
    Bedrock->>Coord: "OPEN chrome\nHOTKEY ctrl+l\nDICTATE gmail.com\nHOTKEY Return"
    Coord->>Agent: execute multi-step action sequence
    Agent->>User: chrome opens, navigates to gmail
    Coord->>Trainer: outcome_hook(cmd, actions, success)
    Trainer->>Trainer: log to routing_log.jsonl (routed_to=cloud, gate_reached=2)
```

---

## 6. Startup and Sensor Initialization

```mermaid
sequenceDiagram
    autonumber
    participant Main as main.py
    participant SF as IPadSensorFusion
    participant Beam as BeamGazeTracker
    participant LiDAR as IPadLiDARCapture
    participant Cam as IPadWebcam
    participant WS as WhisperStream
    participant GS as GestureStream
    participant Touch as TouchInputServer
    participant Coord as HybridCoordinator
    participant Trainer as ContinuousTrainer

    Main->>SF: run()
    SF->>Beam: start()
    alt Beam SDK installed and iPad reachable
        Beam-->>SF: gaze stream active
    else SDK missing or unavailable
        Beam-->>SF: ImportError logged, fallback to IrisGazeEstimator
    end

    SF->>LiDAR: start()
    alt Record3D running on iPad
        LiDAR-->>SF: depth stream active
    else Not available
        LiDAR-->>SF: warning logged, no depth
    end

    SF->>Cam: start()
    SF->>WS: start()
    SF->>GS: start()
    SF->>Touch: start()
    SF->>Coord: initialize(thresholds)
    SF->>Trainer: start(coordinator)
    Trainer->>Trainer: load gesture_calibration.json
    Trainer->>Trainer: patch LocalInference with PromptAugmenter

    SF->>Main: print sensor status table
    Main->>Main: run asyncio event loop (FusionEngine at 60 Hz)
```

---

## 7. Continuous Trainer Threshold Adaptation

```mermaid
sequenceDiagram
    autonumber
    participant Trainer as ContinuousTrainer
    participant Reader as LogReader
    participant Tuner as ThresholdTuner
    participant Coord as HybridCoordinator

    loop every 5 minutes
        Trainer->>Tuner: run_pass()
        Tuner->>Reader: read_recent(n=500)
        Reader-->>Tuner: last 500 routing log entries

        Tuner->>Tuner: cloud_rate = count(routed_to=cloud) / total
        Tuner->>Tuner: local_fail_rate = count(outcome=failure, routed_to=local) / local_count

        alt cloud_rate > 0.30 AND local_fail_rate < 0.10
            Tuner->>Tuner: relax whisper_logprob_min by -0.05
            Tuner->>Coord: update_thresholds(new_thresholds)
        else cloud_rate < 0.05
            Tuner->>Tuner: tighten whisper_logprob_min by +0.02
            Tuner->>Coord: update_thresholds(new_thresholds)
        end
    end
```
