# Diagram 11 — NemoClaw Integration: Gate Decision Flow

Detailed flowchart of every routing decision in `HybridCoordinator`.
Gate 0 (green) is the NemoClaw privacy concept — a sensitive-data match
short-circuits all other gates and forces local inference.
Each terminal node shows the `gate_that_decided` string written to `routing_log.jsonl`.

```mermaid
flowchart TD
    CMD(["Command\nfrom FusionEngine"])

    G0{"Gate 0\nPrivacy Check\nNemoClaw concept"}
    G0_PASS["force local\ngate_that_decided:\ngate0_privacy"]

    BYPASS{"Source in\nbypass set?\ntouch · sound_action\ngaze_dwell · multimodal"}
    BYPASS_PASS["run local\ngate_that_decided:\nbypass"]

    SKIP1{"Source skips\nGate 1?\nvoice_local"}

    G1{"Gate 1\nConfidence\nwhisper_logprob ≥ -1.0\ngesture_conf ≥ 0.6"}
    G1_GESTURE["Discard silently\ngate_that_decided:\ndiscard"]
    G1_VOICE["Amazon Transcribe\nre-transcription stub"]

    G2{"Gate 2\nComplexity\ntokens ≤ 40\nno chain keywords"}
    G2_FAIL["→ Cloud\ngate_that_decided:\ngate2_complexity"]

    G3{"Gate 3\nVRAM\nfree ≥ 8.0 GB\npynvml"}
    G3_FAIL["→ Cloud\ngate_that_decided:\ngate3_vram"]

    G4{"Gate 4\nLatency EMA\n≤ 600 ms"}
    G4_FAIL["→ Cloud\ngate_that_decided:\ngate4_latency"]

    LOCAL["→ Local Inference\ngate_that_decided:\nall_pass"]

    CLOUD(["AWS Bedrock\nclaude-3-5-haiku"])
    EXEC(["CommandExecutor\n→ pyautogui / Win32"])
    LOGFILE[("routing_log.jsonl")]

    CMD --> G0
    G0 -->|"sensitive pattern\nmatched"| G0_PASS
    G0 -->|"clean"| BYPASS
    G0_PASS --> EXEC

    BYPASS -->|"yes"| BYPASS_PASS
    BYPASS -->|"no"| SKIP1
    BYPASS_PASS --> EXEC

    SKIP1 -->|"yes"| G2
    SKIP1 -->|"no"| G1

    G1 -->|"gesture low conf"| G1_GESTURE
    G1 -->|"voice low conf"| G1_VOICE
    G1 -->|"pass"| G2
    G1_VOICE --> G2

    G2 -->|"fail"| G2_FAIL
    G2 -->|"pass"| G3
    G2_FAIL --> CLOUD

    G3 -->|"fail"| G3_FAIL
    G3 -->|"pass"| G4
    G3_FAIL --> CLOUD

    G4 -->|"fail"| G4_FAIL
    G4 -->|"pass"| LOCAL
    G4_FAIL --> CLOUD

    LOCAL --> EXEC
    CLOUD --> EXEC
    EXEC --> LOGFILE

    style G0 fill:#1a472a,color:#fff
    style G0_PASS fill:#1a472a,color:#fff
    style BYPASS_PASS fill:#155263,color:#fff
    style LOCAL fill:#155263,color:#fff
    style CLOUD fill:#7b2d00,color:#fff
    style G2_FAIL fill:#7b2d00,color:#fff
    style G3_FAIL fill:#7b2d00,color:#fff
    style G4_FAIL fill:#7b2d00,color:#fff
    style G1_GESTURE fill:#4a0000,color:#fff
    style LOGFILE fill:#2d3561,color:#fff
```
