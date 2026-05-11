# iPad-Focused Accessibility Agent — Diagram Index

All diagrams use [Mermaid](https://mermaid.js.org/) syntax and reflect the iPad-only
architecture where a native Swift/SwiftUI app replaces all standalone sensor hardware.

---

## Files

| # | File | Contents |
|---|------|----------|
| 01 | [01-system-architecture.md](01-system-architecture.md) | High-level system architecture, iPad↔PC split, WebSocket protocol |
| 02 | [02-class-diagram.md](02-class-diagram.md) | Class diagram for both iPad-side (Swift) and PC-side (Python) |
| 03 | [03-sequence-diagrams.md](03-sequence-diagrams.md) | Interaction flows for all 10 input modalities |
| 04 | [04-state-machines.md](04-state-machines.md) | State machines for iPad app, sensor modes, fusion engine, debouncing |
| 05 | [05-data-flow.md](05-data-flow.md) | iPad sensor data flows, WebSocket message schema, persistent storage |
| 06 | [06-fusion-routing.md](06-fusion-routing.md) | 10-level fusion priority, 4-gate routing, action execution |
| 07 | [07-bridge-architecture.md](07-bridge-architecture.md) | iPad↔Bridge↔MCP↔pyautogui stack overview |
| 08 | [08-bridge-message-routing.md](08-bridge-message-routing.md) | Full message routing flowchart (13 types, 11 action verbs) |
| 09 | [09-bridge-sequence.md](09-bridge-sequence.md) | Sequence diagram: touch_command and trackpad end-to-end |
| 10 | [10-nemoclaw-system-architecture.md](10-nemoclaw-system-architecture.md) | Full pipeline with NemoClaw additions: Gate 0, NemotronInference, log field |
| 11 | [11-nemoclaw-gate-flow.md](11-nemoclaw-gate-flow.md) | HybridCoordinator gate decision flowchart with gate_that_decided labels |
| 12 | [12-nemoclaw-inference-tiers.md](12-nemoclaw-inference-tiers.md) | Local inference backends mapped against RTX 5090 VRAM budget |
| 14 | [14-database-schema.md](14-database-schema.md) | agent.db (11 tables) + analytics.duckdb ER diagrams; pipeline write topology; index coverage |

---

## Quick Reference

### System Split
```
iPad Pro (Swift/SwiftUI)          PC (Python 3.11 asyncio)
─────────────────────────         ──────────────────────────
Core Motion (tilt)         ──┐
ARKit (gaze, head pose)    ──┤    IPadBridge (receives streams)
Speech Framework (keywords)──┼──► FusionEngine (10-level priority)
AVFoundation (sound actions)─┤    HybridCoordinator (4-gate routing)
Touch UI (command pad)     ──┤    DesktopAgent (pyautogui)
Camera feed (gesture)      ──┘    ContinuousTrainer (learning)
                                  WhisperStream (GPU transcription)
```

### Sensor Priority (FusionEngine — 10 levels)
```
 1. iPad touch command       → immediate, bypasses LLM
 2. Sound action             → mapped mouth sounds
 3. Gaze dwell click         → resting gaze triggers click
 4. Gaze + voice "click"     → click at gaze pixel
 5. Gaze + gesture POINT     → click at gaze pixel
 6. Tilt navigation          → cursor movement from iPad tilt
 7. Head tracking            → coarse cursor from head pose
 8. Gesture alone            → gesture command
 9. On-device voice keyword  → local Speech framework match
10. PC-transcribed voice     → full Whisper pipeline
```

### WebSocket Message Format
```json
{"type": "<sensor_type>", "ts": <unix_ms>, "data": {...}}
```

### Action Vocabulary
```
CLICK <target>      SCROLL <dir> [n]   TYPE <text>
OPEN <app>          CLOSE <target>     HOTKEY <keys>
DICTATE <text>      CLARIFY <question> SCREENSHOT
```
