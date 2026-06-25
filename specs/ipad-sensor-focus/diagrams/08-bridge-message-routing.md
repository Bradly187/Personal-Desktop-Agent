# Bridge Message Routing

```mermaid
flowchart TD
    IN(["WebSocket message\narrives"]) --> PARSE["Parse JSON\nextract type + id"]

    PARSE --> BAD{"Valid\nJSON?"}
    BAD -- No --> ERR_JSON["ack: error\n'invalid JSON'"]

    BAD -- Yes --> TYPE{"message\ntype?"}

    %% ── Direct execution (bypass FusionEngine) ──────────────────────────
    TYPE -- touch_command --> CE["CommandExecutor\n.execute(Command)"]
    TYPE -- tilt_tap --> TTAP["Command(action=CLICK)\nsource=touch"]
    TTAP --> CE

    %% ── Direct pyautogui (bypass LLM + executor) ────────────────────────
    TYPE -- trackpad --> TPEV{"trackpad\nevent?"}
    TPEV -- move --> MOVE["mouse_move(cx+dx, cy+dy)"]
    TPEV -- tap --> TAP["mouse_click(cx,cy,btn)"]
    TPEV -- scroll --> TSCROLL["mouse_scroll(cx,cy,dir)"]

    %% ── Sensor stream → FusionEngine ────────────────────────────────────
    TYPE -- "tilt\ntilt_position\nkeyword" --> FE["FusionEngine\n60 Hz tick, 6-level priority"]
    FE --> HC["HybridCoordinator\n4-gate routing"]
    HC --> CE

    %% ── Specialist receivers ────────────────────────────────────────────
    TYPE -- depth_frame --> LR["LiDARReceiver\ndepth map + confidence\nget_depth_at() for gestures"]
    TYPE -- camera_frame --> GP["GestureProcessor\nMediaPipe Hands"]
    TYPE -- audio_stream --> WHSP["WhisperStream\nSilero VAD + faster-whisper\n→ Command(source=voice)"]
    WHSP --> FE

    %% ── Inline processing ───────────────────────────────────────────────
    TYPE -- handwriting_image --> OCR["pix2tex LaTeX OCR\nunicode fallback converter"]
    OCR --> HWR(["handwriting_result\nLaTeX + unicode → client"])

    %% ── Control messages ────────────────────────────────────────────────
    TYPE -- ping --> PONG(["ack: pong"])
    TYPE -- "set_dwell_action\nset_feature_toggle" --> FST["Update FusionEngine\nfeature / dwell state"]

    %% ── Unknown ─────────────────────────────────────────────────────────
    TYPE -- other --> UNK["ack: error\n'unknown type'"]

    %% ── CommandExecutor verb expansion ──────────────────────────────────
    CE --> ACTION{"action\nverb?"}
    ACTION -- CLICK --> CLICK["mouse_click(x,y)"]
    ACTION -- MOUSEDOWN --> MDOWN["mouse_down(x,y)\nsynchronous"]
    ACTION -- MOUSEUP --> MUP["mouse_up(x,y)\nsynchronous"]
    ACTION -- SCROLL --> SCROLL["mouse_scroll(x,y,dir)"]
    ACTION -- TYPE --> TYPE2["keyboard_type(text)\nASCII only"]
    ACTION -- DICTATE --> DICTATE["clipboard paste\nfull unicode via win32"]
    ACTION -- OPEN --> OPEN["Win+S → type name\n→ Enter"]
    ACTION -- CLOSE --> CLOSE["hotkey(alt, f4)"]
    ACTION -- HOTKEY --> HOTKEY["hotkey(*keys)"]
    ACTION -- CLARIFY --> CLARIFY["Polly TTS\nspeak question aloud"]
    ACTION -- SCREENSHOT --> SS["screenshot(active window)\ncopy PNG → clipboard"]
    SS --> IMG(["screenshot msg\nbase64 PNG → client"])

    CLICK & MDOWN & MUP & SCROLL & TYPE2 & DICTATE & OPEN & CLOSE & HOTKEY & CLARIFY & SS & MOVE & TAP & TSCROLL --> ACK(["ack + status\nsent to client"])

    style IN fill:#1a3a5c,color:#fff
    style ACK fill:#1a4a2e,color:#fff
    style HWR fill:#1a4a2e,color:#fff
    style PONG fill:#1a4a2e,color:#fff
    style IMG fill:#1a4a2e,color:#fff
    style ERR_JSON fill:#4a1a1a,color:#fff
    style UNK fill:#4a1a1a,color:#fff
    style FE fill:#2a1a4a,color:#fff
    style HC fill:#2a2a4a,color:#fff
```

**Message routing categories:**

| Category | Types | Destination |
|----------|-------|-------------|
| Direct execution | `touch_command`, `tilt_tap` | CommandExecutor (bypasses FusionEngine) |
| Direct hardware | `trackpad` | pyautogui (bypasses LLM + executor) |
| Sensor stream | `tilt`, `tilt_position`, `keyword` | FusionEngine → HybridCoordinator → CommandExecutor |
| Specialist receivers | `depth_frame` → LiDARReceiver, `camera_frame` → GestureProcessor, `audio_stream` → WhisperStream → FusionEngine | per-subsystem |
| Inline | `handwriting_image` | pix2tex OCR → `handwriting_result` reply |
| Control | `ping`, `set_dwell_action`, `set_feature_toggle` | ack / FusionEngine state update |

The core iPad→PC sensor-stream types are: `tilt`, `tilt_position`, `keyword`, `touch_command`, `trackpad`, `audio_stream`, `camera_frame`, `depth_frame`, `handwriting_image`, `tilt_tap` (plus `tilt_ratchet`, `dwell_click`, `a2ui_event` and settings/diagnostics control types — 26 iPad→PC message types in total).
