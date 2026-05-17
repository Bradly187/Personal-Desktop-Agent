# Project Structure

## Top-Level Layout

```
project/
├── mcp_server/               # MCP server — exposes desktop control to Claude
│   ├── desktop_mcp_server.py # Entry point (stdio MCP server, 14 tools)
│   └── tools/
│       ├── mouse.py          # move, click, double_click, scroll, drag
│       ├── keyboard.py       # type, hotkey, press, paste (unicode via clipboard), key_down, key_up
│       ├── screen.py         # screenshot (base64 PNG), get_screen_size, find_text_on_screen (OCR)
│       ├── windows.py        # get_active_window, list_windows, focus_window
│       └── handwriting.py    # recognize_math (pix2tex LaTeX OCR, GPU), latex_to_unicode
├── requirements.txt          # Pinned Python dependencies
├── main.py                   # Unified entry point (--full, --measure-vram, --safe-mode)
├── hybrid_coordinator.py     # 4-gate routing engine (local vs cloud)
├── whisper_stream.py         # Audio from iPad mic → Silero VAD → Whisper → Command
├── gesture_processor.py       # iPad camera frames → MediaPipe Hands → Command
├── command_executor.py        # Command → mouse/keyboard execution
├── continuous_trainer.py     # Background learning (thresholds, vocab, few-shot)
├── iPadApp/                  # Native Swift/SwiftUI Xcode project (iPadOS 17+)
│   ├── Audio/                # SharedAudioSession — shared AVAudioEngine for all 3 audio sensors
│   ├── Sensors/              # TiltSensor, GazeTracker, HeadTracker, SharedFaceSession,
│   │                         #   KeywordListener, SoundDetector, AudioStreamer
│   ├── UI/                   # CommandPadView, TrackpadView, ScientificKeypadView, HandwritingCanvasView,
│   │                         #   ScreenshotOverlayView, SettingsView
│   ├── DesignSystem/         # DesignTokens, AppTheme, Components/ (DAButton, DACard, DAConnectionBanner, DASectionHeader)
│   ├── Network/              # WebSocketManager, ServiceDiscovery (NWBrowser mDNS)
│   ├── SensorManager.swift   # Lifecycle hub: starts/stops all 7 sensors, owns SharedAudioSession + SharedFaceSession
│   ├── ScreenshotStore.swift # Decodes screenshot messages, publishes to UI
│   └── SettingsStore.swift   # UserDefaults persistence for all sensor preferences
├── ipad_bridge.py            # WebSocket server :8765, receives all iPad sensor streams
│                             # dispatches to FusionEngine/WhisperStream/GestureProcessor
│                             # and direct-to-pyautogui for trackpad/tilt/head events
│                             # Also serves web_client/ as iPad Safari fallback
├── web_client/               # Static HTML/JS client served by ipad_bridge (Safari fallback)
├── .kiro/
│   ├── steering/             # AI assistant guidance (this directory)
│   └── specs/                # Active feature specs
└── kiro/
    └── specs/                # Reference specs and diagrams
```

## Architectural Layers

```
Input        → iPad (TrueDepth/LiDAR/mic/camera/touch/tilt/gaze/sound)
Processing   → Whisper (CUDA) | YOLOv8 (CUDA) | MediaPipe (CPU) | iPad on-device
Intelligence → Claude via MCP (Ollama local / AWS Bedrock fallback)
Coordinator  → HybridCoordinator (4-gate routing)
Execution    → MCP Server (desktop_mcp_server.py) → pyautogui / Win32 API
Learning     → ContinuousTrainer (adapts while running)
```

The MCP server is the canonical execution interface. Claude calls its tools; nothing else writes to pyautogui directly in the production path.

## Key Data Flow

Every pipeline produces a `Command` dataclass. Nothing else crosses pipeline boundaries.

```python
@dataclass
class Command:
    text: str                    # Natural language action text
    whisper_logprob: float       # Transcription confidence (or 0.0)
    gesture_confidence: float    # Gesture confidence (or 1.0)
    source: str                  # "touch" | "sound_action" | "gaze_dwell" | "multimodal" |
                                 # "tilt" | "head_track" | "gesture" | "voice_local" | "voice"
    session_context: list[str]   # Last 20 successful commands
    _gaze_coords: tuple | None   # Screen (x, y) when gaze active
```

## Sensor Priority (FusionEngine)

1. iPad touch command → immediate, bypasses LLM
2. Sound action → mapped mouth sounds
3. Gaze dwell click → resting gaze triggers click
4. Gaze + voice "click" → click at gaze pixel
5. Gaze + gesture POINT → click at gaze pixel
6. Tilt navigation → cursor movement from iPad tilt
7. Head tracking → coarse cursor from head pose
8. Gesture alone → gesture command
9. On-device voice keyword → local Speech framework match
10. PC-transcribed voice → full Whisper pipeline

## Adding a New Sensor

1. Create a class with `start()`, `stop()`, and a property exposing its data type
2. Add a graceful `ImportError` handler in `start()` — system must not crash if absent
3. Expose data as one of: `GazePoint`, `HandFrame`, `RGBDFrame`, or `Command`
4. Register in `FusionEngine.tick()` at the appropriate priority level
5. Document hardware cost and `pip install` in the class docstring
6. **If the sensor uses ARKit face tracking**: register as a consumer on `SharedFaceSession` instead of creating a new `ARSession`. ARKit only supports one face-tracking session per device — GazeTracker and HeadTracker already share via this pattern. Call `sharedFaceSession.addConsumer(id, handler:)` in `start()` and `removeConsumer(id)` in `stop()`.
   - **Threading**: The handler is called directly on the ARKit delegate thread (not main). Mark it `@Sendable`. If you need main-thread access, dispatch internally within your handler.
   - **Error recovery**: `SharedFaceSession` auto-recovers from ARSession errors (up to 3 attempts, 1s delay). Subscribe to `onError` if your sensor needs to update UI state on failure. Observe `isRunning` (now `@Published`) for session state changes.

## Persistent Files

All persistence goes through `db.py`. Legacy flat files are superseded by AgentDB.

| Store | Writer | Reader |
|-------|--------|--------|
| `agent.db` (SQLite / AgentDB) | All pipeline components | ContinuousTrainer, HybridCoordinator, ModelRouter |
| `analytics.duckdb` (AnalyticsDB) | BenchmarkModels | AnalyticsDB OLAP queries |

Legacy files (`routing_log.jsonl`, `hotwords.txt`, `gesture_calibration.json`, `few_shot_memory.db`) are migrated by `migrate.py` — delete after running once.
