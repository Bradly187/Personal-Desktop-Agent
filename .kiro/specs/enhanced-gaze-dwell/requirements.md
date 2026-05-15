# Requirements Document

## Introduction

Enhanced Gaze Dwell Actions extends the Personal Desktop Agent's gaze dwell system beyond single left-click to support right-click, double-click, drag, and edge-scroll actions. It also introduces a feature toggle system allowing the user to enable/disable gaze actions individually, and an optional gaze-to-cursor mode where the cursor follows gaze position. These enhancements close the gap with commercial eye-tracking products (Tobii Dynavox, Windows Eye Control, Apple iOS Eye Tracking) while preserving the existing graceful degradation and local-first architecture.

## Glossary

- **Dwell_Action_Selector**: The component (iPad-side UI or voice command) that determines which action type the next gaze dwell will fire
- **GazeTracker**: The existing ARKit-based gaze tracking class on iPad that detects stable gaze and fires dwell events (`GazeTracker.swift`)
- **FusionEngine**: The 60 Hz sensor fusion loop on PC that consumes gaze_dwell events and emits Commands (`fusion_engine.py`)
- **Feature_Toggle_Store**: The persistence layer for per-feature enable/disable state, extending the existing `SettingsStore.swift`
- **Edge_Scroll_Zone**: A configurable region at screen edges where sustained gaze triggers continuous scrolling
- **Dwell_Action**: One of the supported gaze dwell action types: left-click, right-click, double-click, drag-start, drag-end
- **Gaze_Cursor_Mode**: An optional mode where the desktop cursor position follows the user's gaze coordinates in real time
- **Command**: The sole DTO dataclass crossing pipeline boundaries, carrying action verb, source, and gaze coordinates
- **IPad_Bridge**: The WebSocket server on PC that receives sensor streams from the iPad app (`ipad_bridge.py`)

## Requirements

### Requirement 1: Dwell Action Type Selection

**User Story:** As a user with limited fine motor control, I want to select which action my next gaze dwell will perform, so that I can right-click, double-click, or drag without needing precise mouse manipulation.

#### Acceptance Criteria

1. THE Dwell_Action_Selector SHALL support the following action types: left-click, right-click, double-click, drag-start, drag-end
2. WHEN the user selects a Dwell_Action type via the iPad UI, THE Dwell_Action_Selector SHALL persist that selection and display the currently active action type until the user changes it or a one-shot reset occurs
3. WHEN a drag-start dwell action fires (MOUSEDOWN sent at gaze coordinates), THE Dwell_Action_Selector SHALL automatically transition to drag-end mode without requiring additional user input
4. WHEN a drag-end action completes, THE Dwell_Action_Selector SHALL automatically reset to left-click mode regardless of the action type that was active before the drag sequence began
5. THE Dwell_Action_Selector SHALL provide a "one-shot" option, disabled by default, where non-default actions (right-click, double-click) reset to left-click after a single execution
6. WHEN the user issues a voice command naming a dwell action type (e.g., "right click mode", "double click mode", "drag mode") AND the system recognizes the command with sufficient confidence to confirm user intent, THE Dwell_Action_Selector SHALL switch to that action type within 600 milliseconds of recognition; false-positive recognitions (low confidence or unintended utterances) SHALL NOT trigger a mode switch
7. IF the user issues a voice command that does not match any supported dwell action type, THEN THE Dwell_Action_Selector SHALL remain in its current mode and provide an audible or visual indication that the command was not recognized
8. WHEN the user selects any dwell action type (including left-click), THE Dwell_Action_Selector SHALL immediately update the active action type display on the iPad UI so the user can confirm the current mode without recalling from memory, regardless of whether a dwell has been performed yet

### Requirement 2: Gaze Dwell Command Routing

**User Story:** As a user, I want gaze dwell events to execute the currently selected action type at the gaze coordinates, so that all dwell actions land precisely where I am looking.

#### Acceptance Criteria

1. WHEN a gaze_dwell event arrives with action type left-click, THE FusionEngine SHALL emit a Command with action "CLICK" and gaze_coords set to the dwell pixel coordinates computed as (normalized_x × screen_width, normalized_y × screen_height) rounded to the nearest integer
2. WHEN a gaze_dwell event arrives with action type right-click, THE FusionEngine SHALL emit a Command with action "CLICK", params containing button "right", and gaze_coords set to the dwell pixel coordinates computed as (normalized_x × screen_width, normalized_y × screen_height) rounded to the nearest integer
3. WHEN a gaze_dwell event arrives with action type double-click, THE FusionEngine SHALL emit a Command with action "CLICK", params containing clicks "2", and gaze_coords set to the dwell pixel coordinates computed as (normalized_x × screen_width, normalized_y × screen_height) rounded to the nearest integer
4. WHEN a gaze_dwell event arrives with action type drag-start, THE FusionEngine SHALL emit a Command with action "MOUSEDOWN" and gaze_coords set to the dwell pixel coordinates computed as (normalized_x × screen_width, normalized_y × screen_height) rounded to the nearest integer
5. WHEN a gaze_dwell event arrives with action type drag-end, THE FusionEngine SHALL emit a Command with action "MOUSEUP" and gaze_coords set to the dwell pixel coordinates computed as (normalized_x × screen_width, normalized_y × screen_height) rounded to the nearest integer
6. THE FusionEngine SHALL preserve the existing bypass-all-gates routing for all gaze dwell action types (priority 3 in the fusion priority order), emitting Commands with source "gaze_dwell" so the HybridCoordinator skips all four confidence gates
7. IF a gaze_dwell event arrives with an action type not in the set {left-click, right-click, double-click, drag-start, drag-end}, THEN THE FusionEngine SHALL discard the event without emitting a Command and log a warning containing the unrecognized action type value
8. IF a gaze_dwell event arrives with normalized coordinates outside the range [0.0, 1.0] for either axis, THEN THE FusionEngine SHALL clamp the coordinates to the range [0.0, 1.0] before computing pixel coordinates

### Requirement 3: Edge Scroll by Gaze

**User Story:** As a user, I want the screen to scroll automatically when I look at the edges, so that I can navigate long documents and web pages without switching input modalities.

#### Acceptance Criteria

1. WHILE the user's gaze rests within an Edge_Scroll_Zone for longer than the configured activation delay, THE FusionEngine SHALL emit one SCROLL Command per tick (60 Hz) in the corresponding direction (up, down, left, or right based on which edge zone the gaze occupies)
2. THE Edge_Scroll_Zone SHALL be configurable as a percentage of screen dimensions between 2% and 20% (default: 8% from each edge), applied independently to all four screen edges
3. WHEN the user's gaze leaves the Edge_Scroll_Zone, THE FusionEngine SHALL stop emitting SCROLL Commands within one tick (16ms at 60 Hz)
4. THE FusionEngine SHALL scale scroll speed linearly from 1 scroll unit per tick at the inner boundary of the Edge_Scroll_Zone to 10 scroll units per tick at the screen edge
5. IF edge scrolling is disabled via Feature_Toggle_Store, THEN THE FusionEngine SHALL ignore gaze-based scroll triggers while allowing other scroll sources (touch commands, voice commands) to continue functioning normally
6. THE Edge_Scroll_Zone activation delay SHALL default to 500ms and be configurable between 200ms and 2000ms
7. IF the user's gaze rests in a corner region where two Edge_Scroll_Zones overlap AND edge scrolling is enabled, THEN THE FusionEngine SHALL emit SCROLL Commands combining both directions (diagonal scroll); IF edge scrolling is disabled, corner gaze SHALL be ignored for scrolling while other scroll sources remain active

### Requirement 4: Feature Toggle System

**User Story:** As a user whose capabilities vary day to day, I want to toggle individual gaze features on or off, so that I can customize my input experience based on current comfort and fatigue levels.

#### Acceptance Criteria

1. THE Feature_Toggle_Store SHALL persist the enabled/disabled state for each of the following features independently: gaze dwell click, gaze dwell right-click, gaze dwell double-click, gaze dwell drag, edge scroll, gaze-to-cursor mode — with all toggles defaulting to enabled on first launch
2. WHEN a feature is disabled, THE FusionEngine SHALL discard incoming events for that feature without emitting a Command and without altering the processing or state of other active features
3. THE Feature_Toggle_Store SHALL propagate a toggle state change from the originating device (iPad or PC) to the other device via the IPad_Bridge WebSocket connection within 500ms of the change, measured from write on the source to read-confirmation on the destination
4. WHEN the user issues a voice command matching the pattern "enable [feature name]" or "disable [feature name]" (where feature name matches one of the six toggleable features), THE Feature_Toggle_Store SHALL update the corresponding toggle state within 600ms of utterance end
5. IF all gaze dwell action types (click, right-click, double-click, drag) are explicitly disabled by the user, THEN THE GazeTracker SHALL continue streaming raw gaze coordinates but suppress dwell timer firing — edge scroll and gaze-to-cursor mode SHALL remain independently controllable by their own toggles; dwell timers SHALL NOT be suppressed in any other scenario (e.g., gaze loss, calibration)
6. THE Feature_Toggle_Store SHALL persist toggle state across app restarts using UserDefaults on iPad and the existing FusionConfig dataclass serialized to the PC config store
7. IF the WebSocket connection is unavailable when a toggle state changes, THEN THE Feature_Toggle_Store SHALL apply the change locally immediately and re-synchronize the pending state change when the connection is re-established

### Requirement 5: Gaze-to-Cursor Mode

**User Story:** As a user, I want an optional mode where my cursor follows my gaze, so that I can position the cursor by looking instead of using tilt or head tracking.

#### Acceptance Criteria

1. WHILE Gaze_Cursor_Mode is enabled, THE FusionEngine SHALL move the desktop cursor to the user's gaze coordinates on every tick where a stable gaze centroid is available
2. WHILE Gaze_Cursor_Mode is enabled, THE FusionEngine SHALL suppress both tilt-based cursor movement (priority 6) and head-based cursor movement (priority 7) together to prevent conflicting inputs; partial suppression of only one input type is not permitted, and both SHALL remain suppressed even when gaze tracking is temporarily lost, maintaining the cursor at its last known position
3. WHILE Gaze_Cursor_Mode is enabled, THE FusionEngine SHALL apply an exponential moving average with a configurable alpha (default: 0.3) to gaze-to-cursor coordinates before moving the cursor, such that frame-to-frame cursor displacement does not exceed 5% of screen diagonal per tick under steady gaze
4. WHEN Gaze_Cursor_Mode is disabled, THE FusionEngine SHALL immediately restore tilt and head cursor movement without requiring a restart
5. WHILE Gaze_Cursor_Mode is enabled, IF gaze tracking confidence drops below the configured minimum threshold (default: 0.55), THEN THE FusionEngine SHALL hold the cursor at its last known position until confidence recovers above the threshold
6. WHILE Gaze_Cursor_Mode is enabled, IF gaze tracking is lost entirely for more than 500 milliseconds, THEN THE FusionEngine SHALL hold the cursor at its last known position and log a warning, without disabling the mode
7. WHILE Gaze_Cursor_Mode is enabled, THE FusionEngine SHALL continue running the gaze dwell timer and fire the configured dwell action at the smoothed cursor position when the dwell duration (default: 1.0 second) elapses

### Requirement 6: iPad Dwell Action UI

**User Story:** As a user, I want a visible toolbar on the iPad showing available dwell action types, so that I can see and change the current mode with a single large-target tap.

#### Acceptance Criteria

1. THE iPad app SHALL display a Dwell Action toolbar with exactly one button per supported action type: left-click, right-click, double-click, drag, and scroll-mode toggle (5 buttons total)
2. THE Dwell Action toolbar SHALL highlight the currently active action type by rendering its button with a visually differentiated background fill and a border width of at least 2 points, while inactive buttons use an unfilled or muted background
3. WHEN the user taps a Dwell Action button, THE iPad app SHALL send the selected action type to the PC via the existing WebSocket connection using the "set_dwell_action" message type defined in Requirement 7
4. THE Dwell Action toolbar buttons SHALL have a minimum touch target size of 44x44 points as required by iOS accessibility guidelines
5. THE Dwell Action toolbar SHALL be positionable by the user in one of three positions (top-anchored, bottom-anchored, or floating), with the floating position constrained to remain fully within the safe area bounds, and THE iPad app SHALL persist the selected position across app restarts via UserDefaults
6. WHILE a drag operation is in progress, THE Dwell Action toolbar SHALL display a "dragging" state indicator on the drag button by rendering both the active-highlight styling (differentiated background fill and 2-point border) and an additional pulsing animation layered on top; this animation SHALL appear during any active drag operation regardless of whether drag mode remains the selected action type
7. IF the WebSocket connection is not in the connected state, THEN THE Dwell Action toolbar SHALL remain visible and accept taps, queuing the selected action type locally so it is sent when the connection is re-established; queuing SHALL only occur due to WebSocket disconnection, not toolbar visibility state
8. WHEN the iPad app launches and no prior toolbar position has been persisted, THE Dwell Action toolbar SHALL default to the bottom-anchored position regardless of other system preferences or accessibility settings

### Requirement 7: WebSocket Protocol Extension

**User Story:** As a developer, I want the WebSocket protocol to carry dwell action type information, so that the iPad and PC stay synchronized on which action the next dwell will perform.

#### Acceptance Criteria

1. THE gaze_dwell WebSocket message SHALL include an "action_type" string field whose value is one of: "left_click", "right_click", "double_click", "drag_start", "drag_end"
2. WHEN the iPad sends a "set_dwell_action" message containing a "action_type" field with one of the five valid values, THE IPad_Bridge SHALL update the active dwell action type used for subsequent gaze_dwell processing and respond with an "ack" message containing the fields "type": "ack", "status": "ok", and "action_type" echoing the accepted value, within 100ms of receipt
3. WHEN the iPad sends a "set_feature_toggle" message containing a "feature" string field and an "enabled" boolean field, THE IPad_Bridge SHALL update the named feature's enabled state and respond with an "ack" message containing "type": "ack", "status": "ok", "feature" echoing the feature name, and "enabled" echoing the new state, within 100ms of receipt
4. IF a "set_dwell_action" message contains an action_type value not in the set {"left_click", "right_click", "double_click", "drag_start", "drag_end"}, THEN THE IPad_Bridge SHALL respond with an "ack" message with "status": "error" and an "error" field indicating the unrecognized value, and SHALL retain the previously active dwell action type unchanged
5. THE WebSocket protocol extension SHALL be backward-compatible — a gaze_dwell message received without an "action_type" field SHALL be processed using the currently active dwell action type (respecting the user's selection rather than defaulting to left-click)
6. WHEN a new WebSocket connection is established, THE IPad_Bridge SHALL include the current active dwell action type in the welcome status message as an "active_dwell_action" field defaulting to "left_click" if no prior selection has been made; IF the welcome message fails to send, the connection SHALL remain open and operational
