# Tech Stack

## Language and Runtime

- **PC-side**: Python 3.11+ with asyncio throughout. All pipelines are async tasks in a single event loop. Blocking operations (GPU inference, camera I/O) use `asyncio.to_thread`.
- **iPad-side**: Swift / SwiftUI, built in Xcode, targeting iPadOS 17+.

## Core Python Dependencies

| Package | Purpose |
|---------|---------|
| faster-whisper | Whisper large-v3 on CUDA (CTranslate2 backend) |
| ultralytics | YOLOv8 pose estimation |
| mediapipe | Hand landmarks, face mesh iris tracking |
| opencv-python | Camera capture, frame processing |
| sounddevice | Audio capture (mic stream) |
| torch | CUDA tensor ops, Silero VAD |
| pynvml | GPU VRAM monitoring |
| boto3 | AWS SDK (Bedrock, Transcribe, Lex, Lambda) |
| ollama | Local LLM inference — development backend (OllamaInference) |
| vllm | Local LLM inference — production target (~280ms vs ~450ms for Ollama); see `local-inference-comparison.md` |
| aiosqlite | Async SQLite for few-shot memory |
| aiohttp | iPad touch WebSocket server |
| pyautogui | Mouse / keyboard execution |
| psutil | Process management |
| mcp | MCP SDK — exposes desktop tools to Claude (stdio transport) |
| mss | Fast multi-monitor screenshot capture |
| Pillow | Image processing for screenshot encoding |

## iPad Frameworks

- Core Motion (accelerometer, gyroscope, tilt navigation)
- ARKit (eye gaze tracking, head pose via TrueDepth)
- Speech framework (on-device keyword recognition)
- AVFoundation (audio capture, sound action detection)

## AWS Services (fallback only)

- Amazon Bedrock (Claude for complex commands)
- Amazon Transcribe (speech-to-text when Whisper confidence low)
- Amazon Lex (structured intent recognition)
- AWS Lambda (orchestration, logging)
- Amazon Polly (TTS fallback)

## Inference Targets

| Model | Hardware | Latency | VRAM |
|-------|----------|---------|------|
| Whisper large-v3 | RTX 5090 | < 400 ms | ~3 GB |
| YOLOv8-pose | RTX 5090 | < 15 ms/frame | ~0.5 GB |
| MediaPipe hands | CPU | < 5 ms/frame | 0 |
| Ollama Llama 3.1 | RTX 5090 | < 600 ms | ~24 GB |
| EasyOCR | RTX 5090 | < 200 ms | ~1 GB |
| Silero VAD | CPU | < 1 ms/chunk | 0 |

## MCP Integration Layer

`mcp_server/desktop_mcp_server.py` is a standalone MCP server that Claude calls directly. It exposes all desktop actions as typed tools (mouse, keyboard, screenshot, window management) using the stdio transport, which Claude Code and Amazon Bedrock both support natively.

The MCP server is the **execution interface** between Claude's reasoning and the Windows desktop. The `HybridCoordinator` will eventually route multimodal inputs through Claude, which then calls MCP tools to act.

Register in Claude Code (`~/.claude/claude_desktop_config.json`):
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

Set `SAFE_MODE=1` to block `keyboard_type` and `mouse_drag` during testing.

## Build / Run Commands

No formal build system yet. Expected commands:

```bash
# Install Python dependencies
pip install -r requirements.txt

# Start the MCP server (for Claude Code integration)
python mcp_server/desktop_mcp_server.py

# Run the full pipeline
python main.py --full

# Run budget sensor stack
python budget_sensor_fusion.py

# Run iPad bridge
python ipad_bridge.py

# Run gaze calibration
python budget_sensor_fusion.py --calibrate

# iPad app — open in Xcode and build to device
```

## LocalInference Backend Pattern

`LocalInference` is an abstract base class. The coordinator holds a reference to the ABC, not a concrete implementation. This allows swapping backends without touching routing logic.

```python
class LocalInference(ABC):
    @abstractmethod
    async def infer(self, cmd: Command) -> str: ...
    @abstractmethod
    def get_status(self) -> dict: ...

class OllamaInference(LocalInference): ...   # Phase 1 — development
class VLLMInference(LocalInference): ...     # Phase 2 — production target
```

Benchmark task 2.13 in `tasks.md` determines which becomes the default.

## Coding Conventions

- All public async methods: `run()` for pipeline entry, `start()`/`stop()` for lifecycle
- `Command` dataclass is the universal DTO between all pipelines and the coordinator
- Every sensor class degrades gracefully — `ImportError` and connection failures log a warning, system continues
- No global state outside dataclass instances — all state lives in class attributes
- Log levels: DEBUG for per-frame data, INFO for commands/routing, WARNING for sensor failures, ERROR for unrecoverable issues
- Action vocabulary is constrained to: CLICK, MOUSEDOWN, MOUSEUP, SCROLL, TYPE, OPEN, CLOSE, HOTKEY, DICTATE, CLARIFY, SCREENSHOT
