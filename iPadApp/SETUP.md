# iPad App — Xcode Setup

## Create the project

1. Open Xcode → **File › New › Project**
2. Choose **iOS › App**  
3. Fill in:
   - Product Name: `DesktopAgent`
   - Team: your Apple developer team
   - Bundle Identifier: `com.yourname.DesktopAgent`
   - Interface: **SwiftUI**
   - Language: **Swift**
   - Minimum Deployments: **iPadOS 17.0**
4. Save to `iPadApp/` inside this repo.

## Add source files

In the Finder, drag the entire `DesktopAgent/` folder (containing all `.swift` files) into the Xcode project navigator. When prompted:

- ✓ Copy items if needed  
- ✓ Create groups  
- Target: DesktopAgent

Delete the auto-generated `ContentView.swift` and `DesktopAgentApp.swift` that Xcode created (the ones in this repo replace them).

## Required Capabilities (Signing & Capabilities tab)

| Capability | Why |
|---|---|
| Background Modes → Audio | KeywordListener mic continues in background |
| Speech Recognition | SFSpeechRecognizer |
| Camera | ARKit face tracking |
| Motion Usage | Core Motion tilt |
| Microphone | AVAudioEngine |

## Info.plist keys

Add these usage description strings:

```
NSCameraUsageDescription         = "Used for gaze and head tracking."
NSMicrophoneUsageDescription     = "Used for voice keyword detection."
NSSpeechRecognitionUsageDescription = "Used for on-device keyword matching."
NSMotionUsageDescription         = "Used for tilt-based cursor control."
```

## Frameworks (linked automatically via Swift import, but verify)

- ARKit
- CoreMotion
- AVFoundation
- Speech
- PencilKit

## Build & Run

- Target a **physical iPad** — ARFaceTracking requires TrueDepth camera (no Simulator).
- Set `serverHost` in Settings to your PC's local IP address.
- Start the PC bridge first: `python ipad_bridge.py`

> **Sleep prevention**: While the bridge is running it calls `SetThreadExecutionState` to keep Windows awake (the display may still turn off). Normal sleep behavior resumes automatically when the bridge exits.

## Web Client Fallback (iPad Safari)

If you can't build the native app (no Mac, no Xcode, expired provisioning), the bridge also serves a web-based client from the `web_client/` directory at the project root.

1. Start the bridge: `python ipad_bridge.py`
2. On the iPad, open Safari and navigate to `http://<PC_IP>:8765/`
3. The web client connects to the same WebSocket endpoint (`/ws`) as the native app.

The web client is served automatically when the `web_client/` directory exists alongside `ipad_bridge.py`. It provides basic touch/command input but does not support ARKit gaze tracking, Core Motion tilt, or on-device keyword detection — those require the native app.
