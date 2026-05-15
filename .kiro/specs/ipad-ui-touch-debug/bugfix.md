# Bugfix Requirements Document

## Introduction

The iPad app's tab bar and content areas suffer from touch responsiveness issues caused by overlay views intercepting touches that should pass through to underlying content. The `DwellToolbarContainer` overlay uses a full-screen ZStack that can block touches, the `DAConnectionBanner` overlay at the top can intercept navigation area touches, and the toolbar's bottom-positioned padding area overlaps the tab bar's touch region. These issues make tab switching feel "sticky" or unresponsive and can block interaction with tab content.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN the DwellToolbarContainer overlay is present AND the user taps on tab bar items THEN the system intermittently fails to register the tap because the toolbar's `.frame(maxWidth: .infinity)` background/padding area in bottom position overlaps the tab bar's hit-test region

1.2 WHEN the DwellToolbarContainer is in bottom position with `padding(.bottom, 56)` AND the user taps tab bar items THEN the system feels "sticky" because the 56pt bottom padding area of the toolbar overlay sits above the TabView in z-order and intercepts touches meant for the tab bar

1.3 WHEN the DAConnectionBanner overlay is displayed at the top AND the user taps navigation controls or content near the top of a tab THEN the system may intercept the touch in the banner's full-width hit area instead of passing it to the underlying navigation elements

1.4 WHEN the DwellToolbarContainer ZStack is rendered AND the user taps anywhere on tab content that is not directly under the visible toolbar buttons THEN the system may still route the touch through the full-screen ZStack frame before reaching the tab content, causing perceptible delay or missed taps

1.5 WHEN the DwellToolbarContainer is in floating mode AND the floating toolbar overlaps the TrackpadView gesture surface THEN the system may create a gesture conflict between the toolbar's DragGesture and the trackpad's pan gesture recognizer

### Expected Behavior (Correct)

2.1 WHEN the DwellToolbarContainer overlay is present AND the user taps on tab bar items THEN the system SHALL register the tap immediately because only the visible toolbar buttons receive touches — all other areas pass through

2.2 WHEN the DwellToolbarContainer is in bottom position AND the user taps tab bar items THEN the system SHALL respond without delay because the toolbar's non-interactive padding and background areas do not intercept touches in the tab bar region

2.3 WHEN the DAConnectionBanner overlay is displayed AND the user taps navigation controls or content not covered by the visible banner THEN the system SHALL pass the touch through to the underlying view because the banner's hit-test area is constrained to its visible bounds only

2.4 WHEN the DwellToolbarContainer ZStack is rendered AND the user taps anywhere outside the visible toolbar buttons THEN the system SHALL pass the touch through immediately to the tab content below without any hit-test interference from the container's frame

2.5 WHEN the DwellToolbarContainer is in floating mode AND the floating toolbar overlaps the TrackpadView gesture surface THEN the system SHALL only capture drag gestures on the visible toolbar itself — touches outside the toolbar bounds SHALL pass through to the trackpad gesture recognizer

### Unchanged Behavior (Regression Prevention)

3.1 WHEN the user taps a dwell action button on the toolbar (left-click, right-click, double-click, drag, scroll) THEN the system SHALL CONTINUE TO select that action and sync it via WebSocket

3.2 WHEN the user drags the floating toolbar to reposition it THEN the system SHALL CONTINUE TO update the toolbar's position and persist the offset to UserDefaults

3.3 WHEN the user taps the DAConnectionBanner's "Reconnect" button while disconnected THEN the system SHALL CONTINUE TO trigger a WebSocket reconnection

3.4 WHEN a screenshot is received and the ScreenshotOverlayView is displayed THEN the system SHALL CONTINUE TO show the full-screen modal overlay that dismisses on tap

3.5 WHEN the user interacts with the TrackpadView gesture surface (pan, tap, two-finger scroll) in areas not covered by the floating toolbar THEN the system SHALL CONTINUE TO send cursor movement, click, and scroll commands via WebSocket

3.6 WHEN the toolbar position is changed in Settings (top, bottom, floating) THEN the system SHALL CONTINUE TO reposition the toolbar accordingly and persist the choice

3.7 WHEN the user taps command buttons on the CommandPadView grid THEN the system SHALL CONTINUE TO send the corresponding action (CLICK, SCROLL, HOTKEY, etc.) via WebSocket without interference from overlays

3.8 WHEN the user switches between tabs (Commands, Trackpad, Keypad, Write, Settings) by tapping tab bar items that are NOT overlapped by the toolbar THEN the system SHALL CONTINUE TO switch tabs immediately with standard iOS tab bar responsiveness

3.9 WHEN the one-shot dwell action mode is enabled AND a non-default action fires THEN the system SHALL CONTINUE TO reset the active dwell action to leftClick after firing

3.10 WHEN the user is on the HandwritingCanvasView tab and draws on the canvas THEN the system SHALL CONTINUE TO capture drawing input without interference from the DwellToolbarContainer overlay

3.11 WHEN the DwellToolbarContainer is in top position AND the DAConnectionBanner is also displayed at the top THEN the system SHALL CONTINUE TO show both elements without one completely hiding the other — both remain accessible

3.12 WHEN the user scrolls content within a tab (e.g., Settings list, Command grid overflow) THEN the system SHALL CONTINUE TO scroll smoothly without the overlay container consuming scroll gestures

3.13 WHEN the app enters background and returns to foreground THEN the system SHALL CONTINUE TO preserve the toolbar position and floating offset without resetting touch passthrough behavior

3.14 WHEN the ScientificKeypadView is active and the user taps keypad buttons THEN the system SHALL CONTINUE TO register key presses and send them via WebSocket without overlay interference
