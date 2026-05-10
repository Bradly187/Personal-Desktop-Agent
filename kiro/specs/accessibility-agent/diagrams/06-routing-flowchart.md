# Routing & Decision Flowcharts

---

## 1. HybridCoordinator — Complete Routing Decision

```mermaid
flowchart TD
    A([Receive Command]) --> B{source?}

    B -->|touch| C[Bypass all gates]
    B -->|multimodal\ngaze+click| D[Bypass all gates]
    B -->|voice or gesture| E

    C --> LOCAL
    D --> LOCAL

    E{Gate 1\nConfidence} -->|logprob ≥ min\nAND gesture_conf ≥ min| F
    E -->|logprob < min| G[Route to\nAmazon Transcribe]
    G -->|re-transcribed text| F

    F{Gate 2\nComplexity} -->|tokens ≤ max\nno complexity words| H
    F -->|tokens > max\nOR 'and then' / 'after that' / 'for each'| CLOUD

    H{Gate 3\nVRAM} -->|free_gb ≥ floor| I
    H -->|free_gb < floor| CLOUD

    I{Gate 4\nLatency} -->|EMA ≤ budget| LOCAL
    I -->|EMA > budget| CLOUD

    LOCAL[LocalInference\nOllama Llama 3.1 70B\nRTX 5090]
    CLOUD[CloudInference\nAWS Bedrock Claude]

    LOCAL --> J[action string]
    CLOUD --> J

    J --> K{action verb?}

    K -->|CLICK| L[ElementFinder\nlookup target]
    K -->|SCROLL| M[pyautogui.scroll]
    K -->|TYPE| N[pyautogui.typewrite]
    K -->|OPEN| O[psutil / subprocess\nlaunch app]
    K -->|CLOSE| P[ElementFinder\nclose window]
    K -->|HOTKEY| Q[pyautogui.hotkey]
    K -->|DICTATE| R[clipboard paste\nfaster than keystrokes]
    K -->|CLARIFY| S[TTS question\nno desktop action]

    L --> L1{found in\na11y tree?}
    L1 -->|yes| L2[pyautogui.click\nx,y from tree]
    L1 -->|no| L3[EasyOCR fallback\nRTX 5090]
    L3 --> L2

    L2 --> T[OutcomeLogger\nrecord outcome]
    M --> T
    N --> T
    O --> T
    P --> T
    Q --> T
    R --> T
    S --> T

    T --> U[ContinuousTrainer\noutcome_hook]
    U --> V([Done])
```

---

## 2. FusionEngine — Priority Rule Evaluation (60 Hz tick)

```mermaid
flowchart TD
    Start([tick]) --> R1

    R1{Rule 1\niPad touch\ncommand pending?} -->|yes| Emit1[Emit Command\nsource=touch\nbypasses LLM]
    R1 -->|no| R2

    R2{Rule 2\nGaze valid + stable\nAND voice = 'click'?} -->|yes| Emit2[Emit Command\nsource=multimodal\n_gaze_coords set\nbypasses gates]
    R2 -->|no| R3

    R3{Rule 3\nGaze valid + stable\nAND gesture = POINT?} -->|yes| Emit3[Emit Command\nsource=multimodal\n_gaze_coords set]
    R3 -->|no| R4

    R4{Rule 4\nGesture command\npending?} -->|yes| Emit4[Emit Command\nsource=gesture]
    R4 -->|no| R5

    R5{Rule 5\nVoice command\npending?} -->|yes| Emit5[Emit Command\nsource=voice]
    R5 -->|no| NoCmd[No Command\nthis tick]

    Emit1 --> End
    Emit2 --> End
    Emit3 --> End
    Emit4 --> End
    Emit5 --> End
    NoCmd --> End([return None])
    End([return Command])
```

---

## 3. Continuous Trainer — Adaptation Decision Tree

```mermaid
flowchart TD
    OHook([outcome_hook called]) --> Log1[Write to routing_log.jsonl]
    Log1 --> FSM{outcome?}
    FSM -->|success| FSM2[FewShotMemory\nrecord_success]
    FSM -->|failure or clarify| Skip[skip few-shot record]

    subgraph THRESHOLD_PASS ["Threshold Pass — every 5 min"]
        T1([run_pass]) --> T2[Read last 500 log entries]
        T2 --> T3{cloud_rate > 30%\nAND local_fail < 10%?}
        T3 -->|yes — over-routing to cloud| T4[Relax Gate 1 min\n-0.05]
        T3 -->|no| T5{cloud_rate < 5%?}
        T5 -->|yes — local doing well| T6[Tighten Gate 1 min\n+0.02]
        T5 -->|no| T7[No change]
        T4 --> T8{latency_ema > budget?}
        T8 -->|yes| T9[Increase latency_budget_ms\n+50ms]
        T8 -->|no| TEnd([update_thresholds])
        T6 --> TEnd
        T7 --> TEnd
        T9 --> TEnd
    end

    subgraph VOCAB_PASS ["Vocabulary Pass — every 30 min"]
        V1([run_pass]) --> V2[Read successful voice commands]
        V2 --> V3[Count word freq in transcriptions]
        V3 --> V4{word appears ≥ 3 times?}
        V4 -->|yes| V5[Add to hotwords.txt]
        V4 -->|no| V6[Skip]
        V5 --> VEnd([WhisperTranscriber reloads])
        V6 --> VEnd
    end

    subgraph GESTURE_PASS ["Gesture Calibration Pass — every 5 min"]
        G1([run_pass]) --> G2[For each gesture with ≥ 10 samples]
        G2 --> G3[Compute p10 of confidences]
        G3 --> G4[confidence_floor = p10 - 0.05]
        G4 --> G5[Clamp: min=0.40, max=0.85]
        G5 --> GEnd([Write gesture_calibration.json])
    end

    subgraph COMPACTION ["Nightly Compaction — 02:00"]
        C1([run_pass]) --> C2[For each example in few_shot_memory.db]
        C2 --> C3{last_used_at > 30 days ago\nAND usage_count < 3?}
        C3 -->|yes| C4[Mark is_stale = 1]
        C3 -->|no| C5[Keep]
        C4 --> CEnd([VACUUM])
        C5 --> CEnd
    end
```

---

## 4. ElementFinder — Target Resolution Strategy

```mermaid
flowchart TD
    A([find target name]) --> B[Walk OS Accessibility Tree]

    B --> B1{Windows?}
    B1 -->|yes| B2[UI Automation\npywinauto]
    B1 -->|no Linux/Mac| B3[AT-SPI\npyatspi]

    B2 --> C{Element found\nin tree?}
    B3 --> C

    C -->|yes| D[Return x,y coordinates\nfrom BoundingRect]
    C -->|no — canvas, Electron,\nor unlabelled element| E[EasyOCR Fallback\nRTX 5090]

    E --> F[Screenshot current screen]
    F --> G[EasyOCR.readtext on GPU]
    G --> H{Text match found\nin results?}
    H -->|yes| I[Return center of\nmatched bounding box]
    H -->|no| J[Return None\nlog WARNING]

    D --> K([Caller gets coords])
    I --> K
    J --> L([Caller falls back to\nCLARIFY if gaze unavailable])
```
