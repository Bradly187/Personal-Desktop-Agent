# Requirements Document

> **⚠️ Partially superseded.** This spec predates the 2026-05-30 removal of eye-gaze and
> head-pose control (the standard iPad lacks the required TrueDepth sensor). **Tilt is the only
> remaining cursor-driving sensor** — all Gaze_Tracker / Head_Tracker requirements below are
> retained for history only and are **not active**. See `specs/ipad-sensor-focus/` for the
> current sensor design.

## Introduction

This specification defines refinements to the three cursor-driving sensors (tilt/gyromouse, gaze tracker, head tracker) to align with established commercial and academic patterns for digital accessibility aids. The refinements address smoothing quality, transfer functions, axis mapping correctness, re-centering mechanics, and toggle ergonomics — informed by commercial AT devices (HeadMouse Nano, GlassOuse, Tobii), gaming gyro implementations (JibbSmart/JoyShockMapper, Steam Input, Splatoon), and academic HCI research (1-Euro filter, Fitts' Law, pointer acceleration studies).

## Glossary

- **Fusion_Engine**: The PC-side Python component (`fusion_engine.py`) that receives sensor data over WebSocket and drives cursor movement via pyautogui.
- **Tilt_Sensor**: The iPad-side Swift component (`TiltSensor.swift`) that reads Core Motion data and sends tilt events to the PC.
- **Gaze_Tracker**: The iPad-side Swift component (`GazeTracker.swift`) that extracts eye gaze direction deltas from ARKit face anchors.
- **Head_Tracker**: The iPad-side Swift component (`HeadTracker.swift`) that extracts head pitch/yaw deltas from ARKit face anchors.
- **One_Euro_Filter**: An adaptive low-pass filter (Casiez et al., 2012) that applies strong smoothing at low speeds and minimal smoothing at high speeds, reducing jitter without adding latency during fast movements.
- **Power_Curve**: A non-linear transfer function of the form `output = sign(input) * |input|^exponent` that provides fine control at small deflections and fast traversal at large deflections.
- **Ratcheting**: A re-centering mechanism analogous to lifting and replacing a mouse — the user triggers a reset that re-maps the current physical orientation to screen center without moving the cursor.
- **Dead_Zone_Ramp**: A smooth transition from zero output to full output near the dead zone boundary, replacing a hard threshold that causes discontinuous jumps.
- **Gyro_Bias**: A slowly-drifting offset in gyroscope readings caused by sensor temperature and electronics, which accumulates as cursor drift in velocity mode if uncorrected.
- **Transfer_Function**: A mathematical mapping from raw sensor input magnitude to cursor movement magnitude.
- **EMA**: Exponential Moving Average — a fixed-alpha low-pass filter currently used for smoothing throughout the system.
- **Toggle**: A user action that enables, disables, or switches the mode of a sensor input.
- **Sensor_Manager**: The iPad-side Swift component (`SensorManager.swift`) that manages lifecycle of all sensors.

## Requirements

### Requirement 1: Replace EMA Smoothing with 1-Euro Filter (Tilt Velocity Mode)

**User Story:** As a user with RA, I want tilt cursor movement to feel responsive during intentional movements but stable when holding still, so that I can point accurately without fighting jitter or lag.

#### Acceptance Criteria

1. WHEN the Tilt_Sensor operates in velocity mode, THE Fusion_Engine SHALL apply a One_Euro_Filter independently to each axis (pitch and yaw) of the rotation rate values before computing cursor displacement, replacing any prior EMA smoothing on those axes.
2. THE One_Euro_Filter SHALL accept configurable parameters: minimum cutoff frequency (default 1.0 Hz, valid range 0.1–10.0 Hz), speed coefficient beta (default 0.007, valid range 0.0–1.0), and derivative cutoff frequency (default 1.0 Hz, valid range 0.1–10.0 Hz).
3. WHEN the angular velocity is below 0.5 rad/s, THE One_Euro_Filter SHALL produce a cutoff frequency within 20% of the minimum cutoff frequency, providing strong jitter reduction.
4. WHEN the angular velocity exceeds 3.0 rad/s, THE One_Euro_Filter SHALL increase the cutoff frequency proportionally to beta times the speed, resulting in no more than 2 frames of additional latency compared to unfiltered output.
5. THE Fusion_Engine SHALL apply the One_Euro_Filter exactly once per sample in the tilt velocity pipeline — no additional low-pass filter (including EMA) SHALL be applied to the same rotation rate values before or after the One_Euro_Filter.
6. IF any One_Euro_Filter parameter is set outside its valid range, THEN THE Fusion_Engine SHALL clamp the value to the nearest valid bound and log a warning.

### Requirement 2: Replace EMA Smoothing with 1-Euro Filter (Tilt Position Mode)

**User Story:** As a user with RA, I want position-mapped tilt to feel smooth when holding a position but track quickly when I deliberately tilt to a new target, so that I can dwell on targets without cursor wobble.

#### Acceptance Criteria

1. WHEN the Tilt_Sensor operates in position mode, THE Fusion_Engine SHALL apply a One_Euro_Filter independently to each axis of the normalized (x, y) coordinates before converting to pixel positions.
2. THE One_Euro_Filter for position mode SHALL use separate configurable parameters from velocity mode (default minimum cutoff 0.5 Hz, beta 0.004, derivative cutoff 1.0 Hz).
3. WHEN the position change rate is below 0.02 normalized units per sample at the 60 Hz sensor update rate, THE One_Euro_Filter SHALL suppress jitter to less than 1 pixel of cursor movement on each axis.
4. WHEN the position change rate exceeds 0.1 normalized units per sample at the 60 Hz sensor update rate, THE One_Euro_Filter SHALL track the input with less than 3 samples of delay (less than 50ms at 60 Hz).
5. WHEN the Tilt_Sensor first enters position mode or resumes after being disabled, THE Fusion_Engine SHALL initialize the One_Euro_Filter state with the first received sample so that no cursor jump occurs from stale filter state.

### Requirement 3: Replace EMA Smoothing with 1-Euro Filter (Gaze Tracker)

**User Story:** As a user, I want gaze-driven cursor movement to be smooth during fixation but responsive during saccades, so that I can hold a target for dwell-click without the cursor dancing.

#### Acceptance Criteria

1. WHEN the Gaze_Tracker computes gaze direction deltas, THE Gaze_Tracker SHALL apply a One_Euro_Filter independently to the horizontal and vertical delta values, replacing the existing EMA smoothing, before sending them over WebSocket.
2. THE One_Euro_Filter for gaze SHALL use configurable parameters: minimum cutoff frequency (default 1.5 Hz), speed coefficient beta (default 0.01), and derivative cutoff frequency (default 1.0 Hz).
3. WHEN the gaze delta magnitude is below 0.003 radians per frame at 60 fps, THE One_Euro_Filter SHALL suppress the output to less than 0.0005 radians, preventing fixation jitter from reaching the cursor.
4. WHEN a saccade produces gaze deltas exceeding 0.02 radians per frame at 60 fps, THE One_Euro_Filter SHALL pass the movement with less than 2 frames (33ms) of additional latency compared to unfiltered output.
5. WHEN the One_Euro_Filter is active, THE Gaze_Tracker SHALL remove the existing hard dead zone threshold, relying on the filter's adaptive cutoff to suppress fixation noise.
6. IF no gaze data is received for more than 3 consecutive frames (due to blinks or tracking loss), THEN THE Gaze_Tracker SHALL reset the One_Euro_Filter state so that the next valid frame does not produce a large spurious delta from stale history.

### Requirement 4: Replace EMA Smoothing with 1-Euro Filter (Head Tracker)

**User Story:** As a user, I want head-driven cursor movement to be stable when my head is still but responsive when I deliberately turn, so that I can use head tracking as a coarse pointing device without drift.

#### Acceptance Criteria

1. WHEN the Head_Tracker computes pitch/yaw deltas, THE Head_Tracker SHALL apply a One_Euro_Filter to each axis independently before sending over WebSocket, replacing the existing EMA smoothing factor.
2. THE One_Euro_Filter for head tracking SHALL accept configurable parameters: minimum cutoff frequency (default 1.2 Hz), speed coefficient beta (default 0.008), and derivative cutoff frequency (default 1.0 Hz).
3. WHEN head angular velocity is below 1 degree per second, THE One_Euro_Filter SHALL produce output that results in less than 0.5 pixels per second of cursor movement, preventing micro-drift from reaching the cursor.
4. WHEN head angular velocity exceeds 15 degrees per second, THE One_Euro_Filter SHALL track the movement with less than 50ms of additional phase delay compared to unfiltered output.

### Requirement 5: Power-Curve Transfer Function for Tilt Position Mode

**User Story:** As a user with RA, I want small tilts to produce fine cursor adjustments and large tilts to traverse the screen quickly, so that I can both aim precisely and navigate efficiently without changing sensitivity settings.

#### Acceptance Criteria

1. WHEN the Tilt_Sensor operates in position mode, THE Fusion_Engine SHALL apply the One_Euro_Filter to the normalized tilt displacement first, then apply the Power_Curve transfer function to the filtered result, before mapping to screen coordinates.
2. THE Power_Curve SHALL use a configurable exponent (default 1.5, minimum 1.0, maximum 3.0) where values greater than 1.0 provide sub-linear sensitivity near center and super-linear sensitivity at extremes.
3. WHEN the tilt displacement is less than 20% of the configured tilt range, THE Power_Curve SHALL produce cursor displacement less than 10% of the screen dimension, enabling fine targeting.
4. WHEN the tilt displacement is greater than 80% of the configured tilt range, THE Power_Curve SHALL produce cursor displacement greater than 70% of the screen dimension, enabling fast traversal.
5. THE Power_Curve SHALL preserve the sign of the input (tilt direction maps to the same cursor direction regardless of exponent value).
6. THE Power_Curve SHALL be applied independently to each axis (x and y), using the per-axis normalized displacement magnitude so that diagonal tilts produce correct diagonal cursor movement.
7. WHEN the normalized tilt displacement on an axis is zero, THE Power_Curve SHALL produce zero cursor displacement on that axis.

### Requirement 6: Ratcheting / Re-centering for Tilt Position Mode

**User Story:** As a user with RA, I want to reset my tilt neutral point without moving the cursor, so that I can reposition my hands or the iPad without losing my cursor location — like lifting and replacing a mouse.

#### Acceptance Criteria

1. WHEN the user triggers a ratchet action, THE Tilt_Sensor SHALL capture the current gravity vector as the new neutral reference point and THE Fusion_Engine SHALL produce zero cursor displacement for that frame, regardless of the difference between old and new neutral.
2. WHEN a ratchet action is triggered, THE Fusion_Engine SHALL hold the cursor at its current pixel position until the next tilt input produces a displacement exceeding the configured dead zone threshold (default: 2 degrees from the new neutral) after passing through the Dead_Zone_Ramp.
3. THE ratchet action SHALL be triggerable via a dedicated iPad UI button and via a configurable sound action (default: tongue click), and THE iPad UI SHALL provide a non-startling visual confirmation (brief icon highlight lasting 200–400ms, no sound, no modal) when the ratchet is accepted.
4. WHEN the ratchet action is triggered multiple times within 500ms, THE Tilt_Sensor SHALL accept only the first trigger and ignore subsequent triggers (debounce).
5. IF the Tilt_Sensor is not currently active, THEN THE system SHALL ignore ratchet triggers silently without error.
6. IF the device angular velocity exceeds 0.5 rad/s on any axis at the moment of ratchet trigger, THEN THE Tilt_Sensor SHALL delay the gravity vector capture until angular velocity drops below 0.5 rad/s or 300ms elapses (whichever comes first), then capture the gravity vector at that point.

### Requirement 7: Velocity-Mode Gyro Bias Calibration

**User Story:** As a user, I want the velocity-mode tilt cursor to remain stationary when I hold the iPad still, so that accumulated gyro drift does not cause the cursor to creep across the screen.

#### Acceptance Criteria

1. WHEN the Tilt_Sensor operates in velocity mode and the device is stationary (angular velocity below 0.02 rad/s on all axes for at least 1 second), THE Fusion_Engine SHALL estimate the current gyro bias by averaging the rotation rate samples collected during that stationary period (minimum 50 samples, maximum 200 samples).
2. THE Fusion_Engine SHALL subtract the estimated gyro bias from all subsequent rotation rate values before applying the transfer function.
3. WHEN the estimated bias changes by more than 0.005 rad/s from the previous estimate, THE Fusion_Engine SHALL update the applied bias using linear interpolation from the old value to the new value over 500ms to avoid a sudden cursor jump.
4. WHILE the Tilt_Sensor is operating in velocity mode, THE Fusion_Engine SHALL re-evaluate the gyro bias estimate each time a new stationary period is detected, without requiring explicit user action.
5. IF the device transitions from stationary to moving (angular velocity exceeds 0.02 rad/s on any axis), THEN THE Fusion_Engine SHALL freeze the bias estimate at its current value until the next stationary period is detected.
6. IF no stationary period has been detected since the Tilt_Sensor was enabled in velocity mode, THEN THE Fusion_Engine SHALL apply a bias of zero and suppress cursor movement for angular velocities below 0.05 rad/s until the first calibration completes.

### Requirement 8: Replace Hard Dead Zone with Smooth Ramp (Tilt Velocity Mode)

**User Story:** As a user with RA, I want the transition from "no movement" to "cursor moving" to be gradual rather than an abrupt jump, so that I can make very small intentional movements without the cursor snapping unpredictably.

#### Acceptance Criteria

1. WHEN the tilt angular velocity transitions from below the dead zone threshold to above it, THE Fusion_Engine SHALL apply a smooth ramp function that transitions output from zero to full over a configurable ramp width (default: inner threshold times 1.5, configurable from 1.0 to 3.0 times the inner threshold).
2. THE Dead_Zone_Ramp SHALL produce zero output when input magnitude is below the inner threshold (default: 0.05 rad/s).
3. THE Dead_Zone_Ramp SHALL produce full output (equal to the transfer function result) when input magnitude exceeds the outer threshold (inner threshold plus ramp width, default outer threshold: 0.125 rad/s).
4. WHILE input magnitude is between the inner and outer thresholds, THE Dead_Zone_Ramp SHALL interpolate using a cubic hermite function (smoothstep: zero first-derivative at both the inner and outer boundaries).
5. FOR ALL input values, THE Dead_Zone_Ramp output SHALL be continuous — the difference in cursor velocity between any two adjacent sensor samples SHALL not exceed the value predicted by the ramp function evaluated at those samples plus 0.5 pixels per second.

### Requirement 9: Fix Inverted Vertical Axis on Gaze Tracker

**User Story:** As a user, I want looking down to move the cursor down and looking up to move the cursor up, so that gaze tracking matches my natural spatial expectation.

#### Acceptance Criteria

1. WHEN the user's gaze direction moves downward (eyes look toward the floor), THE Gaze_Tracker SHALL produce a positive vertical delta (dy > 0) that moves the cursor downward on screen.
2. WHEN the user's gaze direction moves upward (eyes look toward the ceiling), THE Gaze_Tracker SHALL produce a negative vertical delta (dy < 0) that moves the cursor upward on screen.
3. WHEN the iPad's physical orientation changes (portrait, landscape-left, landscape-right), THE Gaze_Tracker SHALL rotate the raw eye-transform delta components so that the vertical delta remains aligned with the physical world's up/down axis relative to gravity.
4. WHEN the gaze vertical delta is applied by the Fusion_Engine, THE cursor SHALL move in the same direction as the user's eye movement relative to the physical world (down gaze → cursor moves toward bottom of screen, up gaze → cursor moves toward top of screen).
5. WHEN the user's gaze direction moves horizontally (eyes look left or right), THE Gaze_Tracker SHALL produce a horizontal delta with the same sign convention (look right → positive dx → cursor moves right on screen) regardless of iPad orientation.
6. IF the iPad orientation changes while the Gaze_Tracker is active, THEN THE Gaze_Tracker SHALL apply the updated orientation mapping on the next frame without producing a spurious delta from the transition.

### Requirement 10: Fix Inverted Vertical Axis on Head Tracker

**User Story:** As a user, I want tilting my head forward (chin toward chest) to move the cursor down and tilting my head back to move the cursor up, so that head tracking matches my natural spatial expectation.

#### Acceptance Criteria

1. WHEN the user tilts their head forward (positive pitch — chin moves toward chest), THE Head_Tracker SHALL produce a positive pitch delta value over WebSocket.
2. WHEN the user tilts their head backward (negative pitch — head tilts back), THE Head_Tracker SHALL produce a negative pitch delta value over WebSocket.
3. THE Fusion_Engine SHALL map head pitch deltas such that `dy = pitch * head_sensitivity` (positive pitch produces positive dy, moving cursor downward on screen; negative pitch produces negative dy, moving cursor upward on screen).
4. WHEN the user turns their head to the right (positive yaw), THE Fusion_Engine SHALL map the yaw delta such that `dx = yaw * head_sensitivity`, moving the cursor rightward on screen.
5. WHEN the user turns their head to the left (negative yaw), THE Fusion_Engine SHALL map the yaw delta such that `dx = yaw * head_sensitivity`, moving the cursor leftward on screen.
6. THE vertical and horizontal axis mappings SHALL be consistent regardless of the iPad's physical orientation (portrait, landscape-left, landscape-right).

### Requirement 11: Sensor Toggle Patterns — Explicit Enable/Disable

**User Story:** As a user with bipolar disorder, I want sensor toggles to behave predictably with clear on/off states, so that I always know which sensors are active and never experience unexpected cursor movement.

#### Acceptance Criteria

1. THE Sensor_Manager SHALL maintain an explicit enabled/disabled state for each sensor (tilt, gaze, head) that persists across app sessions via SettingsStore.
2. WHEN a sensor is toggled from enabled to disabled, THE Sensor_Manager SHALL stop that sensor's data stream within 100ms and THE Fusion_Engine SHALL discard any buffered data from that sensor without producing cursor movement.
3. WHEN a sensor is toggled from disabled to enabled, THE Sensor_Manager SHALL start the sensor with a fresh state — resetting filter memory, bias estimates, and calibration offsets to defaults so that no stale values from the previous session influence cursor output.
4. WHEN a sensor toggle state changes, THE Sensor_Manager SHALL update the corresponding SensorState (isEnabled, isRunning) within 100ms of the user action, and the UI SHALL reflect the new state without transition animations.
5. IF a sensor fails to start within 2 seconds of being toggled on, THEN THE Sensor_Manager SHALL revert the toggle to disabled, update the SensorState with the failure reason, and display an inline status indicator on the sensor's toggle row (no modal alert, no sound, no vibration).
6. WHEN a sensor is toggled from enabled to disabled, THE Fusion_Engine SHALL hold the cursor at its current position — no residual drift or jump SHALL occur from previously-buffered data after the toggle completes.

### Requirement 12: Sensor Toggle Patterns — Mutual Exclusion for Cursor Sensors

**User Story:** As a user, I want only one cursor-driving sensor active at a time, so that multiple sensors do not fight over cursor position and cause erratic movement.

#### Acceptance Criteria

1. WHEN the user enables a cursor-driving sensor (tilt, gaze, or head), THE Sensor_Manager SHALL disable any other currently-active cursor-driving sensor before starting the newly-selected one, ensuring at most one cursor-driving sensor is active at any time.
2. THE Sensor_Manager SHALL display a persistent visual indicator on the iPad UI identifying which cursor sensor (tilt, gaze, or head) is currently active, or indicating that no cursor sensor is active when all three are disabled.
3. WHEN switching between cursor sensors, THE Fusion_Engine SHALL hold the cursor at its current pixel position for 200ms, discarding all incoming cursor-sensor data during that window, to prevent a jump to the new sensor's initial reading.
4. IF the newly-enabled cursor sensor does not produce valid data within 1000ms of being started, THEN THE Sensor_Manager SHALL revert to no active cursor sensor and update the indicator to reflect that no cursor sensor is active.
5. WHILE the priority-based safety fallback is engaged (gaze data received within the last 300ms suppresses tilt and head output), THE Fusion_Engine SHALL override the user's toggle selection at the movement level without changing the persisted toggle state, so that when gaze data becomes stale the user's selected sensor resumes automatically.

### Requirement 13: Sensor Toggle Patterns — Quick-Pause via Sound Action

**User Story:** As a user with RA, I want to temporarily pause all cursor sensors with a mouth sound so that I can reposition my body or the iPad without the cursor flying across the screen, then resume with the same sound.

#### Acceptance Criteria

1. WHEN the user produces the configured pause sound action (default: sustained hiss > 300ms), THE Fusion_Engine SHALL suppress all cursor movement from tilt, gaze, and head sensors within 100ms of sound detection completing.
2. WHILE cursor sensors are paused, THE Fusion_Engine SHALL continue to receive and discard sensor data without accumulating state (no drift buildup during pause).
3. WHEN the user produces the pause sound action again, THE Fusion_Engine SHALL resume cursor movement within 100ms, re-zeroing each sensor's reference point to the current physical state (tilt neutral to current gravity vector, gaze baseline to current gaze direction, head baseline to current head pose) so that the cursor remains at its pre-resume pixel position with no jump.
4. THE pause/resume state SHALL be indicated by a visible change on the iPad UI (cursor-sensor status icon switches between active and paused states, or the sensor area opacity reduces to 50%) without any startling transition (no flash, no sound, no vibration beyond UIImpactFeedbackGenerator .light style or equivalent).
5. IF the cursor sensors remain paused for more than 60 seconds, THEN THE Fusion_Engine SHALL auto-resume (applying the same sensor re-zeroing as criterion 3) and log a warning, preventing accidental indefinite pause.
6. IF a second pause sound action is detected within 500ms of the first, THEN THE Fusion_Engine SHALL ignore the second trigger, preventing rapid toggling from accidental or sustained sound input.

### Requirement 14: Power-Curve Transfer Function for Tilt Velocity Mode

**User Story:** As a user with RA, I want slow tilts to produce precise cursor movements and fast tilts to produce rapid cursor traversal, so that I can both aim at small targets and cross the screen without excessive wrist effort.

#### Acceptance Criteria

1. WHEN the Tilt_Sensor operates in velocity mode, THE Fusion_Engine SHALL apply a Power_Curve transfer function to the filtered angular velocity before computing cursor displacement, using the formula: `cursor_velocity = sign(input) * |input|^exponent * sensitivity_multiplier`.
2. THE Power_Curve for velocity mode SHALL use a configurable exponent constrained to the range 1.0 to 4.0 inclusive (default 2.0), providing quadratic sensitivity scaling at default.
3. WHEN the angular velocity is within 0.1 rad/s above the Dead_Zone_Ramp outer threshold, THE Power_Curve SHALL produce cursor movement of less than 60 pixels per second at default sensitivity (sensitivity_multiplier = 1.0), enabling very fine adjustments.
4. WHEN the angular velocity exceeds 2.0 rad/s, THE Power_Curve SHALL produce cursor movement exceeding 400 pixels per second at default sensitivity (sensitivity_multiplier = 1.0), enabling full-screen traversal.
5. THE Power_Curve SHALL compose with the Dead_Zone_Ramp such that the ramp output feeds into the power curve input, producing C0-continuous output (no jump in value) and monotonically non-decreasing cursor speed for increasing angular velocity magnitude across the ramp-to-full transition.
6. THE Power_Curve SHALL preserve the sign of the input — a positive angular velocity (tilt right/down) SHALL produce positive cursor displacement, and a negative angular velocity (tilt left/up) SHALL produce negative cursor displacement, regardless of exponent value.
7. FOR ALL angular velocity magnitudes above the dead zone, THE Power_Curve output SHALL increase monotonically with input magnitude — a faster tilt SHALL never produce slower cursor movement than a slower tilt in the same direction.

### Requirement 15: Gaze Tracker Evaluation Against Industry Patterns

**User Story:** As a user, I want the gaze tracker to implement filtering and mapping patterns consistent with commercial eye trackers (Tobii) and academic research, so that the pointing experience meets established usability standards.

#### Acceptance Criteria

1. WHILE gaze velocity exceeds 100 degrees/second (saccade detected), THE Gaze_Tracker SHALL output zero cursor displacement, suppressing all cursor movement until gaze velocity drops below 50 degrees/second for at least 30ms.
2. WHEN a fixation is detected (gaze velocity below 5 degrees/second for at least 100ms), THE Fusion_Engine SHALL reduce cursor movement speed by 50% to assist target acquisition, and SHALL exit the fixation state when gaze velocity exceeds 20 degrees/second for at least 30ms, restoring full cursor movement speed.
3. THE Gaze_Tracker SHALL apply a confidence-weighted output where each frame's contribution to cursor movement is scaled linearly by its confidence value (a frame at confidence 0.3 contributes half the displacement of a frame at confidence 0.6).
4. WHEN gaze tracking confidence drops below 0.3 for more than 500ms, THE Fusion_Engine SHALL freeze the cursor at its last known position and SHALL resume cursor movement only after confidence returns to 0.6 or above for at least 200ms.
5. WHEN transitioning from saccade suppression to normal tracking, THE Fusion_Engine SHALL ramp cursor movement from zero to full output over 50ms to prevent an abrupt cursor jump at the saccade endpoint.

### Requirement 16: Head Tracker Evaluation Against Industry Patterns

**User Story:** As a user, I want head tracking to implement acceleration and filtering patterns consistent with commercial head-pointing devices (HeadMouse Nano, GlassOuse), so that the pointing experience is predictable and efficient.

#### Acceptance Criteria

1. THE Head_Tracker SHALL implement a non-linear acceleration curve where head movements below 3 degrees/second produce cursor movement below 50 pixels/second, head movements between 3 and 15 degrees/second produce proportionally scaled cursor movement, and head movements above 15 degrees/second produce cursor movement exceeding 800 pixels/second at default sensitivity.
2. THE Head_Tracker SHALL produce zero cursor output for head angular velocities below 1 degree/second, suppressing involuntary tremor from reaching the cursor.
3. WHEN the user's head is stationary (angular velocity below 0.5 degrees/second for 200ms), THE Fusion_Engine SHALL lock the cursor position to prevent drift from sensor noise, and WHEN angular velocity subsequently exceeds 1.5 degrees/second, THE Fusion_Engine SHALL unlock and resume cursor movement from the current locked position without a jump.
4. THE Head_Tracker SHALL implement a configurable acceleration exponent (range 1.0 to 3.0, default 1.8) that applies a Power_Curve transfer function of the form `output = sign(input) * |input|^exponent` to the filtered angular velocity.
5. FOR ALL angular velocity inputs, THE Head_Tracker acceleration curve SHALL produce continuous output — there SHALL be no discontinuous jumps in cursor velocity as input transitions between the tremor suppression zone, the fine-control zone, and the accelerated traversal zone.
