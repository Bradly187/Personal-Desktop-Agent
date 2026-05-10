# Fusion & Routing Flowcharts — iPad-Focused Architecture

---

## 1. FusionEngine — 10-Level Priority Decision (60 Hz tick)

```mermaid
flowchart TD
    Start([tick]) --> R1

    R1{"Rule 1\niPad touch command\npending?"}
    R1 -->|yes| E1["Emit Command\nsource=touch\nbypass all gates"]
    R1 -->|no| R2

    R2{"Rule 2\nSound action\npending?"}
    R2 -->|yes| E2["Emit Command\nsource=sound_action\nbypass all gates"]
    R2 -->|no| R3

    R3{"Rule 3\nGaze dwell\nfired?"}
    R3 -->|yes| E3["Emit Command\nsource=gaze_dwell\n_gaze_coords set\nbypass all gates"]
    R3 -->|no| R4

    R4{"Rule 4\nGaze stable AND\nvoice_local = 'click'?"}
    R4 -->|yes| E4["Emit Command\nsource=multimodal\n_gaze_coords set\nbypass gates"]
    R4 -->|no| R5

    R5{"Rule 5\nGaze stable AND\ngesture = POINT?"}
    R5 -->|yes| E5["Emit Command\nsource=multimodal\n_gaze_coords set"]
    R5 -->|no| R6

    R6{"Rule 6\nTilt vector outside\ndead zone?"}
    R6 -->|yes| E6["Map tilt to dx/dy\nSend directly to\npyautogui.moveRel\n(no Command emitted)"]
    R6 -->|no| R7

    R7{"Rule 7\nHead pose delta\nactive?"}
    R7 -->|yes| E7["Map head pose to dx/dy\nSend directly to\npyautogui.moveRel\n(no Command emitted)"]
    R7 -->|no| R8

    R8{"Rule 8\nGesture command\npending?"}
    R8 -->|yes| E8["Emit Command\nsource=gesture"]
    R8 -->|no| R9

    R9{"Rule 9\nOn-device voice\nkeyword pending?"}
    R9 -->|yes| E9["Emit Command\nsource=voice_local"]
    R9 -->|no| R10

    R10{"Rule 10\nPC-transcribed\nvoice pending?"}
    R10 -->|yes| E10["Emit Command\nsource=voice"]
    R10 -->|no| NONE["return None\n(no command this tick)"]

    E1 --> COORD([Route to HybridCoordinator])
    E2 --> COORD
    E3 --> COORD
    E4 --> COORD
    E5 --> COORD
    E8 --> COORD
    E9 --> COORD
    E10 --> COORD
    E6 --> PYAG([pyautogui direct])
    E7 --> PYAG
```

---

## 2. HybridCoordinator — 4-Gate Routing Decision

```mermaid
flowchart TD
    A([Receive Command]) --> B{"source?"}

    B -->|"touch\nsound_action\ngaze_dwell\nmultimodal"| BYPASS["Bypass all gates\n→ direct local"]
    B -->|"voice_local"| SKIP1["Skip Gate 1\n(on-device confidence\nalready validated)"]
    B -->|"gesture\nvoice"| G1

    BYPASS --> LOCAL
    SKIP1 --> G2

    G1{"Gate 1 — Confidence\nlogprob ≥ whisper_logprob_min\nAND gesture_conf ≥ gesture_confidence_min"}
    G1 -->|pass| G2
    G1 -->|fail — voice low conf| TRANSCRIBE["Route to\nAmazon Transcribe\nre-transcribe then return to G2"]
    G1 -->|fail — gesture low conf| DISCARD["Discard silently\nno Command produced"]

    TRANSCRIBE --> G2

    G2{"Gate 2 — Complexity\ntoken_count ≤ max_local_tokens\nAND no complexity keywords\n('and then', 'after that', 'for each')"}
    G2 -->|pass| G3
    G2 -->|fail| CLOUD["Route to\nAWS Bedrock\n(Claude — complex reasoning)"]

    G3{"Gate 3 — VRAM\nvram_free_gb ≥ vram_free_min_gb"}
    G3 -->|pass| G4
    G3 -->|fail| CLOUD

    G4{"Gate 4 — Latency\nlatency_ema_ms ≤ latency_budget_ms"}
    G4 -->|pass| LOCAL
    G4 -->|fail| CLOUD

    LOCAL["LocalInference ABC\n(OllamaInference default, ~450ms\nVLLMInference Phase 2, ~280ms)"]
    CLOUD["CloudInference\nAWS Bedrock Claude"]

    LOCAL --> ACTION["action string"]
    CLOUD --> ACTION

    ACTION --> LOG["OutcomeLogger\nrouting_log.jsonl"]
    LOG --> AGENT["DesktopAgent.execute()"]
```

---

## 3. DesktopAgent — Action Dispatch and Target Resolution

```mermaid
flowchart TD
    A([action string + Command]) --> B["ActionParser.parse()\nsplit verb + target"]

    B --> C{"verb?"}

    C -->|CLICK| D["ElementFinder.find(target)"]
    C -->|SCROLL| M["pyautogui.scroll\n(direction, amount)"]
    C -->|TYPE| N["pyautogui.typewrite(text)"]
    C -->|OPEN| O["psutil / subprocess\nlaunch application"]
    C -->|CLOSE| P["ElementFinder.find(target)\nthen close window"]
    C -->|HOTKEY| Q["pyautogui.hotkey(keys)"]
    C -->|DICTATE| R["Clipboard paste\n(pyperclip + ctrl+v)\nfaster than keystrokes"]
    C -->|CLARIFY| S["TTS speak question\n(pyttsx3 local\nor Polly cloud)\nno desktop action"]
    C -->|INVALID| T["Log WARNING\nreject action"]

    D --> D1{"_gaze_coords\non Command?"}
    D1 -->|yes — gaze targeting| D2["pyautogui.moveTo\n(gaze_coords)\npyautogui.click()"]
    D1 -->|no — resolve target| D3{"Found in\naccessibility tree?\n(UI Automation / AT-SPI)"}

    D3 -->|yes| D4["pyautogui.click\n(x, y from BoundingRect)"]
    D3 -->|no — canvas/Electron| D5["EasyOCR fallback\nRTX 5090\nscreen text matching"]
    D5 --> D6{"Text match\nfound?"}
    D6 -->|yes| D4
    D6 -->|no| D7["Log WARNING\nfire CLARIFY instead"]

    D2 --> DONE
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

## 4. Touch Input Routing — Full Decision Tree

```mermaid
flowchart TD
    A([WebSocket message\nfrom iPadApp]) --> B{"type?"}

    B -->|trackpad\ngesture=drag| C["IPadBridge._dispatch_trackpad\nBYPASS FusionEngine entirely"]
    B -->|trackpad\ngesture=tap, fingers=1| C2["IPadBridge → pyautogui.click()"]
    B -->|trackpad\ngesture=tap, fingers=2| C3["IPadBridge → pyautogui.rightClick()"]
    B -->|trackpad\ngesture=scroll, fingers=2| C4["IPadBridge → pyautogui.scroll(dy)"]
    B -->|touch_command| D["IPadBridge → FusionEngine.on_touch\nRule 1 — highest priority"]
    B -->|gaze| E["IPadBridge → FusionEngine gaze buffer\nused by Rules 3/4/5"]
    B -->|gaze_dwell| F["IPadBridge → FusionEngine.on_gaze_dwell\nRule 3"]
    B -->|tilt| G["IPadBridge → FusionEngine.on_tilt\nRule 6 → pyautogui direct"]
    B -->|tilt_tap| G2["IPadBridge → pyautogui.click()\nat current cursor position"]
    B -->|head_pose| H["IPadBridge → FusionEngine.on_head\nRule 7 → pyautogui direct"]
    B -->|keyword| I{"keyword = 'click'\nAND gaze stable?"}
    I -->|yes| I1["FusionEngine.on_gaze_voice\nRule 4 → multimodal click"]
    I -->|no| I2["FusionEngine.on_voice_local\nRule 9 → standard routing"]
    B -->|sound_action| J["IPadBridge → FusionEngine.on_sound_action\nRule 2"]
    B -->|audio_stream| K["IPadBridge → WhisperStream\nSileroVAD + GPU transcription\n→ Rule 10"]
    B -->|camera_frame| L["IPadBridge → GestureProcessor\nMediaPipe + LiDAR depth\n→ Rule 8"]
    B -->|depth_frame| M["LiDARReceiver.update\nused by GestureProcessor\nfor 3D gesture distances"]

    C --> DIRECT["Direct to pyautogui\nno LLM involved"]
    C2 --> DIRECT
    C3 --> DIRECT
    C4 --> DIRECT
    G --> DIRECT
    G2 --> DIRECT
    H --> DIRECT
    D --> FUSION["FusionEngine\n→ HybridCoordinator\n→ DesktopAgent"]
    E --> FUSION
    F --> FUSION
    I1 --> FUSION
    I2 --> FUSION
    J --> FUSION
    K --> FUSION
    L --> FUSION
```

---

## 5. Gaze Confidence and Stability Logic

```mermaid
flowchart TD
    A([Gaze update received\n{x, y, conf}]) --> B{"conf ≥ 0.55?"}
    B -->|no| DISC["Discard — not used\nfor targeting"]
    B -->|yes| C["Add to stability\nbuffer (last N frames)"]

    C --> D{"Spread of last N\ngaze points < 4%\nof screen diagonal?"}
    D -->|no| UNSTABLE["Gaze valid\nbut NOT stable\nno targeting active"]
    D -->|yes| STABLE["Gaze STABLE\nRules 4/5 can fire"]

    STABLE --> E["Start or continue\ndwell timer"]
    E --> F{"Dwell timer\n≥ configured duration?"}
    F -->|no| STABLE
    F -->|yes| DWELL["Fire gaze_dwell event\nRule 3 — auto click\nno voice/gesture needed"]

    UNSTABLE --> G["Dwell timer resets\nRules 4/5 cannot fire\nuntil stability restored"]
```

---

## 6. Source Priority vs Gate Bypass Summary

```mermaid
flowchart LR
    subgraph PRIORITY["Priority (high → low)"]
        P1["1. touch"]
        P2["2. sound_action"]
        P3["3. gaze_dwell"]
        P4["4. multimodal (gaze+click)"]
        P5["5. multimodal (gaze+POINT)"]
        P6["6. tilt"]
        P7["7. head_track"]
        P8["8. gesture"]
        P9["9. voice_local"]
        P10["10. voice"]
    end

    subgraph GATES["Gate behavior"]
        G0["Bypass ALL 4 gates\n→ direct local LLM"]
        G_DIRECT["Bypass FusionEngine entirely\n→ pyautogui direct (no LLM)"]
        G_SKIP1["Skip Gate 1 only\n(already validated on-device)"]
        G_FULL["Full 4-gate evaluation"]
    end

    P1 --> G0
    P2 --> G0
    P3 --> G0
    P4 --> G0
    P5 --> G0
    P6 --> G_DIRECT
    P7 --> G_DIRECT
    P8 --> G_FULL
    P9 --> G_SKIP1
    P10 --> G_FULL
```
