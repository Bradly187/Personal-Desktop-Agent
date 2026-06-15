"""Generate architecture + DB ER Mermaid sources for the study deck."""
import pathlib

ov = pathlib.Path("docs/diagrams/overview"); ov.mkdir(parents=True, exist_ok=True)
db = pathlib.Path("docs/diagrams/db"); db.mkdir(parents=True, exist_ok=True)

files = {}

files[ov / "A1-architecture.mmd"] = r'''flowchart LR
    subgraph IPAD["iPad Pro — Swift / SwiftUI (sensor hub)"]
      direction TB
      S1["Core Motion<br/>tilt / tap"]
      S2["Speech<br/>keywords"]
      S3["Camera<br/>gesture frames"]
      S4["Touch UI<br/>command pad / trackpad"]
      S5["LiDAR<br/>depth"]
    end

    IPAD -->|"WebSocket :8765<br/>(pairing token)"| BR["ipad_bridge<br/>25 message types"]
    BR --> FE["FusionEngine<br/>60 Hz · 6-level priority"]
    MIC["Whisper large-v3<br/>(PC mic / iPad audio)"] --> FE
    FE --> HC["HybridCoordinator<br/>Gate 0-4 routing"]
    HC --> DC{"DomainClassifier"}
    DC -->|"command"| L8["llama3.1:8b<br/>verb-first"]
    DC -->|"code / math / vision / plan"| DA["DevAgent<br/>ModelRouter -> specialist LLM"]
    HC -. "escalate (10s breaker)" .-> CLOUD["Anthropic API<br/>claude-haiku-4-5 / opus-4-8"]
    L8 --> CE["CommandExecutor<br/>16 verbs"]
    DA --> CE
    CLOUD --> CE
    CE --> EX["pyautogui · Win32 · MCP tools"]
    EX --> DESK["Windows desktop"]
    CE -->|"status / ack / screenshot"| BR

    KERNEL["Agent kernel:<br/>Scheduler · ResourceGovernor · Supervisor<br/>MemoryManager · CircuitBreaker"] -.governs.- FE
    KERNEL -.governs.- HC
    KERNEL -.governs.- DA

    style IPAD fill:#e8f0fe,stroke:#2d6cdf
    style KERNEL fill:#f3e8ff,stroke:#7c3aed
    style CLOUD fill:#fff4e6,stroke:#d9730d
    style DESK fill:#e7f5ec,stroke:#2e7d46
'''

files[ov / "A2-gate-routing.mmd"] = r'''flowchart LR
    CMD(["Command from FusionEngine"])
    G0{"Gate 0 — Privacy<br/>sensitive pattern?"}
    BYP{"Bypass source?<br/>touch · voice-click"}
    G1{"Gate 1 — Confidence<br/>whisper_logprob >= -1.0<br/>gesture_conf >= 0.6<br/>(Budget: 20ms)"}
    RT["phonetic re-transcription<br/>(low-conf voice)"]
    DISC["discard silently<br/>(low-conf gesture)"]
    G2{"Gate 2 — Complexity<br/>tokens <= 40<br/>no chain keywords<br/>(Budget: 10ms)"}
    G3{"Gate 3 — VRAM<br/>free >= 8 GB<br/>(Budget: 0ms)"}
    G4{"Gate 4 — Latency SLO<br/>p50 <= domain budget<br/>(command 600 ms)<br/>(Budget: 5ms)"}
    LOCAL["Local inference<br/>llama3.1:8b / specialist"]
    CLOUD["Cloud — claude-haiku-4-5<br/>(10s timeout breaker)"]
    CLAR["CLARIFY — ask user<br/>(timeout / malformed)"]
    EXEC(["CommandExecutor"])
    LOG[("agent.db<br/>commands.gate_that_decided")]

    CMD --> G0
    G0 -->|"sensitive"| LOCAL
    G0 -->|"clean"| BYP
    BYP -->|"yes"| LOCAL
    BYP -->|"no"| G1
    G1 -->|"gesture low"| DISC
    G1 -->|"voice low"| RT --> G2
    G1 -->|"pass"| G2
    G2 -->|"too complex"| CLOUD
    G2 -->|"pass"| G3
    G3 -->|"low VRAM"| CLOUD
    G3 -->|"pass"| G4
    G4 -->|"over budget"| CLOUD
    G4 -->|"pass"| LOCAL
    CLOUD -->|"timeout"| CLAR
    LOCAL --> EXEC
    CLOUD --> EXEC
    CLAR --> EXEC
    EXEC --> LOG

    style G0 fill:#1a472a,color:#fff
    style LOCAL fill:#155263,color:#fff
    style CLOUD fill:#7b2d00,color:#fff
    style CLAR fill:#4a0000,color:#fff
    style LOG fill:#2d3561,color:#fff
'''

files[ov / "happy_path.mmd"] = r'''sequenceDiagram
    autonumber
    participant U as User
    participant I as iPad Pro
    participant F as FusionEngine (PC)
    participant L as llama3.1:8b (PC)
    participant W as Windows Desktop

    U->>I: Tilts device forward + says "Scroll"
    I->>F: WebSocket (60Hz): {tilt_y: 0.15, text: "scroll"}
    F->>F: Fuse signals, clear Gates 0-4
    F->>L: Request action intent
    L-->>F: Emit verb: [SCROLL] + direction: [DOWN]
    F->>W: PyAutoGUI: scroll(-500)
    W-->>U: Screen scrolls down (Sub-200ms latency)
'''

files[db / "D1-storage-overview.mmd"] = r'''flowchart TD
    PIPE["Hot-path pipeline<br/>(every routed command)"]
    TRAIN["ContinuousTrainer<br/>(reads every 5 min)"]
    ANA["SessionAnalyzer / dashboards"]
    SEC["MCP tools · approval gate"]
    SEM["SemanticMemory · RAG"]

    PIPE -->|"async writes (aiosqlite)"| ADB[("agent.db — SQLite<br/>42 tables · operational store")]
    TRAIN --> ADB
    ADB -. "ATTACH (sqlite ext, zero ETL)" .-> DDB[("analytics.duckdb — DuckDB<br/>3 benchmark tables · OLAP")]
    ANA --> DDB
    SEC -->|"append-only (WAL + triggers)"| AUD[("audit.db — SQLite<br/>tool calls · hash-chained")]
    SEM --> CHR[("ChromaDB (cosine)<br/>codebase · documents · behavioral_memory<br/>personal_kb (outside repo)")]

    style ADB fill:#e8f0fe,stroke:#2d6cdf
    style DDB fill:#fff4e6,stroke:#d9730d
    style AUD fill:#fde8e8,stroke:#c0392b
    style CHR fill:#e7f5ec,stroke:#2e7d46
'''

files[db / "D2-core-star-schema.mmd"] = r'''erDiagram
    sessions ||--o{ commands : "anchors"
    sessions ||--o| session_summaries : "summarized by"
    commands ||--o{ inferences : "triggers"
    commands ||--o{ sensor_events : "logs"
    commands ||--o{ agent_runs : "may launch"

    sessions {
        int id PK
        real started_at
        text mode
        text git_hash
    }
    commands {
        int id PK
        int session_id FK
        text source
        text action
        text route
        text gate_that_decided
        real latency_ms
        int success
        text trace_id
    }
    inferences {
        int id PK
        int command_id FK
        text model
        text domain
        real latency_ms
        text backend
    }
    sensor_events {
        int id PK
        int command_id FK
        text event_type
        real confidence
    }
    session_summaries {
        int id PK
        int session_id FK
        int total_commands
        real success_rate
        real cloud_escalation_rate
        real latency_p50_ms
    }
'''

files[db / "D3a-orchestration-queue.mmd"] = r'''erDiagram
    goal_queue }o--o| agent_runs : "claimed -> run"
    agent_runs ||--o{ agent_steps : "executes"

    goal_queue {
        int id PK
        text goal
        text status
        text idempotency_key UK
        real execute_at
        text recurrence
        int owner_pid
        int run_id FK
    }
    agent_runs {
        int id PK
        text goal
        text domain
        text status
        int step_count
    }
    agent_steps {
        int id PK
        int run_id FK
        int step_num
        text action
        int success
    }
'''

files[db / "D3b-orchestration-capabilities.mmd"] = r'''erDiagram
    agent_runs ||--o{ saga_compensations : "rolls back"
    agent_steps ||--o{ saga_compensations : "reverses"
    agent_runs ||--o{ dev_escalations : "escalates"
    agent_runs ||--o{ tool_calls : "invokes"
    agent_steps ||--o{ tool_calls : "invokes"

    agent_runs {
        int id PK
    }
    agent_steps {
        int id PK
        int run_id FK
    }
    saga_compensations {
        int id PK
        int run_id FK
        int step_id FK
        text compensation_action
        text status
        text triggered_by
    }
    dev_escalations {
        int id PK
        int run_id FK
        text reason
        text status
    }
    tool_calls {
        int id PK
        int run_id FK
        int step_id FK
        text tool_name
        text idempotency_key
        text status
    }
    event_rules {
        int id PK
        text topic_pattern
        text action_kind
        real cooldown_s
    }
    skill_invocations {
        int id PK
        text skill_id
        text tool_name
        int blocked
    }
'''

files[db / "D4-learning-adaptation.mmd"] = r'''erDiagram
    commands ||--o{ few_shot_examples : "becomes"
    commands ||--o{ few_shot_counterexamples : "becomes"
    commands ||--o{ gesture_samples : "records"

    few_shot_examples {
        int id PK
        int command_id FK
        text text
        text action
        text domain
        blob embedding
    }
    few_shot_counterexamples {
        int id PK
        int command_id FK
        text text
        text wrong_action
        text reason
    }
    gesture_samples {
        int id PK
        int command_id FK
        text gesture
        real confidence
        real lidar_depth_m
    }
    gesture_calibration {
        int id PK
        text gesture
        real confidence_floor
        real p10
    }
    gesture_velocity_samples {
        int id PK
        text gesture
        real velocity
        int pain_day
    }
    gesture_velocity_calibration {
        int id PK
        text gesture
        real velocity_floor
    }
    settings_versions {
        int id PK
        text component
        text key
        text new_value
        text changed_by
    }
    adaptation_log {
        int id PK
        text component
        real metric_before
        real metric_after
        int rolled_back
    }
    word_counts {
        text word PK
        int count
    }
    hotwords {
        text word PK
        real added
    }
'''

files[db / "D5-twin-voice.mmd"] = r'''erDiagram
    sessions ||--o{ twin_session_history : "history"
    sessions ||--o{ twin_pain_day_log : "pain-day"
    sessions ||--o{ voice_calibration : "samples"
    voice_calibration_sessions ||--o{ voice_pronunciations : "phrases"

    twin_session_history {
        int id PK
        int session_id FK
        text cmd_text
        text action
        int seq
    }
    twin_pain_day_log {
        int id PK
        int session_id FK
        real pain_day_score
        int pain_day_active
        real fail_ratio
    }
    voice_calibration {
        int id PK
        int session_id FK
        text phrase
        real rms_amplitude
        real avg_logprob
        int is_flare_day
    }
    voice_profile {
        int id PK
        real baseline_rms
        real vad_threshold
        real logprob_floor
    }
    voice_phrases {
        int id PK
        text phrase
        text category
    }
    voice_calibration_sessions {
        int id PK
        text condition
    }
    voice_pronunciations {
        int id PK
        int session_id FK
        text expected
        text heard
        int accepted
    }
    voice_profiles {
        int id PK
        text condition UK
        real vad_threshold
        real logprob_floor
    }
    sensor_rom {
        int id PK
        text sensor
        real max_value
        real comfortable_value
    }
    flare_profile {
        int id PK
        int voice_degrades
        real flare_vad_scale
        int manual_pain_day
    }
'''

files[db / "D6-telemetry-events.mmd"] = r'''erDiagram
    sessions ||--o{ sensor_telemetry : "1 Hz snapshot"
    sessions ||--o{ ambient_transcripts : "lecture mode"
    sessions ||--o{ ipad_logs : "forwarded logs"
    commands ||--o{ event_log : "emits"
    commands ||--o{ rate_limit_events : "throttled"

    sensor_telemetry {
        int id PK
        int session_id FK
        real tilt_rx
        int cursor_x
        int pain_day_active
        text active_source
    }
    ambient_transcripts {
        int id PK
        int session_id FK
        text text
        real logprob
    }
    ipad_logs {
        int id PK
        int session_id FK
        text level
        text subsystem
        text msg
    }
    event_log {
        int id PK
        text topic
        text trace_id
        text source
        text payload
    }
    event_consumers {
        int id PK
        text consumer_name UK
        text topic_pattern
        int last_event_id
    }
    rate_limit_events {
        int id PK
        text resource
        real wait_ms
        int was_dropped
    }
    rate_limit_config {
        text resource PK
        real max_rps
        int burst_capacity
    }
    tool_timeout_config {
        text tool_name PK
        int timeout_ms
    }
    tool_cache_config {
        text tool_name PK
        real ttl_s
    }
'''

for p, c in files.items():
    p.write_text(c, encoding="utf-8")
    print("wrote", p)
print("TOTAL", len(files))
