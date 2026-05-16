# Personal Desktop Agent

Multimodal accessibility desktop control agent for hands-free and low-effort Windows operation. Uses voice, hand gestures, eye gaze, iPad touch/tilt/head tracking, and sound actions to control a PC — all inference runs locally on an RTX 5090.

## Hardware Requirements

- **PC**: Windows 10/11, NVIDIA RTX 5090 (32 GB VRAM), 32+ GB RAM
- **iPad**: iPad Pro 2020+ (TrueDepth camera, LiDAR, accelerometer, gyroscope, microphone)
- **Network**: PC and iPad on the same local network (Wi-Fi)

## Quick Start

### 1. Install Python dependencies

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Pull local LLM models (Ollama)

```bash
ollama pull llama3.1:8b
ollama pull qwen3-coder:30b      # optional: code specialist
ollama pull deepseek-r1:8b       # optional: math/reasoning
```

### 3. Start the iPad Bridge

```bash
python ipad_bridge.py
```

This starts a WebSocket server on `:8765` and advertises via mDNS. Open `http://<PC-IP>:8765/` on your iPad in Safari for the web client, or build the native Swift app.

### 4. Start the full pipeline (voice + gesture + coordinator)

```bash
python main.py --full
```

### 5. Start the MCP server (for Claude integration)

```bash
python mcp_server/desktop_mcp_server.py
```

## Architecture

```
iPad Sensors → WebSocket → ipad_bridge.py → FusionEngine (priority routing)
                                           → WhisperStream (voice → text)
                                           → GestureProcessor (hand landmarks)
                                           ↓
                                    HybridCoordinator (4-gate local/cloud routing)
                                           ↓
                                    CommandExecutor → MCP Server → pyautogui/Win32
                                           ↓
                                    ContinuousTrainer (adapts thresholds over time)
```

## Input Priority (FusionEngine)

1. iPad touch command → immediate, bypasses LLM
2. Sound action → mapped mouth sounds (cluck, pop, hiss)
3. Gaze dwell click → resting gaze auto-clicks
4. Gaze + voice "click" → click at gaze coordinates
5. Gaze + gesture POINT → click at gaze coordinates
6. Tilt navigation → iPad tilt moves cursor
7. Head tracking → head pose moves cursor
8. Gesture alone → hand gesture command
9. On-device voice keyword → Speech Framework match
10. PC-transcribed voice → full Whisper + LLM pipeline

## Project Structure

```
├── main.py                    # Unified entry point (--full, --measure-vram, --safe-mode)
├── ipad_bridge.py             # WebSocket server for iPad (:8765)
├── hybrid_coordinator.py      # 4-gate routing (local vs cloud)
├── fusion_engine.py           # Sensor priority + fusion
├── command_executor.py        # Action dispatch (16 verbs)
├── whisper_stream.py          # Silero VAD → faster-whisper GPU
├── gesture_processor.py       # MediaPipe Hands → gesture commands
├── continuous_trainer.py      # Background learning (thresholds, vocab, few-shot)
├── domain_classifier.py       # Route to specialist models
├── model_router.py            # VRAM-aware model selection
├── health_viz.py              # Cosmic nebula system health visualization (pygame-ce)
├── dev_agent.py               # Plan→execute→reflect for dev tasks
├── local_inference.py         # Ollama / vLLM / Nemotron backends
├── mcp_server/                # MCP server (16 tools for Claude)
│   ├── desktop_mcp_server.py
│   └── tools/ (mouse, keyboard, screen, windows, handwriting)
├── web_client/                # iPad Safari fallback UI
├── iPadApp/                   # Native Swift/SwiftUI app
└── requirements.txt           # Pinned Python dependencies
```

## iPad App

The native app is built with Swift/SwiftUI targeting iPadOS 17+. It uses:
- Core Motion (tilt navigation)
- ARKit (gaze tracking, head pose)
- Speech framework (on-device keywords)
- AVFoundation (sound action detection, audio streaming)

### Building

Push changes to `iPadApp/` → GitHub Actions builds and uploads to TestFlight automatically. See `.github/SIGNING_SETUP.md` for code signing configuration.

## MCP Server Tools

| Category | Tools |
|----------|-------|
| Mouse | move, click, double_click, scroll, drag |
| Keyboard | type, paste, hotkey, press, key_down, key_up |
| Screen | screenshot, get_screen_size, find_text_on_screen |
| Windows | get_active_window, list_windows, focus_window |
| Handwriting | recognize_math, latex_to_unicode |

Set `SAFE_MODE=1` to block destructive tools during testing.

## Health Visualization

A cosmic nebula visualization of system health. Particles swirl outward from the center in spiral arms, creating a depth effect. Health metrics drive color (cyan-teal → golden amber → coral → deep rose), rotation speed, particle density, and arm structure.

```bash
pip install pygame-ce
python health_viz.py
python health_viz.py --width 1000 --height 700 --fps 36
```

CPU usage drives rotation speed and arm brightness. GPU VRAM/util drives core glow intensity. Temperature shifts the palette warmer. Process count and network connections control particle density. Press `Esc` or `Q` to quit.

### Temperature Monitoring

GPU temperature is read via pynvml (always available when an NVIDIA GPU is present). CPU package temperature requires LibreHardwareMonitor or OpenHardwareMonitor running with its WMI provider exposed — install the `wmi` Python package (`pip install wmi`). If neither WMI namespace is available, CPU temp reports as 0 and the visualization continues without it.

Thermal thresholds used for color mapping and error scoring:

| Range | Meaning |
|-------|---------|
| < 40 °C | Cool (green) |
| 40–65 °C | Warm (transition) |
| 65–80 °C | Hot (amber/warning) |
| > 90 °C | Critical (red, adds to error count) |

## Configuration

- **Tilt sensitivity**: Scales rotation rate to cursor movement (default 1.0)
- **Tilt dead zone**: Minimum rotation rate threshold to filter noise (default 0.02 rad/s); applied per-axis independently
- **Tilt smoothing**: EMA filter (α=0.3) reduces jitter while preserving responsiveness; sub-pixel accumulation ensures small sustained tilts still produce movement
- **Tilt inversion**: Reverses tilt-to-cursor mapping for users who prefer opposite direction (default off)
- **Gravity-compensated projection**: Tilt uses the device gravity vector to project rotation rate into a ground-aligned frame. This means "tilt right" always maps to horizontal cursor movement regardless of the angle the iPad is held at (e.g. flat on a lap vs. propped at 30°). No user configuration needed — it adapts automatically.
- **Trackpad speed**: Adjustable in web client Settings tab
- **Palm rejection radius**: Configurable per-session
- **Gaze dwell duration**: Default 800ms, adapts via ContinuousTrainer
- **Voice hotwords**: Auto-promoted from usage patterns

## Health-Aware Design

This system is designed for a user with:
- **JIA** — large touch targets, minimal fine motor input, fatigue-aware sessions
- **SVT** — no sudden startling, reliability over speed
- **Bipolar** — consistent UX, low cognitive load, robust to variable engagement

## License

Private project. Not for redistribution.
