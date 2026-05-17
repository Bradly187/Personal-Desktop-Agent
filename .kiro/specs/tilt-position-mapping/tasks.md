# Implementation Plan: Tilt Position Mapping

## Overview

Replace the TiltSensor's velocity-based cursor control with a position-mapped model. The iPad computes absolute screen coordinates from tilt angle relative to a calibrated neutral position, sends `tilt_position` messages over WebSocket, and the PC-side FusionEngine moves the cursor to the corresponding absolute pixel position with EMA smoothing.

Implementation spans two codebases:
- **iPad (Swift):** Modify `TiltSensor`, `SettingsStore`, and `WebSocketManager`
- **PC (Python):** Modify `ipad_bridge.py` and `FusionEngine`

## Tasks

- [x] 1. Add position-mapping settings to SettingsStore (iPad)
  - [x] 1.1 Add `tiltRange`, `tiltPositionMode`, and neutral gravity persistence to SettingsStore
    - Add `@Published var tiltRange: Double` with default 25.0, clamped [5, 60] in `didSet`
    - Add `@Published var tiltPositionMode: Bool` with default `true`
    - Add `neutralGravityX`, `neutralGravityY`, `neutralGravityZ` Double properties persisted to UserDefaults
    - Add computed `hasPersistedNeutral: Bool` checking if `neutralGravityX` key exists in defaults
    - Change `tiltDeadZone` default from 0.02 to 1.5 (degrees instead of radians for position mode)
    - Load all new values in `init(defaults:)`
    - _Requirements: 2.2, 2.3, 3.1, 3.2, 3.3_

  - [x] 1.2 Write property test for tiltRange clamping (Property 5)
    - **Property 5: Tilt Range Constraint**
    - Generate random Double values (including negatives, zero, >60), assign to `tiltRange`, verify stored value is always in [5, 60]
    - Use swift-testing with `@Test(arguments:)` and randomized inputs (100+ iterations)
    - **Validates: Requirements 3.3**

  - [x] 1.3 Write property test for calibration persistence round trip (Property 3)
    - **Property 3: Calibration Persistence Round Trip**
    - Generate random valid gravity vectors (unit vectors), persist to SettingsStore, reload, verify equality within Float64 epsilon
    - Use swift-testing with randomized inputs (100+ iterations)
    - **Validates: Requirements 2.1, 2.2**

- [x] 2. Implement position computation in TiltSensor (iPad)
  - [x] 2.1 Add neutral gravity state and calibration method to TiltSensor
    - Add `private var neutralGravity: SIMD3<Double>` initialized from SettingsStore or default 45° (`(-0.707, 0, -0.707)`)
    - Add `func calibrate()` that captures current gravity vector, stores to `neutralGravity`, persists to SettingsStore, and provides light haptic feedback
    - Add debounce: ignore calibrations within 500ms of each other
    - Load persisted neutral on `start()` if `settings.hasPersistedNeutral`
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5_

  - [x] 2.2 Implement `computePosition(gravity:)` pure function
    - Extract gravity to SIMD3, compute pitch/roll angles via `atan2`
    - Compute delta from neutral pitch/roll, convert to degrees
    - Apply dead zone: if `abs(delta) < tiltDeadZone`, set delta to 0
    - Linear map to [0.0, 1.0]: `x = 0.5 + (deltaRoll / tiltRange) * 0.5`, `y = 0.5 - (deltaPitch / tiltRange) * 0.5`
    - Apply inversion if `settings.tiltInverted`: `x = 1.0 - x`, `y = 1.0 - y`
    - Clamp both axes to [0.0, 1.0]
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 6.1, 7.1_

  - [x] 2.3 Write property test for linear mapping (Property 1)
    - **Property 1: Linear Mapping Produces Correct Normalized Output**
    - Generate random angles in [-tiltRange, +tiltRange], verify output is linearly proportional with 0→0.5, +range→1.0 (or 0.0 for pitch), -range→0.0 (or 1.0 for pitch)
    - **Validates: Requirements 1.3, 1.4, 3.5**

  - [x] 2.4 Write property test for output clamping (Property 2)
    - **Property 2: Output Clamping Invariant**
    - Generate random gravity vectors (including extreme orientations), verify x and y are always in [0.0, 1.0]
    - **Validates: Requirements 1.5**

  - [x] 2.5 Write property test for dead zone (Property 7)
    - **Property 7: Dead Zone Maps to Center**
    - Generate angular displacements less than `tiltDeadZone` on both axes, verify output is exactly (0.5, 0.5)
    - **Validates: Requirements 6.1**

  - [x] 2.6 Write property test for inversion symmetry (Property 11)
    - **Property 11: Inversion Symmetry**
    - For random tilt angles, compute position with `tiltInverted = false` → (x, y), then with `tiltInverted = true` → verify result is (1.0 - x, 1.0 - y)
    - **Validates: Requirements 7.1**

  - [x] 2.7 Write property test for recalibration recenters (Property 4)
    - **Property 4: Recalibration Recenters Output**
    - For random gravity vectors, set as neutral, then compute position with same gravity → verify output is (0.5, 0.5)
    - **Validates: Requirements 2.5**

  - [x] 2.8 Write property test for tap independence (Property 12)
    - **Property 12: Tap Independence**
    - For random CMDeviceMotion frames, verify `computePosition` output is identical regardless of `userAcceleration` magnitude (only gravity matters)
    - **Validates: Requirements 8.3**

- [x] 3. Implement message sending and suppression in TiltSensor (iPad)
  - [x] 3.1 Add stationary lock and message suppression logic
    - Add `lastSentX`, `lastSentY` tracking for suppression (delta < 0.001 threshold)
    - Add `stationaryStartTime` and `lockedCoords` for stationary lock (angular velocity ≤ 0.01 rad/s for 200ms)
    - In `handle(_:)`: when `tiltPositionMode` is true, call `computePosition`, apply stationary lock, check suppression, send via `ws.sendTiltPosition(x:y:)`
    - When `tiltPositionMode` is false, use existing velocity-based logic
    - Do NOT send legacy `tilt` messages when in position mode
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 6.3_

  - [x] 3.2 Add `sendTiltPosition(x:y:)` to WebSocketManager
    - Add method: `func sendTiltPosition(x: Double, y: Double)` that sends `{"type": "tilt_position", "x": x, "y": y}`
    - _Requirements: 4.1_

  - [x] 3.3 Write property test for message suppression (Property 8)
    - **Property 8: Message Suppression**
    - Generate sequences of coordinates where consecutive values differ by < 0.001 on both axes, verify no message is sent for subsequent values
    - **Validates: Requirements 4.3**

  - [x] 3.4 Write property test for stationary lock (Property 10)
    - **Property 10: Stationary Lock**
    - Generate motion sequences with stationary periods (angular velocity ≤ 0.01 rad/s for ≥ 200ms), verify output coordinates freeze at last pre-stationary value
    - **Validates: Requirements 6.3**

- [x] 4. Checkpoint - Ensure iPad-side tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 5. Implement PC-side position handling
  - [x] 5.1 Add `tilt_position` message handler to ipad_bridge.py
    - Add handler block for `msg_type == "tilt_position"` that extracts `x`, `y` floats and calls `self._fusion.on_tilt_position(x, y)`
    - Wrap in try/except for ValueError/TypeError, log DEBUG on bad data
    - Place handler before the existing `tilt` handler
    - _Requirements: 4.1, 9.2_

  - [x] 5.2 Add `on_tilt_position` method and absolute positioning to FusionEngine
    - Add `_tilt_position: Optional[tuple[float, float]]` state field
    - Add `_tilt_pos_ema_x`, `_tilt_pos_ema_y` (default 0.5), `_tilt_pos_alpha` (default 0.4), `_tilt_pos_initialized` (default False)
    - Add `def on_tilt_position(self, x: float, y: float) -> None` that stores the position
    - In `_tick()` Rule 6: check `_tilt_position` first; if present, apply EMA smoothing, convert to pixels `(x * screen_width, y * screen_height)`, clamp to screen bounds, call `pyautogui.moveTo`
    - Respect gaze suppression: if gaze-to-cursor mode active and gaze is recent, discard `_tilt_position`; if gaze stale, allow tilt and clear gaze EMA
    - If both `_tilt_position` and `_tilt` are present in same tick, use `_tilt_position` and discard `_tilt`
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 9.1, 9.2, 9.3_

  - [x] 5.3 Write property test for EMA smoothing recurrence (Property 9)
    - **Property 9: EMA Smoothing Recurrence**
    - Generate random position sequences, apply EMA with configurable alpha, verify `smoothed[n] == alpha * input[n] + (1 - alpha) * smoothed[n-1]` and variance(smoothed) ≤ variance(input)
    - Use Python `hypothesis` library with 100+ examples
    - **Validates: Requirements 5.2, 6.2**

  - [x] 5.4 Write property test for tilt range immediate effect (Property 6)
    - **Property 6: Tilt Range Immediate Effect**
    - For a fixed non-zero tilt angle within both ranges, compute position with tiltRange r1 then r2 (r1 ≠ r2), verify different screen coordinates
    - Use Python `hypothesis` library (testing the mapping formula)
    - **Validates: Requirements 3.4**

- [x] 6. Preserve impulse tap detection
  - [x] 6.1 Verify tap detection remains independent of position mode
    - Ensure `detectImpulseTap` in TiltSensor continues using `userAcceleration` magnitude threshold and cooldown
    - Ensure `tilt_tap` messages are sent independently of position-mapping
    - Verify no code path in `computePosition` reads or modifies `userAcceleration` data
    - _Requirements: 8.1, 8.2, 8.3_

- [x] 7. Add calibration UI trigger
  - [x] 7.1 Add calibrate button to SettingsView or relevant UI
    - Add a "Calibrate Neutral" button that calls `TiltSensor.calibrate()`
    - Provide light haptic feedback on calibration (UIImpactFeedbackGenerator, light style)
    - _Requirements: 2.1_

- [x] 8. Wire tilt range setting to UI
  - [x] 8.1 Add tilt range slider to SettingsView
    - Add slider for `tiltRange` with range [5, 60], step 1, showing current value in degrees
    - Add toggle for `tiltPositionMode` (position vs legacy velocity)
    - Changes apply immediately via SettingsStore's `@Published` properties
    - _Requirements: 3.1, 3.4_

- [x] 9. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from the design document
- Unit tests validate specific examples and edge cases
- iPad property tests use swift-testing with `@Test(arguments:)` and randomized inputs
- PC property tests use Python `hypothesis` library
- The `computePosition` function should be kept as a pure function (gravity in → coordinates out) to enable easy property testing
- Existing velocity-based tilt (`tilt` messages) remains functional when `tiltPositionMode` is false (backward compatibility)

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "3.2", "5.1"] },
    { "id": 1, "tasks": ["1.2", "1.3", "2.1"] },
    { "id": 2, "tasks": ["2.2", "5.2"] },
    { "id": 3, "tasks": ["2.3", "2.4", "2.5", "2.6", "2.7", "2.8", "5.3", "5.4"] },
    { "id": 4, "tasks": ["3.1", "6.1"] },
    { "id": 5, "tasks": ["3.3", "3.4", "7.1", "8.1"] }
  ]
}
```
