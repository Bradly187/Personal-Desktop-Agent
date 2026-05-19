# iPad UI Touch Passthrough Bugfix Design

## Overview

Overlay views (`DwellToolbarContainer` and `DAConnectionBanner`) in `ContentView.swift` intercept touches that should pass through to the tab bar and tab content. The root cause is that non-interactive areas of these overlays (padding, background fills, disabled button states) participate in hit-testing despite having no interactive purpose. The fix applies `.allowsHitTesting(false)` to non-interactive regions and constrains `.contentShape()` to visible interactive bounds only.

## Glossary

- **Bug_Condition (C)**: A touch lands in the overlay layer's frame but outside any visible interactive element (toolbar button, reconnect button) — the touch is swallowed instead of passing through
- **Property (P)**: Touches outside visible interactive elements pass through overlays to the tab bar and tab content immediately
- **Preservation**: All existing interactive behaviors (dwell action selection, toolbar drag repositioning, reconnect tap, screenshot overlay dismiss) continue to function identically
- **DwellToolbarContainer**: The full-screen ZStack overlay in `ContentView.swift` that positions the dwell action toolbar in top, bottom, or floating mode
- **DAConnectionBanner**: The connection status banner overlaid at the top of the TabView showing connected/disconnected state
- **Hit-testing**: SwiftUI's mechanism for determining which view receives a touch event based on frame geometry and `.allowsHitTesting()` / `.contentShape()` modifiers

## Bug Details

### Bug Condition

The bug manifests when a user touches the screen in an area covered by the overlay views' frames but not by any visible interactive element. The `DwellToolbarContainer` uses `.frame(maxWidth: .infinity)` in top/bottom modes creating a full-width hit area, and the `DAConnectionBanner` uses `.contentShape(Rectangle())` with `.disabled(!isDisconnected)` which still participates in hit-testing when connected.

**Formal Specification:**
```
FUNCTION isBugCondition(input)
  INPUT: input of type TouchEvent with (x, y, targetView)
  OUTPUT: boolean
  
  LET overlayFrame = DwellToolbarContainer.frame OR DAConnectionBanner.frame
  LET visibleInteractiveArea = toolbarButtons.union OR reconnectButton (when disconnected)
  
  RETURN input.point IS WITHIN overlayFrame
         AND input.point IS NOT WITHIN visibleInteractiveArea
         AND input.intendedTarget IS tabBar OR tabContent
END FUNCTION
```

### Examples

- **Tab bar tap blocked by bottom toolbar padding**: User taps the "Trackpad" tab icon. The tap lands in the 56pt bottom padding of `DwellToolbarContainer` (bottom mode). The overlay swallows the touch — tab does not switch.
- **Tab content tap blocked by toolbar full-width frame**: User taps a command button on `CommandPadView`. The tap is within the `.frame(maxWidth: .infinity)` area of the toolbar overlay but not on any toolbar button. Touch is intercepted — command does not fire.
- **Navigation area tap blocked by banner**: User taps a navigation element near the top of a tab. The tap lands within `DAConnectionBanner`'s `.contentShape(Rectangle())` area while connected (button disabled but still hit-testing). Touch is swallowed.
- **Trackpad gesture blocked by floating toolbar overlap**: User drags on `TrackpadView` in an area where the floating toolbar's DragGesture region extends beyond the visible toolbar frame. The drag is captured by the toolbar's gesture instead of the trackpad's pan recognizer.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- Tapping dwell action buttons (left-click, right-click, double-click, drag, scroll) on the toolbar selects that action and syncs via WebSocket
- Dragging the floating toolbar repositions it and persists the offset to UserDefaults
- Tapping "Reconnect" on the DAConnectionBanner when disconnected triggers WebSocket reconnection
- ScreenshotOverlayView continues to show full-screen and dismiss on tap
- TrackpadView pan, tap, and two-finger scroll gestures continue to send commands via WebSocket
- CommandPadView grid buttons continue to fire actions
- One-shot dwell mode resets to leftClick after firing
- Settings toolbar position changes reposition the toolbar correctly

**Scope:**
All touches that land on visible interactive elements (toolbar buttons, reconnect button when disconnected, screenshot overlay) should continue to be handled by those elements. Only touches that land in non-interactive overlay regions (padding, background, disabled banner) should pass through.

## Hypothesized Root Cause

Based on code analysis, the issues are:

1. **DwellToolbarContainer top/bottom mode full-width frame**: In `toolbarPositioned`, the `.frame(maxWidth: .infinity)` modifier on `toolbarView` creates a full-width hit area. The `RoundedRectangle` background fills this entire width. Even though the actual toolbar buttons are narrower, the full-width frame intercepts touches across the entire row.

2. **DwellToolbarContainer bottom padding overlap**: The `.padding(.bottom, 56)` in bottom mode pushes the toolbar up but the padding area itself (56pt tall, full width) remains part of the view's hit-testable frame, overlapping the tab bar region (~49pt).

3. **DAConnectionBanner `.contentShape(Rectangle())` when disabled**: The `.contentShape(Rectangle())` modifier makes the entire banner frame tappable. Combined with `.disabled(!isDisconnected)`, the button is disabled when connected but still participates in hit-testing — SwiftUI's `.disabled()` prevents the action but does NOT prevent hit-test interception.

4. **Floating toolbar DragGesture scope**: The `DragGesture()` is attached to the entire `toolbarView` frame (including background and padding), not just the visible toolbar content. This can capture drags that should reach the TrackpadView underneath.

## Correctness Properties

Property 1: Bug Condition - Overlay Non-Interactive Areas Pass Through Touches

_For any_ touch event where the touch point is within an overlay frame (DwellToolbarContainer or DAConnectionBanner) but NOT within a visible interactive element (toolbar button or enabled reconnect button), the fixed overlay SHALL allow the touch to pass through to the underlying tab bar or tab content view.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5**

Property 2: Preservation - Interactive Overlay Elements Continue to Receive Touches

_For any_ touch event where the touch point IS within a visible interactive element (dwell action button, floating toolbar drag surface, or reconnect button when disconnected), the fixed overlay SHALL continue to handle the touch exactly as before, preserving all existing toolbar interaction, drag repositioning, and reconnection functionality.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10, 3.11, 3.12, 3.13, 3.14**

## Fix Implementation

### Changes Required

Assuming our root cause analysis is correct:

**File**: `iPadApp/DesktopAgent/UI/DwellToolbarContainer.swift`

**Function**: `toolbarPositioned` (top and bottom cases)

**Specific Changes**:

1. **Remove `.frame(maxWidth: .infinity)` from toolbar in top/bottom modes**: Instead of making the toolbar full-width at the container level, let the `DwellActionToolbar` define its own intrinsic width. Apply `.frame(maxWidth: .infinity)` only to the inner content that needs it, and use `.contentShape()` to constrain the hit area to the visible background bounds.

2. **Add `.allowsHitTesting(false)` to the padding/spacer areas**: Wrap the bottom padding in a structure that explicitly disables hit-testing, or use a custom layout that separates the interactive toolbar from the non-interactive spacing.

3. **Constrain hit area with `.contentShape()`**: Apply `.contentShape(RoundedRectangle(...))` to match only the visible `RoundedRectangle` background, so touches outside the visible toolbar shape pass through.

4. **Floating mode: scope DragGesture to visible content only**: Move the `.gesture(DragGesture())` modifier to be applied after `.contentShape()` so it only captures drags that start within the visible toolbar bounds. Use `.contentShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.md))` before the gesture.

5. **Add `.allowsHitTesting(false)` to the outer ZStack's non-interactive space**: Ensure the ZStack itself does not intercept touches in areas not covered by the toolbar. Since the ZStack uses alignment-based positioning, the empty space should not hit-test.

**File**: `iPadApp/DesktopAgent/DesignSystem/Components/DAConnectionBanner.swift`

**Specific Changes**:

6. **Conditionally disable hit-testing when connected**: Add `.allowsHitTesting(isDisconnected)` to the `Button` or its container. When the connection is active (not disconnected), the entire banner passes touches through. Only when disconnected does the banner intercept touches for the reconnect action.

7. **Remove `.contentShape(Rectangle())`**: The `.contentShape(Rectangle())` forces the entire frame to be tappable. Remove it and let the natural button content define the tap area, or move it inside a conditional that only applies when disconnected.

**File**: `iPadApp/DesktopAgent/ContentView.swift`

**Specific Changes**:

8. **No structural changes needed**: The overlay composition order is correct. The fixes in the individual overlay views will resolve the passthrough issues without changing ContentView's structure.

## Testing Strategy

### Validation Approach

The testing strategy follows a two-phase approach: first, surface counterexamples that demonstrate the bug on unfixed code, then verify the fix works correctly and preserves existing behavior.

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate the bug BEFORE implementing the fix. Confirm or refute the root cause analysis. If we refute, we will need to re-hypothesize.

**Test Plan**: Write SwiftUI ViewInspector or UI tests that simulate touches at specific coordinates within overlay frames but outside visible interactive elements. Run these tests on the UNFIXED code to observe that touches are intercepted.

**Test Cases**:
1. **Tab bar tap with bottom toolbar**: Simulate a tap at the tab bar Y-coordinate while DwellToolbarContainer is in bottom mode — verify the tap is swallowed by the overlay (will fail to reach tab bar on unfixed code)
2. **Content tap through toolbar width**: Simulate a tap within the `.frame(maxWidth: .infinity)` area but outside toolbar buttons — verify the tap does not reach CommandPadView (will fail on unfixed code)
3. **Banner tap when connected**: Simulate a tap in the DAConnectionBanner area while in connected state — verify the tap is intercepted despite the button being disabled (will fail on unfixed code)
4. **Trackpad drag under floating toolbar**: Simulate a drag gesture starting near but outside the visible floating toolbar — verify the DragGesture captures it instead of the trackpad (will fail on unfixed code)

**Expected Counterexamples**:
- Touches at tab bar coordinates are consumed by the 56pt padding area of the bottom-positioned toolbar
- Touches in the full-width frame area are consumed by the toolbar's background RoundedRectangle
- Touches on the banner when connected are consumed by the disabled Button with `.contentShape(Rectangle())`
- Possible causes confirmed: `.frame(maxWidth: .infinity)` hit area, `.contentShape(Rectangle())` on disabled button, padding participating in hit-testing

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds, the fixed function produces the expected behavior.

**Pseudocode:**
```
FOR ALL touch WHERE isBugCondition(touch) DO
  result := hitTest_fixed(touch)
  ASSERT touch passes through overlay to underlying view
  ASSERT underlying view (tabBar OR tabContent) receives the touch
END FOR
```

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold, the fixed function produces the same result as the original function.

**Pseudocode:**
```
FOR ALL touch WHERE NOT isBugCondition(touch) DO
  ASSERT hitTest_fixed(touch) = hitTest_original(touch)
  // Toolbar buttons still receive taps
  // Floating toolbar still receives drag gestures on its visible frame
  // Reconnect button still receives taps when disconnected
END FOR
```

**Testing Approach**: Property-based testing is recommended for preservation checking because:
- It generates many random touch coordinates across the screen to verify passthrough behavior
- It catches edge cases at boundary pixels between interactive and non-interactive regions
- It provides strong guarantees that no existing interactive element lost its touch handling

**Test Plan**: Observe behavior on UNFIXED code first for toolbar button taps, drag repositioning, and reconnect taps, then write property-based tests capturing that behavior continues after the fix.

**Test Cases**:
1. **Toolbar button tap preservation**: Verify tapping each dwell action button continues to select that action after the fix
2. **Floating drag preservation**: Verify dragging the floating toolbar continues to reposition it and persist offset
3. **Reconnect tap preservation**: Verify tapping reconnect when disconnected continues to trigger connection
4. **Screenshot overlay preservation**: Verify the screenshot overlay continues to show and dismiss on tap

### Unit Tests

- Test that `.allowsHitTesting(false)` is applied to non-interactive padding areas in top/bottom modes
- Test that `.allowsHitTesting(isDisconnected)` correctly toggles on DAConnectionBanner
- Test that `.contentShape()` on the toolbar matches only the visible RoundedRectangle bounds
- Test edge case: toolbar in bottom mode with exactly 56pt padding — verify tab bar at y=0 of padding is not blocked
- Test edge case: floating toolbar at screen edge — verify touches just outside bounds pass through

### Property-Based Tests

- Generate random (x, y) coordinates within the screen bounds and verify: if the point is NOT within any visible interactive element's bounds, the touch passes through the overlay
- Generate random toolbar positions (top, bottom, floating with random offset) and verify non-interactive areas always pass through
- Generate random connection states (connected, connecting, reconnecting, disconnected) and verify banner hit-testing matches the `isDisconnected` condition

### Integration Tests

- Test full tab switching flow with toolbar in each position (top, bottom, floating) — all tabs must be reachable
- Test CommandPadView button taps with toolbar overlay present — all grid buttons must fire
- Test TrackpadView gestures with floating toolbar positioned over the trackpad area — trackpad gestures must work outside toolbar bounds
- Test DAConnectionBanner state transitions — verify hit-testing updates when connection state changes from disconnected to connected
- Test that HandwritingCanvasView drawing works without interference from overlays
- Test ScientificKeypadView button presses with overlays present
