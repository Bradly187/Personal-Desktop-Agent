# Diagram 10 — NemoClaw Integration: System Architecture

End-to-end view showing how NemoClaw concepts (Gate 0 privacy routing and
`gate_that_decided` logging) slot into the full pipeline.
Green nodes = NemoClaw additions. Dark red = cloud paths. Blue = observability.

> **Note (2026-06-24):** the **local-tier model roster below is historical** —
> `NemotronInference`/`nemotron-mini` was removed and `llama3.1:70b` does not fit
> alongside Whisper. See the "VRAM model roster" gotcha in `CLAUDE.md` for the
> current models (command: `llama3.1:8b`; specialists: `qwen3-coder:30b`,
> `deepseek-r1:8b`, `qwen3-vl:30b`, `gemma3:27b`). Operational writes go to
> `agent.db`, not `routing_log.jsonl`.

```mermaid
graph TD
    subgraph iPad["iPad Pro — Sensor Hub"]
        TiltSensor
        KeywordListener
        CameraStreamer
        CommandPadView
        TrackpadView
    end

    subgraph Bridge["PC — ipad_bridge.py :8765"]
        WS["WebSocket Server\naiohttp"]
        FE["FusionEngine\n60 Hz tick · 6-priority rules"]
    end

    subgraph Coordinator["HybridCoordinator"]
        G0["Gate 0 — Privacy\nNemoClaw concept\nforce-local if sensitive data"]
        G1["Gate 1 — Confidence\nwhisper_logprob · gesture_conf"]
        G2["Gate 2 — Complexity\ntokens ≤ 40 · no chain keywords"]
        G3["Gate 3 — VRAM\nfree ≥ 8 GB · pynvml"]
        G4["Gate 4 — Latency EMA\n≤ 600 ms"]
    end

    subgraph LocalTier["Local Inference Tier"]
        NI["NemotronInference\nnemotron-mini 4B\n~4 GB VRAM · fastest"]
        OI["OllamaInference\nLlama 3.1 70B\n~24 GB VRAM"]
        VI["VLLMInference\nLlama 3.1 70B\nvLLM engine · ~280 ms"]
    end

    subgraph Cloud["Cloud Fallback"]
        Bedrock["AWS Bedrock\nclaude-3-5-haiku"]
        Transcribe["Amazon Transcribe\nGate 1 voice fallback"]
    end

    subgraph Execution["Execution Layer"]
        CE["CommandExecutor\n16-verb vocabulary"]
        MCP["MCP Server\ndesktop_mcp_server.py"]
        PY["pyautogui · Win32"]
    end

    subgraph Log["Observability"]
        RL["routing_log.jsonl\ngate_that_decided field"]
    end

    iPad -->|"WebSocket\n26 iPad→PC message types"| WS
    WS --> FE
    FE -->|"Command DTO"| G0
    G0 -->|"sensitive → force local"| NI
    G0 -->|"bypass sources"| NI
    G0 --> G1
    G1 -->|"gesture fail → discard"| RL
    G1 -->|"voice fail"| Transcribe
    Transcribe --> G2
    G1 --> G2
    G2 -->|"complex"| Bedrock
    G2 --> G3
    G3 -->|"low VRAM"| Bedrock
    G3 --> G4
    G4 -->|"high latency"| Bedrock
    G4 --> NI

    NI --> CE
    OI --> CE
    VI --> CE
    Bedrock --> CE
    CE --> MCP
    MCP --> PY

    Coordinator --> RL

    style G0 fill:#1a472a,color:#fff
    style NI fill:#1a472a,color:#fff
    style RL fill:#2d3561,color:#fff
    style Bedrock fill:#7b2d00,color:#fff
```
