# Requirements Document

## Introduction

Replace the TiltSensor's velocity-based cursor control (streaming rotation-rate deltas that the PC integrates) with a position-mapped model where the iPad's physical tilt angle maps directly to an absolute cursor position on the PC screen. This reduces fatigue for users with arthritis by eliminating the need to sustain a tilt to keep the cursor moving — the cursor simply rests wherever the iPad is angled.

## Glossary

- **TiltSensor**: The iPad-side Swift class that reads Core Motion device motion data and sends tilt messages to the PC over WebSocket.
- **FusionEngine**: The PC-side Python class that receives sensor data and drives cursor movement via pyautogui.
- **Gravity_Vector**: The unit vector from `CMDeviceMotion.gravity` indicating the direction of gravity in the device's coordinate frame.
- **Neutral_Position**: The calibrated reference orientation of the iPad that maps to the center of the screen.
- **Tilt_Angle**: The angular displacement of the iPad from the Neutral_Position, decomposed into pitch (forward/back) and roll (left/right) components.
- **Tilt_Range**: The maximum angular displacement from Neutral_Position that maps to the edge of the screen, expressed in degrees.
- **Position_Message**: A WebSocket JSON message of type `tilt_position` containing normalized screen coordinates (0.0–1.0) derived from the current Tilt_Angle relative to the Neutral_Position.
- **Calibration_Gesture**: A user-initiated action (button press or voice command) that captures the current device orientation as the new Neutral_Position.
- **SettingsStore**: The iPad-side Swift class that persists user preferences to UserDefaults.
- **Screen_Coordinates**: Normalized values in the range [0.0, 1.0] representing a position on the PC screen, where (0.0, 0.0) is top-left and (1.0, 1.0) is bottom-right.

## Requirements

### Requirement 1: Position-Based Tilt Angle Computation

**User Story:** As a user with arthritis, I want the iPad's tilt angle to map directly to a screen position, so that I can hold the iPad at a comfortable angle and the cursor stays put without sustained effort.

#### Acceptance Criteria

1. WHEN device motion data is received, THE TiltSensor SHALL compute the current Tilt_Angle as the angular displacement from the stored Neutral_Position using the Gravity_Vector.
2. THE TiltSensor SHALL decompose the Tilt_Angle into a roll component (left/right cursor movement) and a pitch component (up/down cursor movement).
3. THE TiltSensor SHALL map the roll component linearly to a normalized X coordinate in the range [0.0, 1.0], where negative roll (tilt left) maps toward 0.0 and positive roll (tilt right) maps toward 1.0.
4. THE TiltSensor SHALL map the pitch component linearly to a normalized Y coordinate in the range [0.0, 1.0], where positive pitch (tilt forward/away) maps toward 0.0 (top) and negative pitch (tilt back/toward user) maps toward 1.0 (bottom).
5. THE TiltSensor SHALL clamp computed Screen_Coordinates to the range [0.0, 1.0] on both axes when the Tilt_Angle exceeds the configured Tilt_Range.

### Requirement 2: Neutral Position Calibration

**User Story:** As a user, I want to calibrate the neutral/center position by holding the iPad naturally and pressing a button, so that the system adapts to my comfortable resting posture.

#### Acceptance Criteria

1. WHEN the user triggers a Calibration_Gesture, THE TiltSensor SHALL capture the current Gravity_Vector orientation as the new Neutral_Position.
2. WHEN calibration completes, THE TiltSensor SHALL persist the Neutral_Position to the SettingsStore so it survives app restarts.
3. THE TiltSensor SHALL load the persisted Neutral_Position on startup and use it as the reference orientation.
4. IF no persisted Neutral_Position exists, THEN THE TiltSensor SHALL use a default Neutral_Position corresponding to the iPad held at 45 degrees (±1 degree tolerance) from horizontal (a common lap/desk angle).
5. WHEN calibration completes, THE TiltSensor SHALL immediately recompute Screen_Coordinates relative to the new Neutral_Position without requiring a sensor restart.

### Requirement 3: Tilt Range Configuration

**User Story:** As a user with limited range of motion, I want to configure how much tilt is needed to reach the screen edges, so that I can use the full screen without straining.

#### Acceptance Criteria

1. THE SettingsStore SHALL expose a `tiltRange` setting representing the maximum angular displacement (in degrees) from Neutral_Position that maps to the screen edge.
2. THE SettingsStore SHALL default `tiltRange` to 25 degrees.
3. THE SettingsStore SHALL constrain `tiltRange` to the range [5, 60] degrees.
4. WHEN `tiltRange` is changed, THE TiltSensor SHALL apply the new range immediately without requiring recalibration or sensor restart, even if this causes a sudden cursor position change.
5. WHEN the Tilt_Angle equals the configured Tilt_Range on an axis, THE TiltSensor SHALL output a Screen_Coordinate of 0.0 or 1.0 on that axis (full screen edge).

### Requirement 4: Position Message Protocol

**User Story:** As a developer, I want a clear WebSocket message format for position-based tilt, so that the PC side can set the cursor to an absolute position.

#### Acceptance Criteria

1. THE TiltSensor SHALL send Position_Messages with the JSON structure `{"type": "tilt_position", "x": <float>, "y": <float>}` where x and y are Screen_Coordinates in [0.0, 1.0].
2. THE TiltSensor SHALL send Position_Messages at exactly 60 Hz while tilt is enabled and the device is in motion.
3. WHEN the computed Screen_Coordinates have not changed by more than 0.001 on either axis since the last sent message, THE TiltSensor SHALL suppress the message to reduce network traffic.
4. THE TiltSensor SHALL NOT send the legacy `tilt` message type (with `rx`/`ry` rotation-rate deltas) when operating in position-mapped mode.

### Requirement 5: PC-Side Absolute Cursor Positioning

**User Story:** As a user, I want the PC cursor to move to the exact screen position indicated by my iPad's tilt angle, so that cursor placement is predictable and direct.

#### Acceptance Criteria

1. WHEN the FusionEngine receives a Position_Message, THE FusionEngine SHALL move the cursor to the absolute pixel position computed as `(x * screen_width, y * screen_height)`.
2. THE FusionEngine SHALL apply EMA smoothing (configurable alpha, default 0.4) to received Position_Messages before moving the cursor, to reduce jitter.
3. WHEN gaze-to-cursor mode is active and gaze data is recent, THE FusionEngine SHALL suppress tilt position updates (same suppression logic as the current velocity-based tilt).
4. WHEN gaze-to-cursor mode is active but gaze data is stale, THE FusionEngine SHALL allow tilt position updates as an escape hatch and clear the gaze EMA state.

### Requirement 6: Smoothing and Jitter Reduction

**User Story:** As a user, I want the cursor to move smoothly without jittering when I hold the iPad still, so that I can target UI elements precisely.

#### Acceptance Criteria

1. THE TiltSensor SHALL apply a dead zone around the Neutral_Position where small angular displacements (less than the configured `tiltDeadZone` in degrees, default 1.5 degrees) map to the screen center rather than producing micro-movements.
2. THE FusionEngine SHALL apply EMA smoothing to the received normalized coordinates with a configurable smoothing factor (alpha between 0.1 and 0.9, default 0.4).
3. WHEN the iPad is stationary (absolute angular velocity at or below 0.01 rad/s on both axes for 200ms), THE TiltSensor SHALL lock the output coordinates to the last stable value until motion resumes.

### Requirement 7: Inversion Setting

**User Story:** As a user, I want to optionally invert the tilt-to-cursor mapping on either axis, so that I can choose whichever direction feels natural.

#### Acceptance Criteria

1. WHEN `tiltInverted` is enabled in SettingsStore, THE TiltSensor SHALL invert both axes of the position mapping (left tilt maps to right side of screen, and forward tilt maps to bottom of screen).
2. WHEN `tiltInverted` is changed, THE TiltSensor SHALL apply the new setting immediately without requiring recalibration.

### Requirement 8: Impulse Tap Preservation

**User Story:** As a user, I want the table-tap detection feature to continue working alongside position-based tilt, so that I retain my quick-tap input method.

#### Acceptance Criteria

1. THE TiltSensor SHALL continue detecting impulse taps (sharp acceleration spikes) using the existing `userAcceleration` magnitude threshold and cooldown logic.
2. THE TiltSensor SHALL send `tilt_tap` messages independently of the position-mapping system.
3. THE TiltSensor SHALL NOT allow impulse detection to interfere with position computation (acceleration spikes do not corrupt the Gravity_Vector-based angle calculation).

### Requirement 9: Backward Compatibility

**User Story:** As a developer, I want the PC side to handle both the legacy velocity-based messages and the new position-based messages gracefully during the transition period.

#### Acceptance Criteria

1. WHEN the FusionEngine receives a legacy `tilt` message (with `rx`/`ry`), THE FusionEngine SHALL process it using the existing velocity-based integration logic.
2. WHEN the FusionEngine receives a `tilt_position` message (with `x`/`y`), THE FusionEngine SHALL process it using absolute cursor positioning.
3. IF both message types are received within the same tick, THEN THE FusionEngine SHALL use the `tilt_position` message and completely ignore the legacy `tilt` message without processing it.
