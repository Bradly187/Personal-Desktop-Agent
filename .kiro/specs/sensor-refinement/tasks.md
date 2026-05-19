# Implementation Plan: Sensor Refinement

## Overview

Implements research-backed signal processing across all cursor-driving sensors: 1-Euro adaptive filtering, power-curve transfer functions, dead zone ramps, gyro bias calibration, ratcheting, axis corrections, saccade suppression, head acceleration curves, and mutual-exclusion toggle/pause system. Work is ordered to build foundational components first (filters, math functions), then integrate into pipelines, then add UX features.

## Tasks

- [x] 1. Create Python OneEuroFilter class in `one_euro_filter.py` — implements Casiez et al. 2012 adaptive low-pass filter with configurable min_cutoff (1.0 Hz), beta (0.007), d_cutoff (1.0 Hz), parameter clamping, reset method, and edge case handling (first sample, dt≤0). Write pytest unit tests for step response, monotonicity, reset, and clamping. **Requirements: 1, 2**
- [x] 2. Create Python GyroBiasCalibrator class in `gyro_bias_calibrator.py` — state machine (UNCALIBRATED→COLLECTING→CALIBRATED→FROZEN) with stationary detection (<0.02 rad/s for 1s), sample averaging (50-200 samples), smooth bias transition (lerp over 500ms), and uncalibrated suppression (<0.05 rad/s). Write pytest unit tests for state transitions, bias averaging, lerp timing, freeze on motion. **Requirements: 7**
- [x] 3. Add dead_zone_ramp and power_curve functions to `fusion_engine.py` — smoothstep cubic hermite ramp (3t²-2t³) with configurable inner/outer thresholds, power curve with sign preservation and configurable exponent. Add FusionConfig fields. Write pytest tests for continuity, monotonicity, composition. **Requirements: 5, 8, 14**
- [x] 4. Integrate 1-Euro filter and signal processing into tilt velocity pipeline in `fusion_engine.py` — replace EMA (alpha=0.3) with: bias subtraction → 1-Euro filter (per-axis) → dead zone ramp → power curve → sub-pixel accumulator. Remove _tilt_ema_x/_tilt_ema_y. Add FusionConfig fields for tilt_vel filter params. Write integration test. **Requirements: 1, 7, 8, 14**
- [x] 5. Integrate 1-Euro filter and power curve into tilt position pipeline in `fusion_engine.py` — replace EMA (alpha=0.4) with: 1-Euro filter (per-axis) → power curve (displacement from 0.5) → pixel mapping. Remove _tilt_pos_ema_x/_tilt_pos_ema_y/_tilt_pos_initialized. Initialize filter with first sample. Write integration test. **Requirements: 2, 5**
- [x] 6. Fix inverted vertical axis on HeadTracker — in `HeadTracker.swift` verify pitch sign (forward tilt = positive), in `fusion_engine.py` Rule 7 change `dy = int(-pitch * sensitivity)` to `dy = int(pitch * sensitivity)`. Write test confirming positive pitch → cursor down. **Requirements: 10**
- [x] 7. Fix inverted vertical axis on GazeTracker — in `GazeTracker.swift` add `correctedDy = -rawDy` sign flip, add orientation-aware rotation matrix, handle orientation change (reset filter). Write test confirming downward gaze → positive dy. **Requirements: 9**
- [x] 8. Create Swift OneEuroFilter class in `iPadApp/DesktopAgent/Sensors/OneEuroFilter.swift` — matching Python algorithm exactly with init(minCutoff:beta:dCutoff:), filter(_:timestamp:), reset(initialValue:). Write XCTest verifying identical output to Python for same input sequence. **Requirements: 3, 4**
- [x] 9. Integrate 1-Euro filter into GazeTracker with saccade detection — replace EMA with OneEuroFilter (min_cutoff=1.5, beta=0.01), add saccade state machine (TRACKING→SACCADE at >100°/s→RAMP_IN at <50°/s for 30ms→TRACKING), confidence weighting, blink reset (>3 frames lost), remove hard dead zone. Modify gaze_delta WebSocket message to include conf and saccade fields. Add SettingsStore properties. **Requirements: 3, 15**
- [x] 10. Integrate 1-Euro filter into HeadTracker — replace EMA smoothing with OneEuroFilter (min_cutoff=1.2, beta=0.008) per-axis, add SettingsStore properties, remove old headSmoothingFactor. Write XCTest for drift suppression and fast-movement tracking. **Requirements: 4**
- [x] 11. Implement head tracker acceleration curve and stationary lock in `fusion_engine.py` — add head_acceleration_curve function (3-zone: tremor <1°/s=0, fine 1-15°/s, accelerated >15°/s with power curve exponent 1.8), HeadStationaryLock class (hysteresis lock 0.5°/s for 200ms, unlock 1.5°/s). Replace linear head mapping in Rule 7. Write tests for zone boundaries and hysteresis. **Requirements: 16**
- [x] 12. Implement gaze fixation slowdown and confidence freeze in `fusion_engine.py` — add fixation detection (velocity <5°/s for 100ms → 50% speed, exit >20°/s for 30ms), confidence freeze (<0.3 for 500ms → freeze, resume ≥0.6 for 200ms). Parse conf/saccade from gaze_delta in ipad_bridge.py. Add FusionConfig fields. Write tests. **Requirements: 15**
- [x] 13. Implement ratcheting system — TiltSensor.swift: add ratchet() with 500ms debounce and motion-delay (delay if >0.5 rad/s, up to 300ms). WebSocketManager: sendRatchet(). ipad_bridge.py: handle tilt_ratchet message. FusionEngine: on_tilt_ratchet() records held position, sets ratchet_active, resets filter; hold cursor until displacement exceeds dead zone (2°). Add visual confirmation (200-400ms highlight). Write tests. **Requirements: 6**
- [x] 14. Implement sensor toggle mutual exclusion — SensorManager.swift: add CursorSensor enum, selectCursorSensor() with disable-old/notify-PC/enable-new/persist. WebSocketManager: sendSensorSwitch(). ipad_bridge.py: handle sensor_switch. FusionEngine: on_sensor_switch() sets 200ms hold, resets new sensor state. SettingsStore: activeCursorSensor. Handle 1000ms timeout (revert if no data). Write tests. **Requirements: 11, 12**
- [x] 15. Implement quick-pause via sound action — FusionEngine: CursorPauseState class (toggle, 500ms debounce, 60s auto-resume). While paused: discard data, no accumulation. On resume: re-zero sensors (ratchet tilt, reset gaze/head filters). Wire sustained hiss >300ms from SoundDetector. WebSocketManager: sendCursorPause/Resume. iPad UI: opacity/icon indicator. Write tests for debounce, auto-resume, no movement while paused. **Requirements: 13**
- [x] 16. Update SensorDashboardView UI — add cursor sensor picker (segmented control), ratchet button (large touch target), pause indicator, active sensor indicator. Follow design system (DAButton, DesignTokens). No startling transitions. **Requirements: 6, 11, 12, 13**

## Task Dependency Graph

```json
{
  "waves": [
    {
      "wave": 1,
      "tasks": [1, 2, 3, 6, 7, 8],
      "description": "Foundational components and quick axis fixes — no dependencies"
    },
    {
      "wave": 2,
      "tasks": [4, 5, 9, 10],
      "description": "Pipeline integration — depends on wave 1 filter/math components"
    },
    {
      "wave": 3,
      "tasks": [11, 12, 13, 14],
      "description": "Advanced features — depends on wave 2 pipeline integration"
    },
    {
      "wave": 4,
      "tasks": [15, 16],
      "description": "UX features and final UI — depends on wave 3 features"
    }
  ]
}
```

```
1 (OneEuroFilter Python) ─┬─→ 4 (tilt velocity integration)
                          └─→ 5 (tilt position integration)
2 (GyroBiasCalibrator) ────→ 4
3 (dead zone ramp + power curve) ─┬─→ 4
                                  └─→ 5
6 (head axis fix) ─────────→ 11 (head acceleration curve)
7 (gaze axis fix) ─────────→ 9 (gaze 1-Euro + saccade)
8 (Swift OneEuroFilter) ───┬─→ 9 (gaze integration)
                           └─→ 10 (head integration)
9 (gaze 1-Euro + saccade) ─→ 12 (fixation slowdown)
10 (head 1-Euro) ──────────→ 11
5 (tilt position) ─────────→ 13 (ratcheting)
4 (tilt velocity) ─────────→ 13
14 (mutual exclusion) ─────→ 15 (quick-pause)
14 ────────────────────────→ 16 (UI)
13 ────────────────────────→ 16
15 ────────────────────────→ 16
```

## Notes

- Tasks 1-3 are foundational (pure math/algorithm) and can be developed in parallel
- Tasks 6-7 (axis fixes) are quick wins that immediately improve UX — prioritize early
- Task 8 (Swift OneEuroFilter) blocks all iPad-side filter integration (tasks 9, 10)
- Tasks 4-5 (PC-side tilt integration) depend on tasks 1-3 completing
- Task 16 (UI) is the final integration task that depends on all feature tasks
- All filter parameter defaults are starting points — expect tuning during manual testing
