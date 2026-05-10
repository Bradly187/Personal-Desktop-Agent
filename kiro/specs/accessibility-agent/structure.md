# Project structure and conventions

## Architectural layers

```
┌─────────────────────────────────────────────────────────┐
│  Input layer       Mic │ Camera │ Ultraleap │ Tobii │ iPad touch  │
├─────────────────────────────────────────────────────────┤
│  Processing layer  Whisper (CUDA) │ YOLOv8 (CUDA) │ MediaPipe    │
├─────────────────────────────────────────────────────────┤
│  Intelligence      Ollama local LLM │ → AWS Bedrock fallback      │
├─────────────────────────────────────────────────────────┤
│  Coordinator       HybridCoordinator — 4-gate routing             │
├─────────────────────────────────────────────────────────┤
│  Execution         DesktopAgent — pyautogui + accessibility tree  │
├─────────────────────────────────────────────────────────┤
│  Learning          ContinuousTrainer — adapts while running       │
└─────────────────────────────────────────────────────────┘
```

## The Command dataclass

Every pipeline produces a `Command`. Every consumer reads a `Command`.
Nothing else crosses the pipeline boundary.

```python
@dataclass
class Command:
    text: str                    # Natural language action text
    whisper_logprob: float       # Transcription confidence (voice) or 0.0 (gesture/touch)
    gesture_confidence: float    # Gesture confidence (gesture) or 1.0 (voice/touch)
    source: str                  # "voice" | "gesture" | "touch" | "multimodal"
    session_context: list[str]   # Last 20 successful commands (coordinator-populated)
    _gaze_coords: tuple | None   # Screen (x, y) pixels when gaze targeting active
```

## Routing gates (HybridCoordinator)

A command runs locally unless it fails a gate. Gates are evaluated in order.

| Gate | Condition to escalate to cloud              | Configurable threshold         |
|------|---------------------------------------------|--------------------------------|
| 1    | Whisper logprob < min OR gesture conf < min | `whisper_logprob_min`, `gesture_confidence_min` |
| 2    | Token count > max OR complexity keyword     | `max_local_tokens`             |
| 3    | Free VRAM < minimum                         | `vram_free_min_gb`             |
| 4    | Latency EMA > budget                        | `latency_budget_ms`            |

## Action vocabulary

The LLM system prompt constrains output to exactly these verbs:

```
CLICK <target>
SCROLL <direction> [amount]
TYPE <text>
OPEN <application>
CLOSE <target>
HOTKEY <keys>
DICTATE <text>
CLARIFY <question>
```

## Continuous training schedule

| Pass           | Interval     | What it does                                      |
|----------------|--------------|---------------------------------------------------|
| Threshold      | 5 min        | Tunes Gates 1–4 based on outcome rates            |
| Vocabulary     | 30 min       | Rebuilds Whisper hotwords.txt from success log    |
| Gesture        | 5 min        | Updates per-gesture confidence floor from p10     |
| Few-shot       | Real-time    | Records (input → action) pairs in SQLite          |
| Compaction     | Daily 02:00  | Prunes stale few-shot examples                    |

## Sensor priority in FusionEngine

```
1. iPad touch command          → immediate, bypasses LLM
2. Gaze + voice "click"        → CLICK at gaze pixel
3. Gaze + Ultraleap POINT      → CLICK at gaze pixel
4. Ultraleap/Leap gesture      → gesture command
5. Voice alone                 → standard Whisper pipeline
```

## Adding a new sensor

1. Create a class with `start()`, `stop()`, and a property exposing its data type
2. Add a graceful `ImportError` handler in `start()` — the system must not crash if absent
3. Expose data as one of: `GazePoint`, `HandFrame`, `RGBDFrame`, or `Command`
4. Register in `FusionEngine.tick()` at the appropriate priority level
5. Document hardware cost and `pip install` in the class docstring
