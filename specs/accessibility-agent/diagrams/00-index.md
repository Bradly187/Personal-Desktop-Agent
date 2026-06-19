# Accessibility Desktop Agent — Diagram Index

All diagrams use [Mermaid](https://mermaid.js.org/) syntax and render natively in:
- GitHub / GitLab markdown
- VS Code with the Mermaid Preview extension
- JetBrains IDEs with the Mermaid plugin
- Obsidian, Notion, and most modern wikis

---

## Files

| # | File | Contents |
|---|------|----------|
| 01 | [01-class-diagram.md](01-class-diagram.md) | Full class diagram — all modules, classes, relationships, and the Command DTO |
| 02 | [02-database-schema.md](02-database-schema.md) | ER diagrams for `few_shot_memory.db`, `routing_log.jsonl`, `gesture_calibration.json`, `hotwords.txt` |
| 03 | [03-sequence-diagrams.md](03-sequence-diagrams.md) | 7 sequence diagrams covering all major interaction flows |
| 04 | [04-component-deployment.md](04-component-deployment.md) | C4 component/deployment diagram, network topology |
| 05 | [05-state-machines.md](05-state-machines.md) | State machines for UtteranceSegmenter, HybridCoordinator routing, TouchInputServer, sensor degradation, GestureDebouncer |
| 06 | [06-routing-flowchart.md](06-routing-flowchart.md) | Routing decision flowchart, FusionEngine priority evaluation, continuous trainer adaptation tree, ElementFinder resolution strategy |
| 07 | [07-hardware-sensor-matrix.md](07-hardware-sensor-matrix.md) | Sensor fallback chain, VRAM budget pie chart, latency Gantt chart, iPad data flow |

---

## Quick Reference

### Core data type
```python
@dataclass
class Command:
    text: str
    whisper_logprob: float   # 0.0 for gesture/touch
    gesture_confidence: float  # 1.0 for voice/touch
    source: str              # voice | gesture | touch | multimodal
    session_context: list[str]
    _gaze_coords: tuple | None
```

### Routing gate order
```
Gate 1 → confidence (logprob / gesture_conf)
Gate 2 → complexity (token count / multi-step keywords)
Gate 3 → VRAM headroom
Gate 4 → latency EMA
```

### Sensor priority (FusionEngine)
```
1. iPad touch      → bypass LLM
2. Gaze + "click"  → direct click at gaze pixel
3. Gaze + POINT    → direct click at gaze pixel
4. Gesture alone   → standard routing
5. Voice alone     → standard routing
```

### Action vocabulary
```
CLICK <target>      SCROLL <dir> [n]   TYPE <text>
OPEN <app>          CLOSE <target>     HOTKEY <keys>
DICTATE <text>      CLARIFY <question>
```
