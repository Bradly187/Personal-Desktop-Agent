# Diagram 10 — NemoClaw Integration: System Architecture

End-to-end view showing how NemoClaw concepts (Gate 0 privacy routing,
`NemotronInference`, and `gate_that_decided` logging) slot into the full pipeline.
Green nodes = NemoClaw additions. Dark red = cloud paths. Blue = observability.

```mermaid
graph TD
    subgraph iPad["iPad Pro — Sensor Hub"]
        TiltSensor
        GazeTracker
        HeadTracker
        KeywordListener
        SoundDetector
        CommandPadView
        TrackpadView
    end

    subgraph Bridge["PC — ipad_bridge.py :8765"]
        WS["WebSocket Server\naiohttp"]
        FE["FusionEngine\n60 Hz tick · 10-priority rules"]
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
        CE["CommandExecutor\n9-verb vocabulary"]
        MCP["MCP Server\ndesktop_mcp_server.py"]
        PY["pyautogui · Win32"]
    end

    subgraph Log["Observability"]
        RL["routing_log.jsonl\ngate_that_decided field"]
    end

    iPad -->|"WebSocket\n12 message types"| WS
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
