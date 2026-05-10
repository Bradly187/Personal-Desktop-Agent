# Diagram 12 — NemoClaw Integration: Local Inference Backend Tiers

Shows the three local inference backends mapped against the RTX 5090 VRAM budget.
All three implement `LocalInference` ABC — swapping is one constructor argument.
`NemotronInference` (green, Tier 1) is the NemoClaw addition; with nemotron-mini (4B)
the VRAM floor drops to ~7.4 GB total, effectively eliminating Gate 3 failures.

```mermaid
graph LR
    subgraph Tier1["Tier 1 — Fast · Small"]
        NM["NemotronInference\nnemotron-mini 4B\n~4 GB VRAM\np95 target: <200 ms\nOllama HTTP API\nNew — NemoClaw"]
    end

    subgraph Tier2["Tier 2 — Balanced"]
        OI["OllamaInference\nLlama 3.1 70B\n~24 GB VRAM\np95 target: ~450 ms\nOllama HTTP API\nPhase 1 default"]
        VI["VLLMInference\nLlama 3.1 70B\n~24 GB VRAM\np95 target: ~280 ms\nvLLM AsyncLLMEngine\nTask 2.13 candidate"]
    end

    subgraph Tier3["Tier 3 — Stretch Goal (Task N.6)"]
        N70["NemotronInference\nnemotron 70B\n32 GB VRAM + RAM offload\n192 GB RAM on this machine\np95 target: TBD"]
    end

    subgraph Cloud["Cloud Fallback"]
        BD["AWS Bedrock\nclaude-3-5-haiku\nno VRAM cost\n~800-1200 ms round-trip"]
    end

    subgraph VRAM["RTX 5090 VRAM Budget — 32 GB total"]
        V1["nemotron-mini 4B · ~4 GB"]
        V2["Whisper large-v3 · ~3 GB"]
        V3["YOLOv8-pose · ~0.4 GB"]
        V4["Free headroom · ~24.6 GB"]
    end

    HC["HybridCoordinator\nselects backend\nvia LocalInference ABC"]

    HC -->|"Gate 0 force-local\nor all gates pass"| NM
    HC -->|"Gate 2/3/4 fail"| BD
    HC -.->|"swap backend"| OI
    HC -.->|"swap backend"| VI
    HC -.->|"stretch goal"| N70

    NM -.->|"4 GB used"| V1
    V1 --- V2
    V2 --- V3
    V3 --- V4

    style NM fill:#1a472a,color:#fff
    style Tier1 fill:#0d2b1a,color:#fff
    style BD fill:#7b2d00,color:#fff
    style Cloud fill:#3d1000,color:#fff
    style VRAM fill:#1a1a2e,color:#fff
    style V4 fill:#155263,color:#fff
```

## Backend Selection Guide

| Situation | Recommended backend | Why |
|---|---|---|
| Default / benchmarking | `NemotronInference("nemotron-mini")` | 4B model, ~4 GB VRAM, Gate 3 never fires |
| Quality concerns on edge cases | `OllamaInference("llama3.1:70b")` | Larger model, more reasoning depth |
| Lowest latency needed | `VLLMInference(...)` | vLLM engine optimised for throughput |
| 32 GB VRAM fully available | `NemotronInference("nemotron")` | 70B Nemotron, RAM offload path |

## Swapping backends

```python
# In ipad_bridge.py or wherever HybridCoordinator is constructed:
from local_inference import NemotronInference, OllamaInference, VLLMInference

coordinator = HybridCoordinator(
    local=NemotronInference("nemotron-mini"),   # swap this line only
    config=CoordinatorConfig(),
)
```
