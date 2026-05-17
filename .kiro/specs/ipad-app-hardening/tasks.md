# Tasks: iPad App Hardening

## Task 1: Create SharedAudioSession
**Requirements**: 2.1, 2.2, 2.3, 2.4, 2.5

- [x] 1.1 Create `iPadApp/DesktopAgent/Audio/SharedAudioSession.swift` with reference-counted AVAudioEngine management
- [x] 1.2 Implement `activate()` — configure AVAudioSession category `.playAndRecord` with `.mixWithOthers`, start engine
- [x] 1.3 Implement `addConsumer(_:)` / `removeConsumer(_:)` with reference counting
- [x] 1.4 Implement `deactivate()` — stop engine and set session inactive only when consumers == 0
- [x] 1.5 Subscribe to `AVAudioSession.interruptionNotification` — pause on interruption began, re-activate on interruption ended
- [x] 1.6 Expose `engine` property for sensors to install taps on

## Task 2: Refactor Audio Sensors to Use SharedAudioSession
**Requirements**: 2.2, 2.3

- [x] 2.1 Refactor `KeywordListener` — remove private `AVAudioEngine()`, accept `SharedAudioSession` in init, install tap on shared engine
- [x] 2.2 Refactor `SoundDetector` — remove private `AVAudioEngine()`, accept `SharedAudioSession` in init, install tap on shared engine
- [x] 2.3 Refactor `AudioStreamer` — remove private `AVAudioEngine()`, accept `SharedAudioSession` in init, install tap on shared engine
- [x] 2.4 Ensure each sensor removes its tap in `stop()` without stopping the shared engine
- [x] 2.5 Verify all three sensors can run simultaneously on the same engine without tap conflicts

## Task 3: Create SensorManager
**Requirements**: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6

- [x] 3.1 Create `iPadApp/DesktopAgent/SensorManager.swift` — instantiate all 7 sensors with shared dependencies (SharedAudioSession for audio trio, SharedFaceSession for GazeTracker + HeadTracker)
- [x] 3.2 Implement `startAll()` — start sensors whose toggles are enabled and hardware is available
- [x] 3.3 Implement `stopAll()` — stop all sensors and release resources
- [x] 3.4 Implement Combine subscriptions to SettingsStore toggles (`tiltEnabled`, `gazeEnabled`, `headEnabled`, `audioStreamEnabled`, `keywordList`, `soundMappings`)
- [x] 3.5 Add hardware availability checks before starting each sensor (CMMotionManager.isDeviceMotionAvailable, ARFaceTrackingConfiguration.isSupported)
- [x] 3.6 Expose `@Published` sensor states for UI consumption

## Task 4: Wire SensorManager into DesktopAgentApp
**Requirements**: 1.1, 1.4, 1.6

- [x] 4.1 Replace `AudioStreamerController` in `DesktopAgentApp.swift` with `SensorManager`
- [x] 4.2 Initialize SensorManager with WebSocketManager and SettingsStore
- [x] 4.3 Call `sensorManager.startAll()` in `.onAppear`
- [x] 4.4 Call `sensorManager.stopAll()` in `.onDisappear`
- [x] 4.5 Inject `sensorManager` as `.environmentObject` for UI access
- [x] 4.6 Remove the old `AudioStreamerController` class and its `.onReceive(settings.$audioStreamEnabled)` logic

## Task 5: Fix SettingsStore.wsURL Force-Unwrap
**Requirements**: 3.1, 3.2, 3.3, 3.4, 3.5

- [x] 5.1 Change `wsURL` from `URL` to `URL?` — validate host non-empty, port in 1...65535 range
- [x] 5.2 Add `wsURLOrDefault` computed property returning `wsURL ?? URL(string: "ws://192.168.1.100:8765/ws")!` (compile-time safe literal)
- [x] 5.3 Update `WebSocketManager._connect()` to use `settings?.wsURLOrDefault` instead of `settings.wsURL`
- [x] 5.4 Add input validation UI in SettingsView — show warning text when wsURL is nil
- [x] 5.5 Add unit tests fuzzing serverHost/serverPort edge cases

## Task 6: Fix WebSocket Optimistic Connection State
**Requirements**: 4.1, 4.2, 4.3, 4.4

- [x] 6.1 In `_connect()`, set state to `.connecting` (not `.connected`) after `wsTask.resume()`
- [x] 6.2 In `_receiveLoop()`, transition to `.connected` only after first successful `task.receive()` call
- [x] 6.3 Reset `reconnectAttempt` to 0 only when transitioning to `.connected`
- [x] 6.4 Ensure `send()` guards on `state == .connected` (already does, but verify no race)
- [x] 6.5 Update `_handleDisconnect` to transition to `.reconnecting` (not `.disconnected`) on first failure

## Task 7: Implement Screenshot Display
**Requirements**: 5.1, 5.2, 5.3, 5.4, 5.5

- [x] 7.1 Create `iPadApp/DesktopAgent/ScreenshotStore.swift` — `@MainActor ObservableObject` with `latestScreenshot: UIImage?` and `showScreenshot: Bool`
- [x] 7.2 Implement `handleScreenshot(base64:mime:)` — decode base64, create UIImage, set showScreenshot = true
- [x] 7.3 Create `iPadApp/DesktopAgent/UI/ScreenshotOverlayView.swift` — full-screen overlay with dismiss-on-tap
- [x] 7.4 Add accessibility annotations: `.accessibilityLabel("Screenshot from PC")`, `.accessibilityAddTraits(.isModal)`
- [x] 7.5 Wire ScreenshotStore into ContentView — observe `wsManager.$lastMessage` for `.screenshot` cases
- [x] 7.6 Add ScreenshotOverlayView as `.overlay` on ContentView

## Task 8: Implement mDNS Discovery
**Requirements**: 6.1, 6.2, 6.3, 6.4, 6.5

- [x] 8.1 Create `iPadApp/DesktopAgent/Network/ServiceDiscovery.swift` with NWBrowser for `_desktop-agent._tcp`
- [x] 8.2 Implement `startBrowsing()` — create NWBrowser, set state/results handlers
- [x] 8.3 Implement endpoint resolution — NWConnection to resolve service name to host:port
- [x] 8.4 Implement `stopBrowsing()` — cancel browser, release resources
- [x] 8.5 Wire into WebSocketManager — if discoveredHost is available and no manual override, use discovered endpoint
- [x] 8.6 Add 5-second timeout fallback to manual host/port from SettingsStore
- [x] 8.7 Show discovered service info in SettingsView connection section

## Task 9: Create DesignTokens
**Requirements**: 7.1, 7.2, 7.3

- [x] 9.1 Create `iPadApp/DesktopAgent/DesignSystem/DesignTokens.swift` — enum with Size, Spacing, Radius, Typography nested enums
- [x] 9.2 Define `Size.touchTargetMin = 80`, `Size.touchTargetCompact = 64`, `Size.iconSize = 24`, `Size.iconSizeLarge = 32`
- [x] 9.3 Define spacing scale: xs=4, sm=8, md=12, lg=16, xl=24, xxl=32
- [x] 9.4 Define radius scale: sm=8, md=12, lg=16, xl=20
- [x] 9.5 Define typography: headline, body, caption, mono using system fonts

## Task 10: Create AppTheme and Environment
**Requirements**: 7.4, 7.5, 7.6, 7.7

- [x] 10.1 Create `iPadApp/DesktopAgent/DesignSystem/AppTheme.swift` — struct with semantic color properties
- [x] 10.2 Create `AppThemeKey: EnvironmentKey` with default theme using system semantic colors
- [x] 10.3 Add `EnvironmentValues` extension for `appTheme` key path
- [x] 10.4 Create `adaptiveGlass()` View extension — `#available(iOS 26, *)` check with `.glassEffect()` or `.regularMaterial` fallback
- [x] 10.5 Inject AppTheme into environment at app root level

## Task 11: Create Shared Component Library
**Requirements**: 8.1, 8.2, 8.3, 8.4, 8.5

- [x] 11.1 Create `iPadApp/DesktopAgent/DesignSystem/Components/DAButton.swift` — 80pt min target, icon + label, accessibility
- [x] 11.2 Create `iPadApp/DesktopAgent/DesignSystem/Components/DACard.swift` — surface background, rounded corners, padding
- [x] 11.3 Create `iPadApp/DesktopAgent/DesignSystem/Components/DASectionHeader.swift` — headline font, consistent spacing
- [x] 11.4 Create `iPadApp/DesktopAgent/DesignSystem/Components/DAConnectionBanner.swift` — prominent status with reconnect action
- [x] 11.5 Add `.accessibilityLabel` and `.accessibilityHint` to all interactive components

## Task 12: Enhance Connection Status Banner
**Requirements**: 9.1, 9.2, 9.3, 9.4, 9.5

- [x] 12.1 Redesign `ConnectionBanner` — larger indicator (not capsule, full-width bar), minimum 44pt tap target
- [x] 12.2 Add "Reconnect" button visible when state is `.disconnected`
- [x] 12.3 Show reconnection attempt count when state is `.reconnecting`
- [x] 12.4 Add tap gesture on banner to trigger `wsManager.connect()` when disconnected
- [x] 12.5 Display discovered mDNS host when available (from ServiceDiscovery)
- [x] 12.6 Apply DesignTokens and AppTheme to the banner

## Task 13: Adopt Design System Across Existing Views
**Requirements**: 7.1, 8.1

- [x] 13.1 Refactor `CommandPadView` — replace hardcoded 80pt with `DesignTokens.Size.touchTargetMin`, use DAButton
- [x] 13.2 Refactor `TrackpadView` — apply DesignTokens spacing and radii, ensure click buttons meet 80pt minimum
- [x] 13.3 Refactor `ScientificKeypadView` — apply DesignTokens to key buttons (64pt compact minimum)
- [x] 13.4 Refactor `HandwritingCanvasView` — apply DesignTokens to toolbar buttons
- [x] 13.5 Refactor `SettingsView` — apply DASectionHeader, consistent spacing
- [x] 13.6 Replace `ConnectionBanner` in ContentView with `DAConnectionBanner`
- [x] 13.7 Verify all 5 tabs pass accessibility audit for touch target sizes
