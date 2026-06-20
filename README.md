# Personal Desktop Agent

Multimodal accessibility desktop control for hands-free and low-effort Windows operation, built for a single user with rheumatoid arthritis. An iPad is the sensor hub and primary touch surface; a Windows PC with an RTX 5090 runs all inference locally and executes desktop actions. Voice, hand gesture, iPad tilt, and direct touch are all mapped to a 16-verb action vocabulary.

The system adapts to the user's body: a pain-day engine fuses behavioural signals (voice clarity, gesture jitter, command failure rate) and automatically relaxes sensor thresholds, slows velocity floors, and sheds background load during flares.

## Hardware

- **PC**: Windows 10/11, NVIDIA RTX 5090 (32 GB VRAM), 32+ GB RAM
- **iPad**: any iPad with Core Motion and a microphone. A LiDAR-equipped iPad Pro additionally enables depth-validated hand gestures and the camera/depth debug feeds; everything degrades gracefully without it
- **Optional — Intel RealSense L515**: desk-mounted depth camera for camera-based hand-pointer control (experimental, in active bring-up)
- **Network**: PC and iPad on the same local network

## Quick Start

```bash
# 1. Install Python dependencies
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

# 2. Pull local LLM models (Ollama)
ollama pull llama3.1:8b          # command domain (required)
ollama pull gemma4:12b           # general domain
ollama pull qwen3-coder:30b      # optional: code + plan specialist
ollama pull deepseek-r1:8b       # optional: math/reasoning
ollama pull qwen3-vl:30b         # optional: vision grounding

# 3. Start the full pipeline (bridge + fusion + coordinator + trainer)
python main.py
#   or on Windows: start_agent.bat

# 4. Optional: MCP server for Claude desktop-control integration
python mcp_server/desktop_mcp_server.py
```

### MCP server registration (Claude Code / Claude Desktop)

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

The bridge listens on `0.0.0.0:8765` and advertises via mDNS; the iPad app discovers it automatically. Useful flags:

| Flag | Purpose |
|------|---------|
| `--safe-mode` | Block `keyboard_type` / `mouse_drag` during testing |
| `--viewer` | Desktop window with live iPad camera + LiDAR depth feeds |
| `--measure-vram` | Load all models, print a VRAM table, exit |
| `--dashboard` | Live TUI metrics dashboard |
| `--index-codebase` | Build the ChromaDB RAG index over source + docs |
| `--backend {ollama,vllm,vllm-server,llamacpp}` | Inference backend (default `ollama`) |
| `--cloud-dev-agent` | Route dev-domain queries to Claude Opus instead of local specialists |

### vLLM server backend (optional)

For lower command-domain latency, an OpenAI-compatible vLLM server can run in WSL2:

```bash
wsl bash scripts/start_vllm_server.sh     # serves on :8000
python main.py --backend vllm-server
```

See `scripts/start_vllm_server.sh` for model and VRAM settings and `scripts/bench_vllm_server.py` for the latency/accuracy benchmark used to tune them.

### Auto-start + crash recovery (recommended)

The agent is an accessibility dependency — if `main.py` crashes mid-day on an unattended machine, the desktop is uncontrollable until someone restarts it. Two native Task Scheduler tasks close that gap (no services, no NSSM):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\Install-AgentService.ps1            # register + start now
powershell -ExecutionPolicy Bypass -File scripts\Install-AgentService.ps1 -NoStart   # register only (starts at next logon)
powershell -ExecutionPolicy Bypass -File scripts\Install-AgentService.ps1 -Uninstall # remove both tasks
```

This registers (current user, at logon, interactive session — required for pyautogui):

| Task | Runs | Port |
|------|------|------|
| `PersonalDesktopAgent` | `main.py` via `scripts\agent_watchdog.ps1` | :8765 |
| `PersonalDesktopAgent-Proxy` | `windows_action_proxy.py` (WSL-mode action proxy) | :8768 |

The watchdog restarts its target on any non-zero exit with backoff (5 s / 10 s / 20 s), bounded at **3 crashes per rolling 10 minutes** — after that it gives up, logs loudly to `logs\watchdog_agent.log` / `logs\watchdog_proxy.log`, and stays down until `Start-ScheduledTask` or the next logon. Exit code 0 (graceful Ctrl-C/SIGTERM shutdown) is treated as intentional and is **not** restarted. Both watchdogs are no-ops if the target is already running (port/health check), so they coexist with `start_agent.bat` / `start_desktop.bat`.

**Crash notice:** `main.py` writes `logs\agent.running` at startup and removes it only after a graceful shutdown. If the marker is still there on the next start, the agent says *"I restarted after a crash"* over TTS so you know recovered state may apply (interrupted plans are reconciled and queued goals requeued automatically). Note that `Stop-ScheduledTask` kills the process tree hard — the next start will announce a crash restart; that's expected.

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

Every pipeline boundary carries a `Command` dataclass. The `HybridCoordinator` routes through 4 gates (privacy, cache, local, cloud) with circuit breakers on both the local and cloud paths; cloud fallback uses the Anthropic API (Haiku for commands, Opus for the dev agent). An `AccessibilityScheduler` gives accessibility/voice/gesture traffic priority over dev-agent and background work, and a `ResourceGovernor` evicts heavy specialist models from VRAM during pain flares.

## Sensor Priority (FusionEngine)

1. iPad touch command — bypasses LLM entirely
2. Voice "click" keyword — clicks at the current cursor position
3. Tilt navigation (Core Motion) — absolute position or velocity mode
4. Hand gesture (MediaPipe, peace-sign base pose, 13-gesture vocabulary)
5. On-device voice keyword (Speech framework)
6. PC-transcribed voice (Silero VAD + faster-whisper large-v3 on GPU)

Cursor gravity (magnetic snap toward clickable UI targets) assists levels 2–3, backed by a UIAutomation target cache.

## Action Vocabulary

**Accessibility verbs (11):** `CLICK` `MOUSEDOWN` `MOUSEUP` `SCROLL` `TYPE` `OPEN` `CLOSE` `HOTKEY` `DICTATE` `CLARIFY` `SCREENSHOT`

**Dev-agent verbs (5):** `WRITE_FILE` `RUN_TERMINAL` `EXPLAIN` `SEARCH_WEB` `READ_SCREEN`

`CLICK` resolution falls back through UIAutomation tree search → local vision grounding (qwen3-vl) → OCR → clarify. An `ActionVerifier` perceptually diffs before/after screenshots to confirm actions landed.

## Project Structure

```
├── main.py                  # Unified entry point
├── core/                    # ipad_bridge, fusion_engine, hybrid_coordinator,
│                            #   scheduler, supervisor, resource_governor,
│                            #   goal_session, circuit_breaker
├── sensors/                 # whisper_stream, gesture_processor, lidar_receiver,
│                            #   one_euro_filter, sensor_viewer, realsense_*
├── inference/               # local_inference (Ollama/vLLM/llama.cpp backends),
│                            #   model_router, dev_agent, codebase_indexer, sandbox
├── adaptive/                # continuous_trainer, behavioral_twin_state,
│                            #   mcp_trust_classifier, content_filter
├── storage/                 # db (AgentDB, 38 tables + DuckDB analytics),
│                            #   memory_manager, semantic_memory, audit_log
├── desktop/                 # ui_automation, vision_grounder, action_verifier,
│                            #   target_cache, flick_engine
├── calibration/             # acoustic_profiler, voice_calibrator, gyro_bias
├── tts/ + tts_service/      # Polly streaming sidecar (Node) + local Chatterbox GPU TTS
├── monitoring/              # metrics, tracing (DA_TRACE), benchmark_models
├── mcp_server/              # MCP stdio server + tool modules (mouse, keyboard,
│                            #   screen, windows, handwriting)
├── iPadApp/                 # Native SwiftUI app (40 Swift sources, 15 test files)
├── scripts/                 # vLLM server, benchmarks, RealSense validation
└── tests/                   # pytest suite (1200+ tests)
```

## iPad App

Native Swift/SwiftUI app. Five tabs — **Commands**, **Trackpad**, **Write**, **Settings**, **Sensors** — with page-style swipe between the first three. The Write tab does handwriting recognition in Math mode (pix2tex LaTeX OCR on the PC) or Text mode (on-device Vision framework).

- Core Motion tilt navigation with gyro-bias calibration and 1€ filtering
- Speech framework on-device keywords + continuous audio streaming to PC Whisper
- LiDAR depth + camera streaming for gesture validation (LiDAR iPads)
- Mic mute toggle with two-way state sync to the PC
- First-run onboarding wizard with skippable calibration steps (voice profiling, gesture assessment, flare profile)
- Structured log forwarding to the PC (`ipad_logs` table)

Push to `iPadApp/` → GitHub Actions builds and uploads to TestFlight. See `.github/SIGNING_SETUP.md`.

## Security & Approval

- **Voice approval gate** — destructive tool calls are spoken aloud (Polly TTS) and require an explicit verbal yes/no; silence and ambiguity fail safe to deny
- **Goal sessions** — authorize a high-level goal once by voice and its constituent steps run silently, scoped by a deny-by-default Bash allowlist
- **Sandbox** — `RUN_TERMINAL` executes inside a WSL2 bubblewrap jail when available
- **Audit log** — append-only `audit.db` (UPDATE/DELETE blocked by triggers) records every MCP tool invocation and security event
- **MCP trust classifier** — taint analysis on tool outputs for injection patterns before they re-enter LLM context
- **Content filter** — secrets/PII redaction before any text leaves for a cloud API; Gate 0 blocks privacy-sensitive prompts from the cloud path entirely

## Health-Aware Design

Built for rheumatoid arthritis first: large touch targets, minimal fine-motor input, and a `PainDayEngine` that detects flare days from passive signals and relaxes the whole sensor stack in response (thresholds, gesture velocity floors, Whisper VAD, VRAM pressure). Voice calibration profiles cover distinct conditions (good day / flare / allergy). Reliability is always preferred over speed.

## License

Apache 2.0 — see [LICENSE.txt](LICENSE.txt).
