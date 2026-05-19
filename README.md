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

This starts a WebSocket server on `0.0.0.0:8765` (all interfaces) and advertises via mDNS. Open `http://<PC-IP>:8765/` on your iPad in Safari for the web client, or build the native Swift app.

To bind to a specific interface or change the port, pass arguments to `IPadBridge`:

```python
bridge = IPadBridge(port=9000, host="192.168.1.50")
```

### 4. Start the full pipeline (voice + gesture + coordinator)

```bash
python main.py --full
```

Add `--viewer` to open a desktop window showing live iPad camera and LiDAR depth feeds:

```bash
python main.py --full --viewer
```

Or run the viewer standalone (useful for verifying iPad video/depth streaming):

```bash
python sensor_viewer.py
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
3. Gaze delta cursor → eye movement drives cursor via relative deltas
4. Gaze + voice "click" → click at current cursor position
5. Gaze + gesture POINT → click at current cursor position
6. Tilt navigation → iPad tilt moves cursor
7. Head tracking → head pose moves cursor
8. Gesture alone → hand gesture command
9. On-device voice keyword → Speech Framework match
10. PC-transcribed voice → full Whisper + LLM pipeline

## Project Structure

```
├── main.py                    # Unified entry point (--full, --viewer, --viewer-only, --measure-vram, --safe-mode)
├── ipad_bridge.py             # WebSocket server for iPad (:8765)
├── hybrid_coordinator.py      # 4-gate routing (local vs cloud)
├── fusion_engine.py           # Sensor priority + fusion
├── command_executor.py        # Action dispatch (16 verbs)
├── whisper_stream.py          # Silero VAD → faster-whisper GPU
├── gesture_processor.py       # MediaPipe Hands → gesture commands
├── continuous_trainer.py      # Background learning (thresholds, vocab, few-shot)
├── audit_log.py               # Append-only security audit trail (SQLite WAL)
├── mcp_trust_classifier.py    # Taint analysis for MCP tool outputs (injection detection)
├── content_filter.py          # Secrets/PII redaction before API transmission
├── domain_classifier.py       # Route to specialist models
├── model_router.py            # VRAM-aware model selection
├── sensor_viewer.py           # Desktop viewer: camera + depth + overlays + snapshot (tkinter)
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
- ARKit (gaze tracking, head pose via `SharedFaceSession`)
- Speech framework (on-device keywords)
- AVFoundation (sound action detection, audio streaming)

`SharedFaceSession` multiplexes a single ARKit face-tracking session across GazeTracker and HeadTracker. Consumer handlers run on the ARKit delegate thread (zero-hop, ~16ms latency savings) and must be `@Sendable`. The session auto-recovers from errors (3 attempts, 1s backoff).

### Onboarding

On first launch, a 7-step wizard guides setup: Welcome → PC Connection (mDNS auto-discovery or manual IP) → Hardware Detection (shows available sensors) → Cursor Control (pick tilt/gaze/head/trackpad) → Calibration (tilt neutral position) → Voice & Sound (keywords, mouth sounds, Whisper streaming) → Done. Persists `onboardingComplete` to UserDefaults so it only runs once. Re-run from Settings if needed.

### Navigation

Six tabs: Commands, Trackpad, Keypad, Write, Settings, Sensors. The first four (Commands through Write) support page-style swipe-to-switch — swipe left/right on the content area to slide between them. Settings and Sensors are tap-only (utility views). The tab bar also supports a horizontal drag gesture (60pt threshold) to switch between any tabs.

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

## Sensor Viewer

A desktop window (tkinter) that displays live iPad camera and LiDAR depth feeds side by side. Useful for verifying that video/depth streaming is working before relying on it for gesture or spatial input.

- **Camera panel**: Decoded JPEG from iPad rear/front camera, with hand landmark skeleton overlay
- **Depth panel**: LiDAR depth map colorized blue (near) → red (far); invalid pixels shown as dark grey; gaze cursor overlay when active
- **Connection status**: Green (live), amber (stale >2s), grey (no data)
- **FPS counters**: Per-panel, smoothed with EMA
- **Depth hover readout**: Hover over the depth panel to see distance in metres at cursor position
- **Freeze-frame**: Pause both feeds to inspect a frame (Space or Freeze checkbox)
- **Snapshot**: Save current camera + depth frames as PNG to `snapshots/` (Ctrl+S or 📷 button)
- **Always-on-top**: Pin the viewer above other windows (Ctrl+T or Pin checkbox)
- **Aspect-ratio preserving resize**: Panels adapt to the actual frame dimensions from iPad

The viewer runs in a daemon thread and doesn't block the asyncio event loop. Frames are delivered via thread-safe queues from `ipad_bridge.py`.

```bash
# Standalone (empty panels until iPad connects)
python sensor_viewer.py

# Integrated with full pipeline
python main.py --full --viewer

# Viewer-only (bridge + viewer, no inference pipeline)
python main.py --viewer-only
```

### Keyboard Shortcuts

| Key | Action |
|-----|--------|
| Space | Toggle freeze-frame |
| Ctrl+T | Toggle always-on-top |
| Ctrl+S | Save snapshot to `snapshots/` |

## Configuration

- **Tilt sensitivity**: Scales rotation rate to cursor movement (default 1.0)
- **Tilt dead zone**: Minimum rotation rate threshold to filter noise (default 0.02 rad/s); applied per-axis independently
- **Tilt smoothing**: EMA filter (α=0.3) reduces jitter while preserving responsiveness; sub-pixel accumulation ensures small sustained tilts still produce movement
- **Tilt inversion**: Reverses tilt-to-cursor mapping for users who prefer opposite direction (default off)
- **Gravity-compensated projection**: Tilt uses the device gravity vector to project rotation rate into a ground-aligned frame. This means "tilt right" always maps to horizontal cursor movement regardless of the angle the iPad is held at (e.g. flat on a lap vs. propped at 30°). No user configuration needed — it adapts automatically.
- **Trackpad speed**: Adjustable in web client Settings tab
- **Palm rejection radius**: Configurable per-session
- **Gaze sensitivity**: Scales gaze delta to cursor movement speed (default in SettingsStore). Can be auto-tuned via the Gaze Calibration sheet.
- **Gaze smoothing**: EMA factor reuses `gazeStabilityThreshold` setting; suppresses jitter while preserving responsiveness. A dead zone (0.002) filters sub-threshold noise. Auto-tuned alongside sensitivity during calibration.
- **Gaze calibration**: A guided 15-second flow (Settings → Gaze Calibration) that measures baseline jitter and directional eye range, then computes optimal sensitivity and smoothing values. Phases: look straight (3s baseline) → left/right/up/down (2s each) → auto-compute → accept or retry.
- **Voice hotwords**: Auto-promoted from usage patterns

## Audit Log

An append-only security audit trail stored in `audit.db` (separate from `agent.db`). Records shell executions, API calls, file access, MCP tool invocations, security events, approval decisions, and session lifecycle events.

- SQLite WAL mode for concurrent reads during writes
- Triggers enforce append-only — no UPDATE or DELETE allowed on `audit_events`
- Async interface (`aiosqlite`) consistent with the AgentDB pattern
- Gracefully disabled if `aiosqlite` is not installed

```python
from audit_log import AuditLog

audit = AuditLog()
await audit.open("audit.db")
await audit.log_mcp_call("mouse_click", params={"x": 100, "y": 200})
await audit.log_security_event("PII detected in prompt", severity="warning")
await audit.log_shell_exec("notepad.exe", outcome="success")
await audit.close()
```

Event types: `shell_exec`, `api_call`, `file_access`, `mcp_call`, `security_event`, `approval`, `session_lifecycle`.

## MCP Trust Classifier

Taint analysis layer that scans all MCP tool outputs before they enter the next LLM reasoning step. Detects prompt injection, command injection, data exfiltration, and privilege escalation patterns embedded in untrusted data (email content, document text, web pages, file contents).

Risk levels:
- **HIGH** → block execution, require human approval
- **MEDIUM** → log warning, proceed with caution flag
- **LOW** → pass through, log for audit trail

```python
from mcp_trust_classifier import MCPTrustClassifier, RiskLevel

tc = MCPTrustClassifier(audit_log=audit)
verdict = await tc.classify(tool_name="read_email", result=email_body)
if verdict.should_block:
    # Require human approval before proceeding
    pass
```

Detection categories: `prompt_injection` (role reassignment, delimiter injection, roleplay triggers), `command_injection` (shell metacharacters, subshell execution), `data_exfil` (encoded URL parameters, known exfil endpoints), `priv_escalation` (sudo requests, dangerous permission changes).

A synchronous `classify_sync()` method is available for non-async contexts (skips audit logging).

## Content Filter

Regex-based secrets and PII scanner that redacts sensitive data before text is sent to external APIs. Integrates with `AuditLog` to record security events when secrets are detected.

Detected patterns include AWS keys, Anthropic/OpenAI/GitHub tokens, private keys, database connection strings, SSNs, and credit card numbers. The filter does not block execution — it redacts and logs, leaving the caller to decide whether to proceed.

```python
from content_filter import ContentFilter

cf = ContentFilter(audit_log=audit)
clean_text, findings = await cf.scrub(prompt_text)
# clean_text has secrets replaced with [REDACTED:pattern_name]
# findings list contains metadata (never the full secret)
```

A synchronous `scrub_sync()` method is available for non-async contexts (skips audit logging).

## Health-Aware Design

This system is designed for a user with:
- **JIA** — large touch targets, minimal fine motor input, fatigue-aware sessions
- **SVT** — no sudden startling, reliability over speed
- **Bipolar** — consistent UX, low cognitive load, robust to variable engagement

## License

Private project. Not for redistribution.
