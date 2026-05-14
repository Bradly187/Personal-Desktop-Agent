# Personal Desktop Agent

Multimodal accessibility desktop control for a single user with rheumatoid arthritis. An iPad Pro (2020+) is the sensor hub and primary touch surface; a Windows PC with RTX 5090 runs inference and executes desktop actions.

## What This Is

The user controls a Windows desktop through voice, eye gaze, head pose, hand gesture, iPad tilt, mouth sounds, and direct touch — all mapped to a constrained 9-verb action vocabulary. Sensor data streams over WebSocket from a native Swift iPad app to a Python backend on the PC. The PC runs local LLM inference (Ollama → vLLM in production) and executes commands via pyautogui/Win32.

- Full requirements (17): `.kiro/specs/ipad-sensor-focus/requirements.md`
- Architecture diagrams (13): `.kiro/specs/ipad-sensor-focus/diagrams/00-index.md`
- Tech stack: `.kiro/steering/tech.md`
- Open tasks: `.kiro/specs/ipad-sensor-focus/tasks.md`
- Daily reviews: `docs/`

## Current Status — Phases 1–4 skeleton complete

**Done (Phase 1):** `ipad_bridge.py`, `command_executor.py`, `mcp_server/` (5 tool modules + MCP server), `tests/test_bridge_client.py`, `tests/test_touch_scroll_e2e.py`, `requirements.txt`

**Done (Phase 2):**
- `fusion_engine.py` — 10-level priority sensor fusion at 60 Hz
- `hybrid_coordinator.py` — 4-gate routing (Gate 0 privacy + Gates 1–4); `routing_log.jsonl` outcome logging
- `local_inference.py` — `LocalInference` ABC + `OllamaInference`, `VLLMInference` (stub), `NemotronInference`
- `mcp_server/tools/handwriting.py` — pix2tex LaTeX OCR + unicode conversion
- `iPadApp/DesktopAgent/` — SwiftUI app: `WebSocketManager`, `TiltSensor`, `GazeTracker`, `HeadTracker`, `KeywordListener`, `SoundDetector`, `CommandPadView`, `TrackpadView`, `ScientificKeypadView`, `HandwritingCanvasView`, `SettingsStore`

**Done (Phase 3 skeleton):**
- `gesture_processor.py` — MediaPipe Hands; POINT/PINCH/OPEN_PALM/FIST; LiDAR depth integration; 800 ms debounce
- `lidar_receiver.py` — Decodes `depth_frame` messages; confidence-map filtering; `get_depth_at()`
- `domain_classifier.py` — Keyword-scoring domain detection: COMMAND/CODE/MATH/VISION/PLAN/GENERAL
- `model_router.py` — VRAM-aware specialist model selection; domain-tuned prompts
- `dev_agent.py` — Plan→execute→reflect agentic loop; 5 dev verbs; session context
- `tests/test_gaze_dwell_click.py`, `tests/test_gaze_dwell_e2e.py`, `tests/test_tilt_navigation.py` — integration tests (tasks 2.11, 2.12)

**Done (Phase 4 skeleton):**
- `continuous_trainer.py` — Routing threshold adaptation; few-shot ranking; gesture confidence floors; delegates storage to `AgentDB`
- `main.py` — Unified entry point; `--measure-vram`; startup status table; Ctrl-C shutdown
- `benchmark_models.py` — Ollama model benchmark; p50/p95 latency; VRAM snapshots
- `whisper_stream.py` — GPU-accelerated speech; Silero VAD + faster-whisper; emits `Command(source="voice")` to FusionEngine
- `db.py` — `AgentDB` (aiosqlite, 12 tables) + `AnalyticsDB` (DuckDB); MiniLM semantic few-shot retrieval
- `migrate.py` — One-time migration from legacy files (trainer.db, routing_log.jsonl, benchmark_results.json) to new DB layer

**Not yet built:** full `VLLMInference` (task 2.13)

## Run Commands

```bash
# Full pipeline — bridge + FusionEngine + HybridCoordinator + ContinuousTrainer
python main.py [--port 8765] [--no-mdns] [--debug] [--safe-mode]

# Measure actual VRAM usage on RTX 5090 (loads all models, prints table, exits)
python main.py --measure-vram

# MCP server — Claude's desktop control interface (stdio transport)
python mcp_server/desktop_mcp_server.py

# iPad WebSocket bridge (standalone, without FusionEngine)
python ipad_bridge.py [--port 8765] [--no-mdns] [--debug]

# End-to-end integration test (start bridge first in another terminal)
python tests/test_bridge_client.py

# Install dependencies
pip install -r requirements.txt
```

Set `--safe-mode` (or `SAFE_MODE=1`) to block `keyboard_type` and `mouse_drag` during testing.

## Action Vocabulary

**Accessibility verbs (11)** — for iPad sensor pipeline and simple commands:
`CLICK` `MOUSEDOWN` `MOUSEUP` `SCROLL` `TYPE` `OPEN` `CLOSE` `HOTKEY` `DICTATE` `CLARIFY` `SCREENSHOT`

`MOUSEDOWN`/`MOUSEUP` are executed synchronously (no `asyncio.to_thread`) because they are timing-critical for drag-select and must not compete with trackpad moves.

**Dev-agent verbs (5)** — emitted by specialist models via DevAgent:
`WRITE_FILE` `RUN_TERMINAL` `EXPLAIN` `SEARCH_WEB` `READ_SCREEN`

The `CommandExecutor` handles all 16 verbs. The `DomainClassifier` determines which pipeline a query enters — accessibility (llama3.1:8b, verb-first) or dev-agent (specialist model, free-form).

## Architecture

```
iPad sensors  → WebSocket :8765 → ipad_bridge → FusionEngine → HybridCoordinator ─┐
                                                                                    │
                                               DomainClassifier                     │
                                              /               \                     │
                                       command domain       dev domains             │
                                             │           (CODE/MATH/VISION/         │
                                        llama3.1:8b       PLAN/GENERAL)            │
                                        verb-first         ModelRouter              │
                                             │            specialist LLM            │
                                             └──────────────────┘                  │
                                                      │                             │
                                               CommandExecutor                      │
                                            (16 verbs: 11 access + 5 dev)          │
                                                      │                             │
                                         mcp_server/tools/ → pyautogui / Win32 ←──┘

Claude (MCP) → stdio → mcp_server/desktop_mcp_server.py → mcp_server/tools/
```

Every pipeline boundary carries a `Command` dataclass. `DomainClassifier` gates the pipeline: simple commands go straight to `llama3.1:8b`; dev-domain queries go to `DevAgent` which selects the right specialist model.

## Key Files

| File | Purpose |
|------|---------|
| `ipad_bridge.py` | aiohttp WebSocket server on :8765; routes 13 incoming message types; sends `ack`, `status`, `screenshot`, `handwriting_result` replies |
| `command_executor.py` | Maps 16 action verbs to mcp_server tool calls; `_resolve_coords` falls back to screen centre; SCREENSHOT defaults to active window and copies to Windows clipboard |
| `mcp_server/desktop_mcp_server.py` | MCP stdio server; 14 tools; `SAFE_MODE` env var |
| `mcp_server/tools/mouse.py` | move, click, double_click, scroll, drag |
| `mcp_server/tools/keyboard.py` | type, hotkey, press, paste (unicode via clipboard) |
| `mcp_server/tools/screen.py` | screenshot (base64 PNG), get_screen_size, find_text_on_screen (OCR) |
| `mcp_server/tools/windows.py` | get_active_window, list_windows, focus_window (win32gui + psutil) |
| `mcp_server/tools/handwriting.py` | pix2tex LaTeX OCR; latex_to_unicode fallback converter |
| `fusion_engine.py` | 60 Hz tick loop; 10-level sensor priority; direct pyautogui for tilt/head |
| `hybrid_coordinator.py` | 4-gate routing (Gate 0 privacy + Gates 1–4); AWS Bedrock fallback; outcome logger |
| `local_inference.py` | `LocalInference` ABC; `OllamaInference`, `VLLMInference` (stub), `NemotronInference` |
| `continuous_trainer.py` | Routing threshold adaptation; few-shot ranking; gesture confidence floors; delegates all storage to `AgentDB` |
| `lidar_receiver.py` | Decodes depth_frame messages; confidence-map filtering; `get_depth_at()` |
| `gesture_processor.py` | MediaPipe Hands; POINT/PINCH/PALM/FIST; LiDAR pinch depth; 800 ms debounce |
| `domain_classifier.py` | Keyword-scoring domain detection: COMMAND/CODE/MATH/VISION/PLAN/GENERAL |
| `model_router.py` | VRAM-aware specialist model selection; domain-tuned prompts; Ollama inference |
| `dev_agent.py` | Plan→execute→reflect agentic loop; 5 dev verbs; session context |
| `main.py` | Unified entry point; `--measure-vram`; startup status table; Ctrl-C shutdown |
| `whisper_stream.py` | GPU-accelerated speech: Silero VAD + faster-whisper large-v3; emits `Command(source="voice")` to FusionEngine |
| `db.py` | `AgentDB` (aiosqlite, 12 tables, all pipeline writes) + `AnalyticsDB` (DuckDB, benchmark history); MiniLM semantic retrieval |
| `migrate.py` | One-time migration from legacy trainer.db / routing_log.jsonl / benchmark_results.json to new DB layer; run once then delete |
| `tests/test_bridge_client.py` | Simulated iPad client; sends 8 test messages; verifies ack for each |

## WebSocket Protocol

**iPad → PC (13 types):** `tilt`, `gaze`, `gaze_dwell`, `head_pose`, `keyword`, `sound_action`, `touch_command`, `trackpad`, `audio_stream`, `camera_frame`, `depth_frame`, `handwriting_image`, `tilt_tap`

**PC → iPad (4 types):** `ack` (every message), `status` (window + cursor after each command), `screenshot` (base64 PNG after SCREENSHOT action), `handwriting_result` (LaTeX + unicode after handwriting_image)

`touch_command` and `trackpad` bypass FusionEngine directly. `handwriting_image` is handled inline by the bridge. `audio_stream` feeds `WhisperStream` → FusionEngine priority 10. The remaining 9 sensor types (gaze, head_pose, keyword, etc.) are dispatched to FusionEngine or logged until their pipeline stages are wired.

## Sensor Priority (FusionEngine — `fusion_engine.py`)

1. iPad touch command — bypasses LLM entirely
2. Sound action (mouth sounds via AVFoundation)
3. Gaze dwell click
4. Gaze + voice "click"
5. Gaze + gesture POINT
6. Tilt navigation (Core Motion)
7. Head tracking (ARKit face anchor)
8. Gesture alone
9. On-device voice keyword (Speech Framework)
10. PC-transcribed voice (Whisper large-v3 on GPU)

## Coding Conventions

- All pipeline classes are `async`; blocking I/O uses `asyncio.to_thread`
- Every sensor class must degrade gracefully — wrap hardware imports in `try/except ImportError`, log a warning, never crash
- No global state outside dataclass instances; all state lives in class attributes
- `Command` is the universal DTO — never pass raw dicts across pipeline boundaries
- Log levels: DEBUG per-frame, INFO commands/routing, WARNING sensor failures, ERROR unrecoverable

## Known Gotchas

- `SCREENSHOT` automatically copies the captured image to the Windows clipboard (CF_DIB via `win32clipboard`) so the user can Ctrl+V immediately. Failure to copy is non-fatal — the base64 result is still returned.
- `pyautogui.typewrite` is ASCII-only — `TYPE` has this limitation and keeps it for backward compat. `DICTATE` was fixed (2026-05-07) to use `keyboard_paste()` (win32clipboard + Ctrl+V) and now supports full unicode including mathematical symbols.
- `pyautogui.FAILSAFE = True` is set globally — moving the mouse to the top-left screen corner raises `FailSafeException`. All tool wrappers inherit this behaviour.
- `find_text_on_screen` matches individual OCR words only; search phrases spanning multiple words won't match.
- Tesseract OCR must be installed system-wide for `find_text_on_screen` to function; the function returns `{"found": false, "error": "pytesseract not installed"}` otherwise.
- mDNS advertisement requires `zeroconf`; the bridge degrades gracefully without it (logs a warning, still accepts connections).
- VRAM measured 2026-05-08: baseline 8.3 GB, Whisper +4.2 GB, ~19 GB free for LLM. `llama3.1:70b` does not fit alongside Whisper.
- Default LLM is `llama3.1:8b` (4.6 GB VRAM) for command, plan, and general domains. `nemotron-mini` scored 25% and is not suitable without fine-tuning. `deepseek-r1:8b` produces reasoning output incompatible with the verb-first format. `gpt-oss:20b` removed from primary profiles (retained in fallback chains only).

## MCP Server Registration (Claude Code)

Add to `~/.claude/claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "desktop-agent": {
      "command": "python",
      "args": ["E:/Personal_Desktop_Agent/mcp_server/desktop_mcp_server.py"]
    }
  }
}
```
