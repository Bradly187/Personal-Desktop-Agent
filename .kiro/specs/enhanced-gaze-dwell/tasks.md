;.l,/# Implementation Plan: Enhanced Gaze Dwell Actions

## Overview

This plan implements the enhanced gaze dwell system in two parallel tracks: PC-side Python (FusionEngine, IPadBridge, FusionConfig) and iPad-side Swift/SwiftUI (DwellActionSelector, DwellActionToolbar, FeatureToggleStore, GazeTracker modifications). The WebSocket protocol extension is the foundation — both sides depend on it. Property-based tests use Hypothesis (PC) and SwiftCheck (iPad).

## Tasks

- [x] 1. WebSocket protocol extension and IPadBridge modifications
  - [x] 1.1 Extend IPadBridge to handle `set_dwell_action` messages
    - Add handler for `set_dwell_action` message type in `ipad_bridge.py`
    - Validate `action_type` against the set {"left_click", "right_click", "double_click", "drag_start", "drag_end"}
    - Respond with ack message: `{"type":"ack","status":"ok","action_type":"<value>"}` on valid input
    - Respond with ack message: `{"type":"ack","status":"error","error":"<reason>"}` on invalid input
    - Store active dwell action type as bridge state (default: "left_click")
    - _Requirements: 7.2, 7.4_

  - [x] 1.2 Extend IPadBridge to handle `set_feature_toggle` messages
    - Add handler for `set_feature_toggle` message type
    - Accept `feature` (string) and `enabled` (boolean) fields
    - Respond with ack: `{"type":"ack","status":"ok","feature":"<name>","enabled":<bool>}`
    - Respond with error ack for unknown feature names
    - Forward toggle state to FusionEngine via `set_feature_toggle()`
    - _Requirements: 7.3_

  - [x] 1.3 Modify `gaze_dwell` message handling for action_type field
    - Extract `action_type` from incoming `gaze_dwell` messages
    - If `action_type` field is missing, use the currently stored active dwell action type
    - Pass `action_type` to `FusionEngine.on_gaze_dwell(x, y, action_type)`
    - _Requirements: 7.1, 7.5_

  - [x] 1.4 Extend welcome/status message with `active_dwell_action` field
    - Include `active_dwell_action` in the welcome status message sent on new connections
    - Default to "left_click" if no prior selection
    - _Requirements: 7.6_

  - [x] 1.5 Write property test for WebSocket protocol correctness (Hypothesis)
    - **Property 17: WebSocket set_dwell_action protocol correctness**
    - Generate valid/invalid action_type strings and verify ack responses and state changes
    - Generate gaze_dwell messages with/without action_type field and verify fallback behavior
    - **Validates: Requirements 7.2, 7.4, 7.5**

- [x] 2. PC-side FusionEngine action-type-aware dwell routing
  - [x] 2.1 Implement `on_gaze_dwell(x, y, action_type)` with action routing
    - Modify existing `on_gaze_dwell` signature to accept `action_type` parameter (default: "left_click")
    - Map action types to Command objects:
      - "left_click" → Command(action="CLICK", params={}, gaze_coords=(px_x, px_y))
      - "right_click" → Command(action="CLICK", params={"button":"right"}, gaze_coords=(px_x, px_y))
      - "double_click" → Command(action="CLICK", params={"clicks":"2"}, gaze_coords=(px_x, px_y))
      - "drag_start" → Command(action="MOUSEDOWN", params={}, gaze_coords=(px_x, px_y))
      - "drag_end" → Command(action="MOUSEUP", params={}, gaze_coords=(px_x, px_y))
    - Compute pixel coords: `px_x = round(clamp(x, 0.0, 1.0) * screen_width)`, same for y
    - Set source="gaze_dwell" on all emitted Commands
    - Log warning and discard for unrecognized action_type values
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8_

  - [x] 2.2 Add drag state tracking to FusionEngine
    - Add `_drag_active: bool` state field
    - Set `_drag_active = True` when drag_start fires
    - Set `_drag_active = False` when drag_end fires
    - Add 30-second safety timeout: if no drag_end received, auto-release (emit MOUSEUP) and reset
    - _Requirements: 1.3, 1.4_

  - [x] 2.3 Add feature toggle enforcement to dwell routing
    - Add `_feature_toggles` dict with keys: "gaze_dwell_click", "gaze_dwell_right_click", "gaze_dwell_double_click", "gaze_dwell_drag", "edge_scroll", "gaze_cursor_mode"
    - Check toggle state before emitting any Command in `on_gaze_dwell`
    - Map action_type to corresponding toggle key and discard if disabled
    - Implement `set_feature_toggle(feature, enabled)` method
    - _Requirements: 4.2_

  - [x] 2.4 Write property test for action-to-Command mapping (Hypothesis)
    - **Property 1: Dwell action-to-Command mapping**
    - Generate random valid action types and normalized coordinates in [0.0, 1.0]²
    - Verify correct Command action verb, params, source, and gaze_coords
    - **Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.6**

  - [x] 2.5 Write property test for coordinate clamping (Hypothesis)
    - **Property 2: Coordinate clamping invariant**
    - Generate floats in [-1.0, 2.0] range for both x and y
    - Verify gaze_coords always within [0, screen_width] × [0, screen_height]
    - **Validates: Requirements 2.8**

  - [x] 2.6 Write property test for invalid action type rejection (Hypothesis)
    - **Property 3: Invalid action type rejection**
    - Generate arbitrary strings excluding the valid set
    - Verify no Command emitted and warning logged
    - **Validates: Requirements 2.7**

  - [x] 2.7 Write property test for feature toggle enforcement (Hypothesis)
    - **Property 11: Feature toggle enforcement**
    - Generate random toggle states and incoming events
    - Verify disabled features produce no Commands and don't alter other feature state
    - **Validates: Requirements 3.5, 4.2**

- [x] 3. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 4. PC-side edge scroll detection and emission
  - [x] 4.1 Extend FusionConfig with edge scroll parameters
    - Add fields: `edge_scroll_zone_pct` (0.08), `edge_scroll_delay_ms` (500), `edge_scroll_min_speed` (1), `edge_scroll_max_speed` (10)
    - Validate zone_pct in [0.02, 0.20] range
    - _Requirements: 3.2, 3.6_

  - [x] 4.2 Implement `_check_edge_scroll()` in FusionEngine
    - Add state: `_edge_scroll_active`, `_edge_scroll_start`, `_edge_scroll_direction`
    - On each tick with gaze data, check if gaze is in any edge zone (top/bottom/left/right)
    - Edge zone boundary: `edge_scroll_zone_pct × screen_dimension` pixels from each edge
    - Start activation delay timer when gaze enters zone
    - After delay elapsed, emit SCROLL Command per tick with direction matching occupied zone
    - Stop immediately (within one tick) when gaze leaves all edge zones
    - _Requirements: 3.1, 3.3_

  - [x] 4.3 Implement linear speed interpolation for edge scroll
    - Compute depth into zone as fraction: 0.0 at inner boundary, 1.0 at screen edge
    - Scroll speed = `min_speed + depth * (max_speed - min_speed)`, rounded to int
    - Set `clicks` param in SCROLL Command to computed speed
    - _Requirements: 3.4_

  - [x] 4.4 Implement corner diagonal scroll
    - Detect when gaze is in two overlapping edge zones (corner regions)
    - Emit SCROLL Commands combining both directions (e.g., "right" + "down")
    - _Requirements: 3.7_

  - [x] 4.5 Wire edge scroll to feature toggle
    - Check `_feature_toggles["edge_scroll"]` before processing edge scroll
    - When disabled, skip all edge scroll computation but allow other scroll sources
    - _Requirements: 3.5_

  - [x] 4.6 Write property tests for edge scroll behavior (Hypothesis)
    - **Property 7: Edge scroll activation after delay**
    - **Property 8: Edge scroll deactivation within one tick**
    - **Property 9: Edge scroll speed linear interpolation**
    - **Property 10: Corner diagonal scroll**
    - **Property 18: Edge scroll zone boundary computation**
    - Generate gaze position sequences, timing values, and zone percentages
    - **Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.7**

- [x] 5. PC-side gaze-to-cursor mode
  - [x] 5.1 Extend FusionConfig with gaze-to-cursor parameters
    - Add fields: `gaze_cursor_ema_alpha` (0.3), `gaze_cursor_max_jump_pct` (0.05), `gaze_cursor_conf_min` (0.55), `gaze_cursor_lost_timeout_s` (0.5)
    - _Requirements: 5.3, 5.5_

  - [x] 5.2 Implement `_apply_gaze_cursor()` in FusionEngine
    - Add state: `_gaze_cursor_last`, `_gaze_cursor_ema`
    - Apply EMA smoothing: `ema = alpha * raw + (1 - alpha) * prev_ema`
    - Clamp frame-to-frame displacement to max 5% of screen diagonal
    - Move cursor to smoothed position via Command(action="CLICK") or direct pyautogui call
    - Hold cursor at last position when confidence < threshold
    - Hold cursor at last position when gaze lost > 500ms, log warning
    - _Requirements: 5.1, 5.3, 5.5, 5.6_

  - [x] 5.3 Implement tilt/head suppression when gaze-to-cursor active
    - When `gaze_cursor_mode` enabled, suppress tilt (priority 6) and head (priority 7) cursor movement
    - Both remain suppressed even when gaze is temporarily lost
    - Restore tilt/head immediately when mode is disabled
    - _Requirements: 5.2, 5.4_

  - [x] 5.4 Wire dwell firing to smoothed cursor position
    - When gaze-to-cursor mode is active, dwell events use EMA-smoothed position (not raw gaze)
    - Dwell timer continues running normally in this mode
    - _Requirements: 5.7_

  - [x] 5.5 Write property tests for gaze-to-cursor (Hypothesis)
    - **Property 13: Gaze-to-cursor EMA displacement cap**
    - **Property 14: Gaze-to-cursor suppresses tilt and head**
    - **Property 15: Low-confidence gaze holds cursor position**
    - **Property 16: Dwell fires at smoothed cursor position**
    - Generate random gaze sequences, confidence values, and tilt/head events
    - **Validates: Requirements 5.2, 5.3, 5.5, 5.7**

- [x] 6. Checkpoint - Ensure all PC-side tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 7. iPad-side DwellActionSelector state machine
  - [x] 7.1 Add DwellActionType enum and state properties to SettingsStore
    - Define `DwellActionType` enum: leftClick, rightClick, doubleClick, dragStart, dragEnd
    - Add `@Published var activeDwellAction: DwellActionType` (persisted to UserDefaults)
    - Add `@Published var oneShotEnabled: Bool` (default: false)
    - Add `@Published var isDragging: Bool` (transient, not persisted)
    - _Requirements: 1.1, 1.2_

  - [x] 7.2 Implement drag state machine transitions
    - When dragStart fires: auto-set `activeDwellAction = .dragEnd`, set `isDragging = true`
    - When dragEnd fires: auto-set `activeDwellAction = .leftClick`, set `isDragging = false`
    - Send updated action type via WebSocket after each transition
    - _Requirements: 1.3, 1.4_

  - [x] 7.3 Implement one-shot reset behavior
    - When `oneShotEnabled == true` and a non-default action (rightClick, doubleClick) fires
    - Reset `activeDwellAction` to `.leftClick` after single execution
    - _Requirements: 1.5_

  - [x] 7.4 Modify GazeTracker.swift to include action_type in dwell messages
    - `sendGazeDwell()` reads `activeDwellAction` from SettingsStore
    - Include `action_type` field (raw string value) in gaze_dwell WebSocket message
    - After sending dwell, apply state transitions (drag auto-advance, one-shot reset)
    - Suppress dwell timer when all dwell actions are disabled (gaze stream continues)
    - _Requirements: 7.1, 4.5_

  - [x] 7.5 Write property tests for drag state machine (SwiftCheck/swift-testing)
    - **Property 4: Drag state machine transitions**
    - Generate arbitrary initial states, verify drag-start → drag-end → left-click transitions
    - **Validates: Requirements 1.3, 1.4**

  - [x] 7.6 Write property test for one-shot reset (SwiftCheck/swift-testing)
    - **Property 5: One-shot reset behavior**
    - Generate non-default action types with one-shot enabled, verify reset after single fire
    - **Validates: Requirements 1.5**

- [x] 8. iPad-side DwellActionToolbar UI
  - [x] 8.1 Create DwellActionToolbar SwiftUI view
    - Render exactly 5 buttons: left-click, right-click, double-click, drag, scroll-toggle
    - Minimum touch target: 44×44 pt per button
    - Highlight active action with differentiated background fill and ≥2pt border
    - Inactive buttons use unfilled/muted background
    - _Requirements: 6.1, 6.2, 6.4_

  - [x] 8.2 Implement toolbar positioning and persistence
    - Support three positions: top-anchored, bottom-anchored, floating
    - Floating position constrained to safe area bounds
    - Persist selected position to UserDefaults
    - Default to bottom-anchored on fresh install
    - _Requirements: 6.5, 6.8_

  - [x] 8.3 Implement drag state indicator
    - Show pulsing animation on drag button when `isDragging == true`
    - Layer animation on top of active-highlight styling
    - _Requirements: 6.6_

  - [x] 8.4 Wire toolbar taps to WebSocket and handle disconnection
    - On tap: send `set_dwell_action` message via WebSocketManager
    - If WebSocket disconnected: queue action type locally, send on reconnection
    - Toolbar remains visible and interactive regardless of connection state
    - _Requirements: 6.3, 6.7_

- [x] 9. iPad-side feature toggle system
  - [x] 9.1 Add feature toggle properties to SettingsStore
    - Add 6 `@Published` Bool properties (all default true):
      - `gazeDwellClickEnabled`, `gazeDwellRightClickEnabled`, `gazeDwellDoubleClickEnabled`
      - `gazeDwellDragEnabled`, `edgeScrollEnabled`, `gazeCursorModeEnabled`
    - Persist all to UserDefaults
    - Add computed `allDwellActionsDisabled` property
    - _Requirements: 4.1, 4.6_

  - [x] 9.2 Implement toggle sync via WebSocket
    - Send `set_feature_toggle` message when any toggle changes
    - Queue changes locally if WebSocket disconnected, sync on reconnection
    - _Requirements: 4.3, 4.7_

  - [x] 9.3 Write property test for toggle persistence round-trip (SwiftCheck/swift-testing)
    - **Property 12: Toggle persistence round-trip**
    - Generate random combinations of 6 boolean toggle states
    - Persist to UserDefaults, reload, verify all match
    - **Validates: Requirements 4.1, 4.6**

- [x] 10. Checkpoint - Ensure all iPad-side tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 11. Integration wiring and end-to-end validation
  - [x] 11.1 Wire FusionEngine edge scroll and gaze-to-cursor into the 60Hz tick loop
    - Call `_check_edge_scroll()` on every tick when gaze data is available and edge_scroll enabled
    - Call `_apply_gaze_cursor()` on every tick when gaze_cursor_mode enabled
    - Ensure dwell routing, edge scroll, and gaze-to-cursor coexist without conflicts
    - Respect tick budget: skip edge scroll if tick exceeds 16ms
    - _Requirements: 3.1, 5.1_

  - [x] 11.2 Wire iPad DwellActionToolbar into ContentView and SensorManager
    - Add DwellActionToolbar to the main ContentView layout
    - Connect to SettingsStore and WebSocketManager
    - Ensure SensorManager suppresses dwell timer when `allDwellActionsDisabled`
    - _Requirements: 6.1, 4.5_

  - [x] 11.3 Write integration tests for end-to-end dwell action flow
    - Test: iPad tap → WebSocket message → IPadBridge → FusionEngine → Command emission
    - Test: Toggle sync iPad → PC → verify FusionEngine state
    - Test: Reconnection during drag → verify state consistency
    - **Validates: Requirements 7.2, 4.3**

- [x] 12. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from the design document
- Unit tests validate specific examples and edge cases
- PC-side uses Hypothesis for property-based testing, iPad-side uses SwiftCheck or swift-testing with randomization
- The WebSocket protocol extension (task 1) is the foundation — both iPad and PC tasks depend on it
- Voice command integration (Requirements 1.6, 1.7, 4.4) leverages the existing KeywordListener infrastructure and is wired through the same `set_dwell_action` path

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2", "4.1", "5.1", "7.1", "9.1"] },
    { "id": 1, "tasks": ["1.3", "1.4", "2.1", "4.2", "5.2", "7.2", "7.3", "8.1", "9.2"] },
    { "id": 2, "tasks": ["2.2", "2.3", "4.3", "4.4", "4.5", "5.3", "5.4", "7.4", "8.2", "8.3"] },
    { "id": 3, "tasks": ["1.5", "2.4", "2.5", "2.6", "2.7", "4.6", "5.5", "7.5", "7.6", "8.4", "9.3"] },
    { "id": 4, "tasks": ["11.1", "11.2"] },
    { "id": 5, "tasks": ["11.3"] }
  ]
}
```
