# Fusion & Routing Flowcharts — iPad-Focused Architecture

> Source of truth for the priority order: `core/fusion_engine.py` and the
> "Sensor Priority" section of `CLAUDE.md`. Eye-gaze, head-pose, and mouth-sound
> control were **removed** (the standard iPad lacks the required TrueDepth sensor);
> the priority ladder shrank from 10 levels to **6**. Gesture control is KEPT.

---

## 1. FusionEngine — 6-Level Priority Decision (60 Hz tick)

```mermaid
flowchart TD
    Start([tick]) --> R1

    R1{"Rule 1\niPad touch command\npending?"}
    R1 -->|yes| E1["Emit Command\nsource=touch\nbypass LLM entirely"]
    R1 -->|no| R2

    R2{"Rule 2\nVoice 'click' keyword\npending?"}
    R2 -->|yes| E2["Emit Command\nsource=multimodal\nclick at current cursor\nbypass gates"]
    R2 -->|no| R3

    R3{"Rule 3\nTilt navigation active?\n(3a absolute position\n3b legacy velocity)"}
    R3 -->|yes| E3["Map tilt to cursor pos / dx,dy\nSend directly to pyautogui\n(no Command emitted)"]
    R3 -->|no| R4

    R4{"Rule 4\nGesture command\npending?"}
    R4 -->|yes| E4["Emit Command\nsource=gesture"]
    R4 -->|no| R5

    R5{"Rule 5\nOn-device voice keyword\n(Speech Framework) pending?"}
    R5 -->|yes| E5["Emit Command\nsource=voice_local"]
    R5 -->|no| R6

    R6{"Rule 6\nPC-transcribed voice\n(Whisper large-v3) pending?"}
    R6 -->|yes| E6["Emit Command\nsource=voice"]
    R6 -->|no| NONE["return None\n(no command this tick)"]

    E1 --> COORD([Route to HybridCoordinator])
    E2 --> COORD
    E4 --> COORD
    E5 --> COORD
    E6 --> COORD
    E3 --> PYAG([pyautogui direct])
```

---

## 2. HybridCoordinator — Gate 0 + 4-Gate Routing Decision

```mermaid
flowchart TD
    A([Receive Command]) --> TWIN["BehavioralTwinState.get_snapshot()\nAdjust thresholds if pain_day_active"]

    TWIN --> G0{"Gate 0 — Privacy\nText contains sensitive patterns?\n(password, api_key, SSN, token…)\nPersonal-KB query?"}
    G0 -->|sensitive / personal — force local| LOCAL
    G0 -->|clean| B

    B{"source?"}
    B -->|"touch / multimodal (voice-click)"| BYPASS["Bypass all gates\n→ direct local"]
    B -->|"voice_local"| G2
    B -->|"gesture / voice"| G1

    BYPASS --> LOCAL

    G1{"Gate 1 — Confidence\nwhisper_logprob ≥ min\nAND gesture_conf ≥ min\n(thresholds relaxed on pain day)"}
    G1 -->|pass| G2
    G1 -->|fail — voice low conf| TRANSCRIBE["Stage 1: vocab corrections\nStage 2: Amazon Transcribe\n(if audio_bytes present)"]
    G1 -->|fail — gesture low conf| DISCARD["Discard silently"]

    TRANSCRIBE --> G2

    G2{"Gate 2 — Complexity\ntoken_count ≤ max_local_tokens\nno complexity keywords"}
    G2 -->|pass| G3
    G2 -->|fail| CLOUD

    G3{"Gate 3 — VRAM\nvram_free_gb ≥ 8.0 GB\n(NVML probe off the event loop)"}
    G3 -->|pass| G4
    G3 -->|fail| CLOUD

    G4{"Gate 4 — Latency EMA\nlatency_ema_ms ≤ 600 ms"}
    G4 -->|pass| LOCAL
    G4 -->|fail| CLOUD

    LOCAL["LocalInference\nllama3.1:8b default\n(DomainClassifier → specialist via DevAgent)"]
    CLOUD["CloudInference\nAmazon Bedrock Claude Haiku\n(Bedrock-only; AgentCore deferred)"]

    LOCAL --> ACTION["action string"]
    CLOUD --> ACTION

    ACTION --> LOG["AgentDB.insert_command()\nagent.db commands table\nroute + gate_that_decided + latency_ms"]
    LOG --> EXEC["CommandExecutor.execute()\n16 verbs (11 access + 5 dev)"]
    EXEC --> TRAINER["ContinuousTrainer.observe()\nBehavioralTwinState.observe()"]
```

---

## 3. DesktopAgent — Action Dispatch and Target Resolution

```mermaid
flowchart TD
    A([action string + Command]) --> B["ActionParser.parse()\nsplit verb + target"]

    B --> C{"verb?"}

    C -->|CLICK| D["ElementFinder.find(target)"]
    C -->|SCROLL| M["pyautogui.scroll\n(direction, amount)"]
    C -->|TYPE| N["pyautogui.typewrite(text)\nASCII-only"]
    C -->|OPEN| O["psutil / subprocess\nlaunch application"]
    C -->|CLOSE| P["ElementFinder.find(target)\nthen close window"]
    C -->|HOTKEY| Q["pyautogui.hotkey(keys)"]
    C -->|DICTATE| R["keyboard_paste()\n(win32clipboard + Ctrl+V)\nfull unicode"]
    C -->|CLARIFY| S["TTS speak question\n(Kokoro local default)\nno desktop action"]
    C -->|INVALID| T["Log WARNING\nreject action"]

    D --> D3{"Found in\naccessibility tree?\n(UI Automation / AT-SPI)"}

    D3 -->|yes| D4["pyautogui.click\n(x, y from BoundingRect)"]
    D3 -->|no — canvas/Electron| D5["OCR fallback\nRTX 5090\nscreen text matching"]
    D5 --> D6{"Text match\nfound?"}
    D6 -->|yes| D4
    D6 -->|no| D7["Log WARNING\nfire CLARIFY instead"]

    D4 --> DONE
    M --> DONE
    N --> DONE
    O --> DONE
    P --> DONE
    Q --> DONE
    R --> DONE
    S --> DONE
    D7 --> DONE

    DONE([Outcome reported\nto ContinuousTrainer])
```

---

## 4. Touch / Sensor Input Routing — Decision Tree

```mermaid
flowchart TD
    A([WebSocket message\nfrom iPadApp]) --> B{"type?"}

    B -->|trackpad\ngesture=drag| C["IPadBridge._dispatch_trackpad\nBYPASS FusionEngine entirely"]
    B -->|trackpad\ngesture=tap, fingers=1| C2["IPadBridge → pyautogui.click()"]
    B -->|trackpad\ngesture=tap, fingers=2| C3["IPadBridge → pyautogui.rightClick()"]
    B -->|trackpad\ngesture=scroll, fingers=2| C4["IPadBridge → pyautogui.scroll(dy)"]
    B -->|touch_command| D["IPadBridge → FusionEngine.on_touch\nRule 1 — highest priority"]
    B -->|tilt_position / tilt| G["IPadBridge → FusionEngine.on_tilt\nRule 3 → pyautogui direct"]
    B -->|tilt_tap| G2["IPadBridge → pyautogui.click()\nat current cursor position"]
    B -->|keyword| I{"keyword = 'click'?"}
    I -->|yes| I1["FusionEngine voice-click\nRule 2 → click at cursor"]
    I -->|no| I2["FusionEngine.on_voice_local\nRule 5 → standard routing"]
    B -->|audio_stream| K["IPadBridge → WhisperStream\nSileroVAD + GPU transcription\n→ Rule 6"]
    B -->|camera_frame| L["IPadBridge → GestureProcessor\nMediaPipe\n→ Rule 4"]
    B -->|depth_frame| M["LiDAR/RealSense receiver\nused by GestureProcessor\nfor 3D gesture distances"]

    C --> DIRECT["Direct to pyautogui\nno LLM involved"]
    C2 --> DIRECT
    C3 --> DIRECT
    C4 --> DIRECT
    G --> DIRECT
    G2 --> DIRECT
    D --> FUSION["FusionEngine\n→ HybridCoordinator\n→ DesktopAgent"]
    I1 --> FUSION
    I2 --> FUSION
    K --> FUSION
    L --> FUSION
```

---

## 5. Source Priority vs Gate Bypass Summary

```mermaid
flowchart LR
    subgraph PRIORITY["Priority (high → low)"]
        P1["1. touch"]
        P2["2. voice 'click' (multimodal)"]
        P3["3. tilt (abs / legacy velocity)"]
        P4["4. gesture"]
        P5["5. voice_local (on-device keyword)"]
        P6["6. voice (PC Whisper)"]
    end

    subgraph GATES["Gate behavior"]
        G0["Bypass ALL 4 gates\n→ direct local LLM"]
        G_DIRECT["Bypass FusionEngine entirely\n→ pyautogui direct (no LLM)"]
        G_SKIP1["Skip Gate 1 only\n(already validated on-device)"]
        G_FULL["Full 4-gate evaluation"]
    end

    P1 --> G0
    P2 --> G0
    P3 --> G_DIRECT
    P4 --> G_FULL
    P5 --> G_SKIP1
    P6 --> G_FULL
```
