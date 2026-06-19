---
inclusion: always
---

# Product Context

Multimodal accessibility desktop control agent for a single user with rheumatoid arthritis. Enables hands-free and low-effort Windows desktop control through voice, hand gestures, eye gaze, iPad touch, tilt, head tracking, and sound actions.

## Design Principles

- **Graceful degradation** — every modality is optional. The system must function with any subset of sensors active. Never assume a sensor is available; always guard with try/except or availability checks.
- **Local-first** — all inference runs on an RTX 5090 GPU. AWS services are fallback only, triggered when local confidence is low or GPU is saturated.
- **Adaptive** — routing thresholds, gesture confidence floors, and Whisper vocabulary self-tune over time via ContinuousTrainer. No manual reconfiguration required.
- **RA-aware UX** — oversized touch targets, dwell activation, palm rejection, relaxed confidence thresholds on bad pain days. When in doubt, prefer lower-effort interactions.
- **Single user** — no multi-user, auth, or permissions logic. Optimize for one person's patterns.

## Sensor Priority (FusionEngine routing order)

Higher priority inputs short-circuit lower ones. Respect this order in all routing decisions:

1. iPad touch command → immediate execution, bypasses LLM
2. Sound action → mapped mouth sounds (cluck, pop, hiss)
3. Gaze delta cursor → relative eye movement drives cursor (no dwell)
4. Gaze + voice "click" → click at current cursor position
5. Gaze + gesture POINT → click at current cursor position
6. Tilt navigation → iPad tilt moves cursor (Core Motion)
7. Head tracking → head pose moves cursor (ARKit face anchor)
8. Gesture alone → hand gesture via iPad camera + MediaPipe on PC
9. On-device voice keyword → Speech Framework fast-path match
10. PC-transcribed voice → full Whisper large-v3 + LLM pipeline

## Hardware Scope

- **In scope**: RTX 5090 PC + iPad Pro 2020+ (TrueDepth camera, LiDAR, accelerometer, gyroscope, microphone)
- **Out of scope**: All standalone sensor hardware (ReSpeaker, RealSense, Ultraleap, Tobii, OAK-D, FIFINE, Leap Motion). Do not add dependencies on these devices.

## Product Rules for AI Assistants

- Never introduce a hard dependency on a sensor — wrap all sensor access in graceful fallback logic.
- Prefer iPad built-in sensors over any external hardware solution.
- When adding new input modalities, slot them into the priority list above and document the insertion point.
- All user-facing latency targets matter: voice commands < 600ms end-to-end, gesture recognition < 50ms, touch commands < 100ms.
- Cloud calls (Bedrock, Transcribe) are last-resort fallbacks. Do not add cloud dependencies to the happy path.
- The `Command` dataclass is the only DTO that crosses pipeline boundaries. Do not introduce alternative message types.
- Action vocabulary is constrained to: CLICK, MOUSEDOWN, MOUSEUP, SCROLL, TYPE, OPEN, CLOSE, HOTKEY, DICTATE, CLARIFY, SCREENSHOT. Propose new actions explicitly before adding them.

## Current Focus

The active spec (`ipad-sensor-focus`) builds a native Swift/SwiftUI iPad app using Core Motion, ARKit, Speech framework, and AVFoundation. The PC side handles inference, desktop execution via MCP server, and continuous learning.
