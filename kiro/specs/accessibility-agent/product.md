# Product overview

## What this is

A multimodal accessibility desktop control agent for a user with rheumatoid arthritis.
The system allows hands-free and low-effort desktop control through voice commands,
hand gestures, eye gaze, and iPad touch input. It is designed to remain fully
functional on high-pain days when typing and precise mouse use are difficult or impossible.

## Who it is for

A single primary user with rheumatoid arthritis who experiences variable hand and
wrist pain and needs reliable desktop control that adapts to their condition daily.

## Core design principles

- **Graceful degradation** — every modality is optional. The system works with just
  a microphone if needed, and becomes progressively more capable as more sensors are added.
- **Local-first** — the RTX 5090 GPU runs all inference locally by default. AWS cloud
  services are a fallback, not the primary path. Zero mandatory internet dependency.
- **Adaptive** — the system continuously learns from usage. Routing thresholds, gesture
  confidence floors, Whisper vocabulary, and LLM few-shot examples all improve over time
  without any manual configuration.
- **RA-aware** — all interaction targets are oversized. Dwell activation removes the need
  for press force. Palm rejection prevents accidental input when the hand rests on the iPad.
  Thresholds relax automatically on days when voice and gesture confidence is consistently lower.

## Interaction modalities (priority order)

1. **iPad touch** — highest priority, bypasses LLM routing, directly executes
2. **Gaze + voice** — look at a target, say "click" — most natural RA interaction
3. **Gaze + gesture** — point at target while looking at it
4. **Voice alone** — full natural language commands routed through Whisper + LLM
5. **Gesture alone** — hand gestures from Ultraleap / Leap Motion / MediaPipe

## Hardware targets

### Full stack (~$820)
- RTX 5090 PC (existing)
- ReSpeaker USB Mic Array v2
- Intel RealSense D455
- Ultraleap Controller 2
- Tobii Eye Tracker 5

### Budget stack (~$251)
- RTX 5090 PC (existing)
- FIFINE AM8 USB mic
- OAK-D Lite
- Leap Motion Controller v1 (used)
- Logitech C920 webcam
- Eyeware Beam software ($30)

### iPad stack (if iPad Pro 2020+ owned)
- Replaces OAK-D Lite via Record3D LiDAR streaming
- Replaces C920 via USB webcam mode
- Improves gaze accuracy via TrueDepth + Eyeware Beam iOS app
- Adds touch command pad, virtual trackpad, and dwell zones via Safari web interface
