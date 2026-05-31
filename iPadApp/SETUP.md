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
| Background Modes → Audio | KeywordListener + AudioStreamer mic continues in background |
| Speech Recognition | SFSpeechRecognizer |
| Camera | ARKit face tracking |
| Motion Usage | Core Motion tilt |
| Microphone | AVAudioEngine |

## Info.plist keys

Add these usage description strings:

```
NSMicrophoneUsageDescription     = "Used for voice keyword detection and audio streaming to PC."
NSSpeechRecognitionUsageDescription = "Used for on-device keyword matching."
NSMotionUsageDescription         = "Used for tilt-based cursor control."
```

## Frameworks (linked automatically via Swift import, but verify)

- ARKit
- CoreMotion
- AVFoundation
- Speech
- PencilKit

## Settings

All sensor preferences are persisted in `UserDefaults` via `SettingsStore.swift`. Notable toggles:

| Setting | Default | Description |
|---------|---------|-------------|
| `audioStreamEnabled` | `false` | When enabled, streams iPad mic audio to the PC for Whisper large-v3 transcription. Requires the Background Modes → Audio capability. |

## Build Options

### Option A: Xcode (recommended for device deployment)

- Target a **physical iPad** — Core Motion (tilt) is unavailable on the Simulator.
- Set `serverHost` in Settings to your PC's local IP address.
- Start the PC bridge first: `python ipad_bridge.py`

### Option B: Swift Package Manager

A `Package.swift` is included for command-line builds and CI. From the `iPadApp/` directory:

```bash
swift build --sdk iphoneos
```

This links ARKit, CoreMotion, Speech, AVFoundation, and PencilKit automatically. Note that SPM builds still require signing and a physical device for deployment — use `xcodebuild` or Xcode for the final install step.

### Option C: GitHub Actions CI (signed + TestFlight)

A workflow at `.github/workflows/build-ipad-app.yml` builds, signs, and deploys the app. It triggers on:
- Push to `main` touching files under `iPadApp/` or the workflow file itself
- Manual `workflow_dispatch` (with an option to skip TestFlight upload)

What it does:
1. Runs on `macos-15` with Xcode 16.3
2. Installs the signing certificate and provisioning profile from GitHub Secrets
3. Uses XcodeGen to generate the `.xcodeproj` from `project.yml`
4. Archives a Release build with manual code signing
5. Exports a signed IPA via `xcodebuild -exportArchive`
6. Uploads the IPA to TestFlight (on push, or when `deploy_testflight` input is `true`)
7. Uploads the signed IPA as a GitHub Actions artifact (retained 30 days)

`CFBundleVersion` is set to the GitHub Actions run number automatically, so each build gets a unique version for TestFlight.

#### Required GitHub Secrets

| Secret | Description |
|--------|-------------|
| `CERTIFICATE_P12` | Base64-encoded Apple Distribution certificate (.p12) |
| `CERTIFICATE_PASSWORD` | Password for the .p12 file |
| `KEYCHAIN_PASSWORD` | Arbitrary password for the temporary CI keychain |
| `PROVISIONING_PROFILE` | Base64-encoded App Store provisioning profile (.mobileprovision) |
| `PROVISIONING_PROFILE_NAME` | Name of the provisioning profile (as shown in Apple Developer portal) |
| `TEAM_ID` | Apple Developer Team ID |
| `ASC_KEY_ID` | App Store Connect API Key ID |
| `ASC_ISSUER_ID` | App Store Connect API Issuer ID |
| `ASC_PRIVATE_KEY` | App Store Connect API private key (.p8 contents) |

#### Manual dispatch options

When triggering manually, you can set `deploy_testflight` to `false` to build and sign without uploading to TestFlight. This is useful for validating the build pipeline.

> **Note:** Codemagic CI was previously configured but has been removed. GitHub Actions is the sole CI system.

### Option D: Signed Build + TestFlight (no Mac needed day-to-day)

See [`.github/SIGNING_SETUP.md`](../.github/SIGNING_SETUP.md) for the full walkthrough. Once configured:

1. Push code to `iPadApp/`
2. GitHub Actions builds, signs, and uploads to TestFlight
3. Install on your iPad via the TestFlight app

Requires: Apple Developer Program ($99/yr) + one-time Mac access to generate certificates.

> **Sleep prevention**: While the bridge is running it calls `SetThreadExecutionState` to keep Windows awake (the display may still turn off). Normal sleep behavior resumes automatically when the bridge exits.

## Web Client Fallback (iPad Safari)

If you can't build the native app (no Mac, no Xcode, expired provisioning), the bridge also serves a web-based client from the `web_client/` directory at the project root.

1. Start the bridge: `python ipad_bridge.py`
2. On the iPad, open Safari and navigate to `http://<PC_IP>:8765/`
3. The web client connects to the same WebSocket endpoint (`/ws`) as the native app.

The web client is served automatically when the `web_client/` directory exists alongside `ipad_bridge.py`. It provides basic touch/command input but does not support Core Motion tilt or on-device keyword detection — those require the native app.
