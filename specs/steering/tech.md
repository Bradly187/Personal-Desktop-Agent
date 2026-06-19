# Tech Stack

## Language and Runtime

- **PC-side**: Python 3.11+ with asyncio throughout. All pipelines are async tasks in a single event loop. Blocking operations (GPU inference, camera I/O) use `asyncio.to_thread`.
- **iPad-side**: Swift / SwiftUI, built in Xcode, targeting iPadOS 17+.

## Core Python Dependencies

| Package | Purpose |
|---------|---------|
| faster-whisper | Whisper large-v3 on CUDA (CTranslate2 backend) |
| ultralytics | YOLOv8 pose estimation |
| mediapipe | Hand landmarks via Tasks API (`HandLandmarker`, `hand_landmarker.task`); `mp.solutions` was removed in 0.10.x |
| opencv-python | Camera capture, frame processing |
| sounddevice | Audio capture (mic stream) |
| torch | CUDA tensor ops, Silero VAD |
| pynvml | GPU VRAM monitoring |
| boto3 | AWS SDK (Bedrock, Transcribe, Lex, Lambda) |
| ollama | Local LLM inference — default backend (`OllamaInference`); llama3.1:8b warm wall p50 ~190ms / ~29ms compute (Ollama 0.30.6, RTX 5090) |
| vllm | Local LLM inference — production target; verified working in Ubuntu WSL2 (vLLM 0.21.0 + torch 2.11.0+cu128 on RTX 5090); activate with `--backend vllm` |
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
| Whisper large-v3 | RTX 5090 | < 400 ms | ~4.2 GB |
| MediaPipe HandLandmarker | CPU | < 5 ms/frame | 0 |
| Ollama llama3.1:8b (command) | RTX 5090 | ~190 ms warm wall p50 / ~29 ms compute (Ollama 0.30.6) | 4.6 GB |
| Ollama qwen3-coder:30b (code+plan) | RTX 5090 | — (thinking ON) | 17.3 GB |
| Ollama deepseek-r1:8b (math) | RTX 5090 | — | 4.9 GB |
| Ollama qwen3-vl:30b (vision) | RTX 5090 | ~0.4s warm | 18.2 GB |
| Ollama gemma3:27b (general) | RTX 5090 | — | 16.2 GB |
| EasyOCR | RTX 5090 | < 200 ms | ~1 GB |
| Silero VAD | CPU | < 1 ms/chunk | 0 |
| Chatterbox TTS | RTX 5090 | ~300 ms first token | ~2 GB |

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
python main.py [--port 8765] [--debug] [--safe-mode] [--viewer] [--kiro]

# Run with llama.cpp backend
python main.py --backend llamacpp

# Run iPad bridge only (without FusionEngine)
python -m core.ipad_bridge

# Measure VRAM usage of all models
python main.py --measure-vram

# Benchmark models
python monitoring/benchmark_models.py [--vllm <model-id>]

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

class OllamaInference(LocalInference): ...   # Default — llama3.1:8b command, ~190 ms warm wall p50 (Ollama 0.30.6)
class LlamaCppInference(LocalInference): ... # llama-server HTTP backend (--backend llamacpp)
class VLLMInference(LocalInference): ...     # Production target — verified in WSL2 (--backend vllm)
```

`OllamaInference` with `llama3.1:8b` is the current production default for the command domain (100% accuracy, ~190ms warm wall p50 / ~29ms compute on Ollama 0.30.6). Specialist domains (code/plan/math/vision/general) use `ModelRouter` which selects from qwen3-coder:30b, deepseek-r1:8b, qwen3-vl:30b, gemma3:27b based on VRAM. `NemotronInference` was removed (25% accuracy). `VLLMInference` is verified working in Ubuntu WSL2 with vLLM 0.21.0 + torch 2.11.0+cu128 — activate with `--backend vllm`; use `--gpu-memory-utilization 0.65` when Whisper is also loaded.

## Coding Conventions

- All public async methods: `run()` for pipeline entry, `start()`/`stop()` for lifecycle
- `Command` dataclass is the universal DTO between all pipelines and the coordinator
- Every sensor class degrades gracefully — `ImportError` and connection failures log a warning, system continues
- No global state outside dataclass instances — all state lives in class attributes
- Log levels: DEBUG for per-frame data, INFO for commands/routing, WARNING for sensor failures, ERROR for unrecoverable issues
- Action vocabulary: **Accessibility (11):** CLICK, MOUSEDOWN, MOUSEUP, SCROLL, TYPE, OPEN, CLOSE, HOTKEY, DICTATE, CLARIFY, SCREENSHOT — **Dev-agent (5):** WRITE_FILE, RUN_TERMINAL, EXPLAIN, SEARCH_WEB, READ_SCREEN — **Plan verbs (6, DevAgent only):** GIT_STATUS, GIT_DIFF, GIT_COMMIT, GIT_CHECKOUT, GITHUB_PR, FETCH_URL
