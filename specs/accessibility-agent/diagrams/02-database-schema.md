# Database & Persistent Storage Schema

## Overview

The system uses three persistent stores:
- **`few_shot_memory.db`** — SQLite, async (aiosqlite), stores successful command→action pairs for LLM augmentation
- **`routing_log.jsonl`** — append-only JSONL, every routing decision logged for threshold tuning
- **`gesture_calibration.json`** — JSON snapshot of per-gesture confidence statistics
- **`hotwords.txt`** — plain text, one word per line, fed to Whisper transcriber

---

## 1. few_shot_memory.db (SQLite)

```mermaid
erDiagram
    FEW_SHOT_EXAMPLES {
        INTEGER id PK "AUTOINCREMENT"
        TEXT    command_text    "Natural language input (e.g. 'open chrome')"
        TEXT    action_text     "LLM action output (e.g. 'OPEN chrome')"
        TEXT    source          "voice | gesture | touch | multimodal"
        TEXT    token_key       "Space-joined sorted tokens — fast overlap lookup"
        INTEGER usage_count     "Times retrieved and used successfully"
        REAL    avg_logprob     "Whisper logprob at recording time (voice only)"
        REAL    gesture_conf    "Gesture confidence at recording time (gesture only)"
        TEXT    created_at      "ISO-8601 timestamp"
        TEXT    last_used_at    "ISO-8601 timestamp — updated on each retrieve"
        INTEGER is_stale        "1 if pruned by nightly compaction, 0 otherwise"
    }

    GESTURE_SAMPLES {
        INTEGER id PK "AUTOINCREMENT"
        TEXT    gesture_name    "e.g. POINT, PINCH, SWIPE_DOWN"
        REAL    confidence      "Raw classifier confidence at event time"
        TEXT    recorded_at     "ISO-8601 timestamp"
        TEXT    outcome         "success | discarded"
    }
```

### Retrieval logic (FewShotMemory.retrieve)

```
SELECT * FROM few_shot_examples
WHERE is_stale = 0
ORDER BY
  -- token overlap score (computed in Python, then ranked here)
  usage_count DESC,
  last_used_at DESC
LIMIT k
```

Ranking formula applied in Python:
```
score = token_overlap(query_tokens, example.token_key)
      * log(1 + example.usage_count)
      * recency_decay(example.last_used_at, half_life=7_days)
```

---

## 2. routing_log.jsonl (JSONL schema)

Each line is a single JSON object with the following fields:

```mermaid
erDiagram
    ROUTING_LOG_ENTRY {
        STRING  ts              "ISO-8601 timestamp"
        STRING  command_text    "Verbatim text from Command.text"
        STRING  source          "voice | gesture | touch | multimodal"
        FLOAT   whisper_logprob "From Command.whisper_logprob"
        FLOAT   gesture_conf    "From Command.gesture_confidence"
        INT     gate_reached    "0=touch bypass, 1-4=first gate that fired"
        STRING  routed_to       "local | cloud"
        STRING  service         "ollama | bedrock | transcribe | none"
        STRING  action          "Action string returned by inference"
        STRING  outcome         "success | failure | clarify | discarded"
        FLOAT   latency_ms      "End-to-end routing+inference time"
        FLOAT   vram_free_gb    "GPU VRAM headroom at routing time"
        FLOAT   latency_ema_ms  "EMA value at routing time"
        INT     token_count     "Token count of command_text"
        BOOL    had_complexity  "True if multi-step language detected"
    }
```

### Example record
```json
{
  "ts": "2025-01-15T09:32:14.221Z",
  "command_text": "open chrome",
  "source": "voice",
  "whisper_logprob": -0.12,
  "gesture_conf": 1.0,
  "gate_reached": 0,
  "routed_to": "local",
  "service": "ollama",
  "action": "OPEN chrome",
  "outcome": "success",
  "latency_ms": 310.4,
  "vram_free_gb": 3.8,
  "latency_ema_ms": 295.1,
  "token_count": 2,
  "had_complexity": false
}
```

---

## 3. gesture_calibration.json (JSON)

```mermaid
erDiagram
    GESTURE_CALIBRATION {
        OBJECT  gestures        "Map of gesture_name to CalibrationData"
    }

    CALIBRATION_DATA {
        FLOAT   confidence_floor    "p10(samples) - 0.05, lower bound"
        FLOAT   p10                 "10th percentile of observed confidences"
        FLOAT   p50                 "Median confidence"
        FLOAT   p90                 "90th percentile"
        INT     sample_count        "Total observations"
        STRING  last_updated        "ISO-8601 timestamp"
    }

    GESTURE_CALIBRATION ||--o{ CALIBRATION_DATA : contains
```

### Example structure
```json
{
  "gestures": {
    "POINT": {
      "confidence_floor": 0.62,
      "p10": 0.67,
      "p50": 0.81,
      "p90": 0.93,
      "sample_count": 47,
      "last_updated": "2025-01-15T09:30:00Z"
    },
    "PINCH": {
      "confidence_floor": 0.58,
      "p10": 0.63,
      "p50": 0.75,
      "p90": 0.89,
      "sample_count": 23,
      "last_updated": "2025-01-15T08:15:00Z"
    }
  }
}
```

---

## 4. hotwords.txt (plain text)

One word or phrase per line. Written by `VocabularyBuilder` every 30 minutes.
Read by `WhisperTranscriber` at startup and on reload.

```
chrome
vscode
scroll
click
close
zoom
slack
open
```

---

## Data lifecycle diagram

```mermaid
flowchart TD
    A[Command executed\noutcome=success] --> B[OutcomeLogger\nrouting_log.jsonl]
    A --> C[FewShotMemory\nfew_shot_memory.db]

    B --> D[ThresholdTuner\nevery 5 min]
    B --> E[VocabularyBuilder\nevery 30 min]
    B --> F[GestureCalibrator\nevery 5 min]

    D -->|update_thresholds| G[HybridCoordinator\nThresholds]
    E -->|write| H[hotwords.txt]
    F -->|write| I[gesture_calibration.json]

    H -->|reload| J[WhisperTranscriber]
    I -->|read at startup| K[GestureDebouncer]

    C -->|retrieve k examples| L[PromptAugmenter]
    L -->|prepend few-shot| M[LocalInference\nOllama]

    N[Nightly 02:00\nCompaction] -->|mark is_stale=1| C
```
