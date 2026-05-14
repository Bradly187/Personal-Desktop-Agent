# Requirements: iPad App Hardening

## Requirement 1: Sensor Lifecycle Management

### Description
A centralized `SensorManager` instantiates, starts, stops, and lifecycle-manages all five dead sensors (TiltSensor, GazeTracker, HeadTracker, KeywordListener, SoundDetector) plus AudioStreamer, driven reactively by SettingsStore toggles.

### Acceptance Criteria

1.1 Given the app launches, when SensorManager is initialized, then all 6 sensors (TiltSensor, GazeTracker, HeadTracker, KeywordListener, SoundDetector, AudioStreamer) are instantiated with references to WebSocketManager and SettingsStore.

1.2 Given a sensor's settings toggle changes from false to true, when the Combine publisher fires, then the corresponding sensor's `start()` method is called within the same run loop cycle.

1.3 Given a sensor's settings toggle changes from true to false, when the Combine publisher fires, then the corresponding sensor's `stop()` method is called and the sensor reports `isRunning == false`.

1.4 Given the app enters background or ContentView disappears, when `stopAll()` is called, then every sensor is stopped and all hardware resources (CMMotionManager, ARSession, AVAudioEngine taps) are released.

1.5 Given a sensor's required hardware is unavailable (e.g., no TrueDepth camera), when `start()` is called, then the sensor logs a warning, does not crash, and reports `isAvailable == false`.

1.6 Given the app returns to foreground after backgrounding, when `startAll()` is called, then sensors whose toggles are enabled are restarted.

## Requirement 2: Shared Audio Session

### Description
KeywordListener, SoundDetector, and AudioStreamer share a single `AVAudioEngine` instance managed by `SharedAudioSession`, eliminating the current bug where three separate engines compete for the microphone.

### Acceptance Criteria

2.1 Given SharedAudioSession is initialized, when any audio sensor starts, then AVAudioSession category is set to `.playAndRecord` with `.mixWithOthers` option.

2.2 Given multiple audio sensors are active, when they install taps, then all taps are on the same AVAudioEngine's inputNode (not separate engine instances).

2.3 Given 3 audio consumers are active and 2 stop, when the reference count drops to 1, then the AVAudioEngine continues running.

2.4 Given the last audio consumer stops, when the reference count reaches 0, then the AVAudioEngine stops and AVAudioSession is deactivated.

2.5 Given an audio interruption occurs (e.g., incoming FaceTime call), when the interruption ends, then SharedAudioSession re-activates and restarts the engine for remaining consumers.

## Requirement 3: Safe WebSocket URL Construction

### Description
`SettingsStore.wsURL` must never force-unwrap. Malformed host/port input returns nil with a safe fallback URL available.

### Acceptance Criteria

3.1 Given `serverHost` is an empty string, when `wsURL` is accessed, then it returns nil (not a crash).

3.2 Given `serverPort` is 0, negative, or > 65535, when `wsURL` is accessed, then it returns nil.

3.3 Given `serverHost` contains special characters or unicode, when `wsURL` is accessed, then it returns nil for invalid URL strings.

3.4 Given `wsURL` returns nil, when WebSocketManager needs a URL, then `wsURLOrDefault` provides `ws://192.168.1.100:8765/ws` as a compile-time-safe fallback.

3.5 Given valid host and port values, when `wsURL` is accessed, then it returns a properly formed `ws://host:port/ws` URL.

## Requirement 4: WebSocket Connection State Accuracy

### Description
WebSocket state must only transition to `.connected` after the first successful message is received, not optimistically on task resume.

### Acceptance Criteria

4.1 Given `connect()` is called, when the URLSessionWebSocketTask resumes, then state transitions to `.connecting` (not `.connected`).

4.2 Given the WebSocket task is in `.connecting` state, when the first message is successfully received, then state transitions to `.connected`.

4.3 Given the WebSocket task is in `.connecting` state, when the connection fails before any message, then state transitions to `.reconnecting` with exponential backoff.

4.4 Given state is `.connected`, when a send error or receive error occurs, then state transitions to `.reconnecting` (not directly to `.disconnected`).

## Requirement 5: Screenshot Display

### Description
Screenshots received from the PC bridge via `BridgeMessage.screenshot` must be decoded and displayed in the UI, not silently dropped.

### Acceptance Criteria

5.1 Given a `screenshot` message is received with valid base64 image data, when the message is parsed, then `ScreenshotStore.latestScreenshot` is set to a non-nil UIImage.

5.2 Given `latestScreenshot` is set, when the UI updates, then a full-screen overlay displays the screenshot image with a dismiss gesture.

5.3 Given the base64 data is corrupted or empty, when decode is attempted, then `ScreenshotStore` logs a warning and does not update the UI (no crash).

5.4 Given a screenshot overlay is displayed, when the user taps anywhere on the overlay, then the overlay dismisses and `showScreenshot` is set to false.

5.5 Given a screenshot overlay is displayed, then it has `.accessibilityLabel("Screenshot from PC")` and `.accessibilityAddTraits(.isModal)`.

## Requirement 6: mDNS Service Discovery

### Description
The iPad app uses Network framework's `NWBrowser` to discover the PC bridge via Bonjour/mDNS, matching the `_desktop-agent._tcp` service type already declared in Info.plist.

### Acceptance Criteria

6.1 Given the app launches and local network permission is granted, when ServiceDiscovery starts, then an NWBrowser is created browsing for `_desktop-agent._tcp`.

6.2 Given the PC bridge is advertising via zeroconf, when NWBrowser finds the service, then `discoveredHost` and `discoveredPort` are populated.

6.3 Given a service is discovered, when the user has not manually configured a host, then WebSocketManager uses the discovered host/port for connection.

6.4 Given no service is found within 5 seconds, when discovery times out, then the app falls back to the manually configured host/port from SettingsStore.

6.5 Given the app is backgrounded, when ServiceDiscovery is stopped, then the NWBrowser is cancelled and resources are released.

## Requirement 7: Design Token System

### Description
A `DesignTokens` enum provides all visual constants (sizing, spacing, radii, typography) enforcing 80pt minimum touch targets for primary actions, with an `AppTheme` environment for adaptive colors.

### Acceptance Criteria

7.1 Given any primary action button in the app, when rendered, then its touch target frame is at least 80×80pt.

7.2 Given any secondary/compact button, when rendered, then its touch target frame is at least 64×64pt.

7.3 Given `DesignTokens.Size.touchTargetMin`, then its value is 80.

7.4 Given the app runs in dark mode, when `AppTheme` colors are resolved, then they use system semantic colors that adapt automatically.

7.5 Given the app runs with increased contrast accessibility setting, when `AppTheme` is applied, then colors provide sufficient contrast ratios (WCAG AA minimum).

7.6 Given iPadOS 26+ is available, when `adaptiveGlass()` modifier is applied, then `.glassEffect()` is used.

7.7 Given iPadOS < 26, when `adaptiveGlass()` modifier is applied, then `.regularMaterial` background is used as fallback.

## Requirement 8: Shared Component Library

### Description
Reusable SwiftUI components (DAButton, DACard, DASectionHeader) provide consistent visual language across all 5 tabs with built-in accessibility.

### Acceptance Criteria

8.1 Given `DAButton` is used, when rendered, then it applies DesignTokens sizing, spacing, and corner radius consistently.

8.2 Given any interactive component in the library, when rendered, then it has `.accessibilityLabel` set with a meaningful description.

8.3 Given any interactive component, when rendered, then it has `.accessibilityHint` describing the action that will occur.

8.4 Given `DACard` is used, when rendered, then it uses `AppTheme.surfaceSecondary` background with `DesignTokens.Radius.md` corner radius.

8.5 Given `DASectionHeader` is used, when rendered, then it uses `DesignTokens.Typography.headline` font and `DesignTokens.Spacing.lg` padding.

## Requirement 9: Connection Status Enhancement

### Description
The connection status indicator must be more prominent and actionable — larger, always visible, and tappable to trigger reconnection.

### Acceptance Criteria

9.1 Given the connection state is `.disconnected`, when the banner is displayed, then it shows a red indicator with "Disconnected" text and a "Reconnect" button.

9.2 Given the connection state is `.connecting` or `.reconnecting`, when the banner is displayed, then it shows a yellow/orange indicator with attempt count.

9.3 Given the user taps the connection banner while disconnected, when the tap is registered, then `WebSocketManager.connect()` is called.

9.4 Given the connection banner, when rendered, then its touch target is at least 44pt tall (tappable area).

9.5 Given mDNS discovery finds a service, when displayed in the connection area, then the discovered host is shown to the user.
