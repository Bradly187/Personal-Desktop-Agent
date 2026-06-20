# Technical Design: Sensor Refinement

> **⚠️ Partially superseded.** Eye-gaze and head-pose control were removed 2026-05-30 (the
> standard iPad lacks TrueDepth). **Only the tilt sensor remains active** — the GazeTracker /
> HeadTracker pipelines, filters, `gaze_delta`/`head_pose` message types, and `sensor_switch`
> `gaze|head` options described below are **no longer present**. See `specs/ipad-sensor-focus/`
> for the current design.

## Overview

This design replaces ad-hoc EMA smoothing and hard dead zones across all three cursor-driving sensors (tilt, gaze, head) with research-backed signal processing: adaptive 1-Euro filtering, power-curve transfer functions, smooth dead zone ramps, gyro bias calibration, ratcheting, saccade suppression, and a mutual-exclusion toggle system. The implementation spans both the iPad-side Swift code (pre-filtering for gaze and head) and the PC-side Python Fusion_Engine (tilt processing, transfer functions, cursor output).

## Architecture

### Component Diagram

```
┌─────────────────────────────────────────────────────────────────────────┐
│  iPad (Swift)                                                           │
│                                                                         │
│  ┌──────────────┐   ┌──────────────────┐   ┌──────────────────┐       │
│  │ TiltSensor   │   │ GazeTracker      │   │ HeadTracker      │       │
│  │ (CoreMotion) │   │ (ARKit eyes)     │   │ (ARKit face)     │       │
│  │              │   │                  │   │                  │       │
│  │ • Ratchet    │   │ • 1-Euro Filter  │   │ • 1-Euro Filter  │       │
│  │   capture    │   │ • Axis correction│   │ • Axis correction│       │
│  │ • Debounce   │   │ • Saccade detect │   │                  │       │
│  │ • Motion-    │   │ • Confidence wt  │   │                  │       │
│  │   delay      │   │                  │   │                  │       │
│  └──────┬───────┘   └────────┬─────────┘   └────────┬─────────┘       │
│         │                    │                       │                  │
│  ┌──────┴────────────────────┴───────────────────────┴─────────┐       │
│  │              SensorManager (mutual exclusion)                │       │
│  │  • Toggle state machine  • 200ms hold on switch             │       │
│  │  • Pause/resume          • Sensor re-zeroing                │       │
│  └──────────────────────────────┬──────────────────────────────┘       │
│                                 │ WebSocket                            │
└─────────────────────────────────┼──────────────────────────────────────┘
                                  │
┌─────────────────────────────────┼──────────────────────────────────────┐
│  PC (Python)                    ▼                                       │
│  ┌──────────────────────────────────────────────────────────────┐      │
│  │                      IPadBridge                               │      │
│  │  • Dispatches tilt/gaze_delta/head_pose to FusionEngine      │      │
│  │  • Handles tilt_ratchet, cursor_pause, sensor_switch msgs    │      │
│  └──────────────────────────────┬───────────────────────────────┘      │
│                                 │                                       │
│  ┌──────────────────────────────▼───────────────────────────────┐      │
│  │                      FusionEngine                             │      │
│  │                                                               │      │
│  │  Tilt Velocity Pipeline:                                      │      │
│  │  ┌─────────┐ ┌──────────┐ ┌───────────┐ ┌──────────┐ ┌───┐ │      │
│  │  │Bias Sub │→│1-Euro Flt│→│Dead Zone  │→│Power     │→│Δpx│ │      │
│  │  │         │ │          │ │Ramp       │ │Curve     │ │   │ │      │
│  │  └─────────┘ └──────────┘ └───────────┘ └──────────┘ └───┘ │      │
│  │                                                               │      │
│  │  Tilt Position Pipeline:                                      │      │
│  │  ┌──────────┐ ┌───────────┐ ┌──────────┐ ┌───────────────┐  │      │
│  │  │Normalize │→│1-Euro Flt │→│Power     │→│Pixel Mapping  │  │      │
│  │  │Position  │ │           │ │Curve     │ │               │  │      │
│  │  └──────────┘ └───────────┘ └──────────┘ └───────────────┘  │      │
│  │                                                               │      │
│  │  Gaze Pipeline (pre-filtered on iPad):                        │      │
│  │  ┌──────────────┐ ┌──────────────┐ ┌─────────────────────┐  │      │
│  │  │Saccade gate  │→│Fixation slow │→│pyautogui.moveRel    │  │      │
│  │  │(from iPad)   │ │(confidence)  │ │                     │  │      │
│  │  └──────────────┘ └──────────────┘ └─────────────────────┘  │      │
│  │                                                               │      │
│  │  Head Pipeline (pre-filtered on iPad):                        │      │
│  │  ┌──────────────┐ ┌──────────────┐ ┌─────────────────────┐  │      │
│  │  │Accel curve   │→│Tremor lock   │→│pyautogui.moveRel    │  │      │
│  │  │(3-zone)      │ │(hysteresis)  │ │                     │  │      │
│  │  └──────────────┘ └──────────────┘ └─────────────────────┘  │      │
│  │                                                               │      │
│  │  Shared:                                                      │      │
│  │  • Gyro bias calibrator (state machine)                       │      │
│  │  • Ratchet handler (cursor hold + dead zone gate)             │      │
│  │  • Pause system (discard + re-zero on resume)                 │      │
│  └───────────────────────────────────────────────────────────────┘      │
└─────────────────────────────────────────────────────────────────────────┘
```

### Data Flow

#### Tilt Velocity Mode
```
iPad TiltSensor (60 Hz CMDeviceMotion)
  → rotationRate (rx, ry) with gravity-compensated projection
  → WebSocket {"type": "tilt", "rx": float, "ry": float}
  → IPadBridge → FusionEngine.on_tilt(rx, ry)
  → Gyro bias subtraction (rx -= bias_x, ry -= bias_y)
  → 1-Euro filter (per-axis, min_cutoff=1.0, beta=0.007, d_cutoff=1.0)
  → Dead zone ramp (smoothstep, inner=0.05, outer=0.125 rad/s)
  → Power curve (sign(v) * |v|^2.0 * sensitivity)
  → Sub-pixel accumulator → pyautogui.moveRel(dx, dy)
```

#### Tilt Position Mode
```
iPad TiltSensor (60 Hz CMDeviceMotion)
  → computePosition(gravity) → normalized (x, y) in [0,1]
  → WebSocket {"type": "tilt_position", "x": float, "y": float}
  → IPadBridge → FusionEngine.on_tilt_position(x, y)
  → 1-Euro filter (per-axis, min_cutoff=0.5, beta=0.004, d_cutoff=1.0)
  → Power curve (sign(d) * |d|^1.5 where d = filtered - 0.5)
  → Pixel mapping (px = (0.5 + curved_d) * screen_w)
  → pyautogui.moveTo(px_x, px_y)
```

#### Gaze Tracker
```
iPad GazeTracker (ARKit ~60 fps face anchor)
  → extractGazeDirection() → average eye forward vector
  → Compute angular delta from previous frame
  → Axis correction (orientation-aware sign mapping)
  → 1-Euro filter (per-axis, min_cutoff=1.5, beta=0.01, d_cutoff=1.0)
  → Saccade detection (>100°/s → suppress output, <50°/s for 30ms → resume)
  → Confidence weighting (delta *= confidence)
  → Scale by sensitivity
  → WebSocket {"type": "gaze_delta", "dx": float, "dy": float, "conf": float}
  → IPadBridge → FusionEngine.on_gaze_delta(dx, dy)
  → Fixation slowdown (if velocity < 5°/s for 100ms → 50% speed)
  → pyautogui.moveRel(dx, dy)
```

#### Head Tracker
```
iPad HeadTracker (ARKit face anchor)
  → Extract Euler pitch/yaw from face transform
  → Compute delta from previous frame
  → Axis correction (ensure forward-pitch → positive dy)
  → 1-Euro filter (per-axis, min_cutoff=1.2, beta=0.008, d_cutoff=1.0)
  → Convert to degrees, scale by smoothing factor
  → WebSocket {"type": "head_pose", "pitch": float, "yaw": float}
  → IPadBridge → FusionEngine.on_head(pitch, yaw)
  → Acceleration curve (3-zone: tremor suppress / fine / accelerated)
  → Stationary lock (hysteresis: lock <0.5°/s for 200ms, unlock >1.5°/s)
  → pyautogui.moveRel(dx, dy)
```

## Components and Interfaces

### New Components

| Component | Language | File | Purpose |
|-----------|----------|------|---------|
| `OneEuroFilter` | Python | `one_euro_filter.py` | Adaptive low-pass filter for tilt velocity and position pipelines |
| `OneEuroFilter` | Swift | `iPadApp/DesktopAgent/Sensors/OneEuroFilter.swift` | Adaptive low-pass filter for gaze and head pre-filtering |
| `GyroBiasCalibrator` | Python | `gyro_bias_calibrator.py` | Continuous background gyro drift correction |
| `HeadStationaryLock` | Python | `fusion_engine.py` (inline) | Hysteresis-based cursor lock for head tracking |
| `CursorPauseState` | Python | `fusion_engine.py` (inline) | Sound-action pause/resume state management |
| `SaccadeDetector` | Swift | `iPadApp/DesktopAgent/Sensors/GazeTracker.swift` (inline) | Velocity-threshold state machine for saccade suppression |

### Modified Interfaces

| Component | Interface Change |
|-----------|-----------------|
| `FusionEngine` | New methods: `on_tilt_ratchet()`, `on_cursor_pause()`, `on_cursor_resume()`, `on_sensor_switch()` |
| `FusionEngine._tick()` | Tilt velocity pipeline: bias → 1-Euro → ramp → power curve. Tilt position: 1-Euro → power curve. Head: acceleration curve + stationary lock. Gaze: fixation slowdown + confidence freeze |
| `IPadBridge` | New message handlers: `tilt_ratchet`, `cursor_pause`, `cursor_resume`, `sensor_switch` |
| `GazeTracker` | Output includes `conf` and `saccade` fields; axis sign corrected |
| `HeadTracker` | Pitch sign corrected; 1-Euro filter applied before send |
| `TiltSensor` | New `ratchet()` method with debounce and motion-delay |
| `SensorManager` | New `selectCursorSensor(_:)` for mutual exclusion; pause forwarding |
| `WebSocketManager` | New send methods: `sendRatchet()`, `sendSensorSwitch()`, `sendCursorPause()`, `sendCursorResume()` |

## Data Models

### FusionConfig (extended)

```python
@dataclass
class FusionConfig:
    # Existing fields preserved...
    
    # 1-Euro filter — tilt velocity
    tilt_vel_min_cutoff: float = 1.0    # Hz
    tilt_vel_beta: float = 0.007
    tilt_vel_d_cutoff: float = 1.0      # Hz
    
    # 1-Euro filter — tilt position
    tilt_pos_min_cutoff: float = 0.5    # Hz
    tilt_pos_beta: float = 0.004
    tilt_pos_d_cutoff: float = 1.0      # Hz
    
    # Power curve exponents
    tilt_vel_exponent: float = 2.0      # [1.0, 4.0]
    tilt_pos_exponent: float = 1.5      # [1.0, 3.0]
    head_accel_exponent: float = 1.8    # [1.0, 3.0]
    
    # Dead zone ramp
    dead_zone_inner: float = 0.05       # rad/s
    dead_zone_ramp_mult: float = 1.5    # outer = inner + inner * mult
    
    # Gyro bias calibration
    bias_stationary_threshold: float = 0.02   # rad/s
    bias_stationary_duration: float = 1.0     # seconds
    bias_lerp_duration: float = 0.5           # seconds
    bias_uncalibrated_suppress: float = 0.05  # rad/s
    
    # Head acceleration
    head_tremor_threshold: float = 1.0        # °/s
    head_lock_threshold: float = 0.5          # °/s
    head_unlock_threshold: float = 1.5        # °/s
    head_lock_delay: float = 0.2              # seconds
    
    # Gaze fixation/confidence
    gaze_fixation_enter: float = 5.0          # °/s
    gaze_fixation_exit: float = 20.0          # °/s
    gaze_fixation_duration: float = 0.1       # seconds
    gaze_fixation_slowdown: float = 0.5       # multiplier
    gaze_conf_freeze_threshold: float = 0.3
    gaze_conf_freeze_duration: float = 0.5    # seconds
    gaze_conf_resume_threshold: float = 0.6
    gaze_conf_resume_duration: float = 0.2    # seconds
    
    # Pause/switch
    pause_auto_resume_s: float = 60.0
    pause_debounce_s: float = 0.5
    switch_hold_duration: float = 0.2         # seconds
```

### WebSocket Message Schemas

```json
// tilt_ratchet (iPad → PC)
{"type": "tilt_ratchet", "ts": 1234567890.123}

// cursor_pause / cursor_resume (iPad → PC)
{"type": "cursor_pause", "ts": 1234567890.123}
{"type": "cursor_resume", "ts": 1234567890.123}

// sensor_switch (iPad → PC)
{"type": "sensor_switch", "from": "tilt|gaze|head|null", "to": "tilt|gaze|head", "ts": 1234567890.123}

// gaze_delta (iPad → PC, modified)
{"type": "gaze_delta", "dx": 0.0023, "dy": -0.0011, "conf": 0.85, "saccade": false}
```

### CursorSensor Enum (Swift)

```swift
enum CursorSensor: String, CaseIterable {
    case tilt, gaze, head
}
```

### GyroBiasCalibrator States

```python
class BiasState(Enum):
    UNCALIBRATED = "uncalibrated"
    COLLECTING = "collecting"
    CALIBRATED = "calibrated"
    FROZEN = "frozen"
```

## Correctness Properties

### Property 1: Filter Idempotence
The 1-Euro filter applied once per sample produces the same result regardless of call timing (deterministic given timestamp).

**Validates: Requirements 1.5, 2.5**

### Property 2: Dead Zone Continuity
For any two adjacent input samples, the output difference is bounded by the smoothstep function evaluation difference plus 0.5 px/s tolerance.

**Validates: Requirements 8.4, 8.5**

### Property 3: Power Curve Monotonicity
For all inputs above the dead zone, increasing input magnitude always produces increasing output magnitude.

**Validates: Requirements 5.5, 14.7**

### Property 4: Bias Convergence
After a stationary period of ≥1 second, the bias estimate converges to within 0.001 rad/s of the true mean rotation rate.

**Validates: Requirements 7.1, 7.2**

### Property 5: Ratchet Invariant
Triggering a ratchet never produces cursor displacement — the cursor position before and after the ratchet frame are identical.

**Validates: Requirements 6.1, 6.2**

### Property 6: Pause Invariant
While paused, no sensor data produces cursor movement. On resume, cursor position equals the position at pause time.

**Validates: Requirements 13.1, 13.2, 13.3**

### Property 7: Mutual Exclusion
At most one cursor-driving sensor produces cursor movement at any time (enforced by both toggle state and priority fallback).

**Validates: Requirements 12.1, 12.5**

### Property 8: Axis Consistency
For all iPad orientations, looking/tilting down produces positive dy (cursor moves down) and looking/tilting right produces positive dx (cursor moves right).

**Validates: Requirements 9.1, 9.2, 10.1, 10.2**

## Error Handling

| Scenario | Handling |
|----------|----------|
| 1-Euro filter receives dt ≤ 0 | Return previous output unchanged |
| 1-Euro filter parameters out of range | Clamp to valid bounds, log warning |
| Gyro bias calibrator receives no stationary period | Remain in UNCALIBRATED state, suppress velocities < 0.05 rad/s |
| Ratchet triggered while TiltSensor inactive | Silently ignored (no error, no log) |
| Sensor fails to start after toggle-on | Revert toggle to disabled within 2s, show inline status indicator |
| Gaze confidence drops below 0.3 for >500ms | Freeze cursor at last known position |
| Head stationary lock oscillation | Hysteresis gap (lock at 0.5°/s, unlock at 1.5°/s) prevents rapid cycling |
| WebSocket message missing expected fields | Use defaults (conf=1.0, saccade=false); log warning at DEBUG level |
| Pause auto-resume timeout (60s) | Auto-resume with sensor re-zeroing, log WARNING |
| Sensor switch target fails to produce data within 1000ms | Revert to no active cursor sensor |

## Detailed Design

### 1-Euro Filter Implementation

The 1-Euro filter (Casiez et al., 2012) is an adaptive low-pass filter that adjusts its cutoff frequency based on signal speed. At low speeds it applies heavy smoothing (jitter reduction); at high speeds it raises the cutoff to minimize latency.

#### Core Algorithm

```
α(fc) = 1 / (1 + τ/(2π·fc·dt))     where τ = 1/(2π·fc)
                                      simplified: α = 1 / (1 + 1/(2π·fc·dt))

For each sample:
  1. Compute derivative: dx = (x - x_prev) / dt
  2. Filter derivative:  dx_hat = LP(dx, α(d_cutoff))
  3. Compute adaptive cutoff: fc = min_cutoff + beta * |dx_hat|
  4. Filter signal: x_hat = LP(x, α(fc))
```

#### Python Implementation (PC-side, fusion_engine.py)

```python
import math
import time

class OneEuroFilter:
    """1-Euro adaptive low-pass filter (Casiez et al., 2012).
    
    Used in FusionEngine for tilt velocity and tilt position pipelines.
    """
    
    def __init__(
        self,
        min_cutoff: float = 1.0,
        beta: float = 0.007,
        d_cutoff: float = 1.0,
    ) -> None:
        self.min_cutoff = max(0.1, min(10.0, min_cutoff))
        self.beta = max(0.0, min(1.0, beta))
        self.d_cutoff = max(0.1, min(10.0, d_cutoff))
        
        self._x_prev: float | None = None
        self._dx_prev: float = 0.0
        self._t_prev: float | None = None
    
    def reset(self, initial_value: float | None = None) -> None:
        """Reset filter state. Optionally seed with an initial value."""
        self._x_prev = initial_value
        self._dx_prev = 0.0
        self._t_prev = None
    
    def __call__(self, x: float, timestamp: float | None = None) -> float:
        """Filter a single sample. Returns filtered value."""
        now = timestamp if timestamp is not None else time.monotonic()
        
        if self._t_prev is None or self._x_prev is None:
            self._x_prev = x
            self._dx_prev = 0.0
            self._t_prev = now
            return x
        
        dt = now - self._t_prev
        if dt <= 0:
            return self._x_prev
        self._t_prev = now
        
        # Filter derivative
        dx = (x - self._x_prev) / dt
        alpha_d = self._alpha(self.d_cutoff, dt)
        self._dx_prev = alpha_d * dx + (1 - alpha_d) * self._dx_prev
        
        # Adaptive cutoff
        fc = self.min_cutoff + self.beta * abs(self._dx_prev)
        
        # Filter signal
        alpha = self._alpha(fc, dt)
        self._x_prev = alpha * x + (1 - alpha) * self._x_prev
        
        return self._x_prev
    
    @staticmethod
    def _alpha(cutoff: float, dt: float) -> float:
        tau = 1.0 / (2.0 * math.pi * cutoff)
        return 1.0 / (1.0 + tau / dt)
```

#### Swift Implementation (iPad-side, for GazeTracker and HeadTracker)

```swift
import Foundation

/// 1-Euro adaptive low-pass filter (Casiez et al., 2012).
/// Applied per-axis on iPad before sending deltas over WebSocket.
final class OneEuroFilter {
    private let minCutoff: Double
    private let beta: Double
    private let dCutoff: Double
    
    private var xPrev: Double?
    private var dxPrev: Double = 0.0
    private var tPrev: CFTimeInterval?
    
    init(minCutoff: Double = 1.0, beta: Double = 0.007, dCutoff: Double = 1.0) {
        self.minCutoff = max(0.1, min(10.0, minCutoff))
        self.beta = max(0.0, min(1.0, beta))
        self.dCutoff = max(0.1, min(10.0, dCutoff))
    }
    
    func reset(initialValue: Double? = nil) {
        xPrev = initialValue
        dxPrev = 0.0
        tPrev = nil
    }
    
    func filter(_ x: Double, timestamp: CFTimeInterval = CACurrentMediaTime()) -> Double {
        guard let prevT = tPrev, let prevX = xPrev else {
            xPrev = x
            dxPrev = 0.0
            tPrev = timestamp
            return x
        }
        
        let dt = timestamp - prevT
        guard dt > 0 else { return prevX }
        tPrev = timestamp
        
        // Filter derivative
        let dx = (x - prevX) / dt
        let alphaD = Self.alpha(cutoff: dCutoff, dt: dt)
        dxPrev = alphaD * dx + (1 - alphaD) * dxPrev
        
        // Adaptive cutoff
        let fc = minCutoff + beta * abs(dxPrev)
        
        // Filter signal
        let a = Self.alpha(cutoff: fc, dt: dt)
        xPrev = a * x + (1 - a) * prevX
        
        return xPrev!
    }
    
    private static func alpha(cutoff: Double, dt: Double) -> Double {
        let tau = 1.0 / (2.0 * .pi * cutoff)
        return 1.0 / (1.0 + tau / dt)
    }
}
```

#### Parameters Per Sensor Type

| Sensor | min_cutoff | beta | d_cutoff | Location |
|--------|-----------|------|----------|----------|
| Tilt velocity | 1.0 Hz | 0.007 | 1.0 Hz | Python (FusionEngine) |
| Tilt position | 0.5 Hz | 0.004 | 1.0 Hz | Python (FusionEngine) |
| Gaze | 1.5 Hz | 0.01 | 1.0 Hz | Swift (GazeTracker) |
| Head | 1.2 Hz | 0.008 | 1.0 Hz | Swift (HeadTracker) |

#### Reset/Initialization Behavior

- **First sample**: Filter initializes with the first value (no output jump).
- **Sensor enable/resume**: Call `reset()` before processing the first sample after a gap.
- **Gaze blink (>3 frames lost)**: Call `reset()` to prevent spurious delta from stale state.
- **Tilt mode switch**: Reset both velocity and position filter instances.
- **Ratchet trigger**: Reset tilt position filter with new neutral-relative coordinates.

### Power Curve Transfer Function

#### Mathematical Formula

```
output = sign(input) * |input|^exponent * sensitivity_multiplier
```

Applied independently per axis. The sign preservation ensures directional consistency regardless of exponent.

#### Per-Mode Configuration

| Mode | Exponent (default) | Range | Sensitivity Multiplier |
|------|-------------------|-------|----------------------|
| Tilt velocity | 2.0 | [1.0, 4.0] | `tilt_sensitivity` (200.0 px/rad/s) |
| Tilt position | 1.5 | [1.0, 3.0] | Implicit (maps to screen dims) |
| Head tracking | 1.8 | [1.0, 3.0] | `head_sensitivity` (80.0 px/°) |

#### Composition with Dead Zone Ramp (Velocity Mode)

The pipeline order is critical for C0 continuity:

```
raw_velocity → bias_subtract → 1-euro_filter → dead_zone_ramp → power_curve → displacement
```

The dead zone ramp outputs a value in [0, |filtered_velocity|] that smoothly transitions from 0 at the inner threshold to the full filtered magnitude at the outer threshold. This ramped value then feeds into the power curve. Because the ramp output is 0 at the inner boundary and continuous through the outer boundary, and the power curve is monotonic for positive inputs, the composition is C0-continuous.

#### Position Mode Application

For tilt position, the power curve operates on displacement from center:

```python
d = filtered_position - 0.5  # displacement from center, range [-0.5, 0.5]
curved = sign(d) * |d / 0.5|^exponent * 0.5  # normalize, curve, denormalize
pixel = (0.5 + curved) * screen_dimension
```

This provides fine control near center (small tilts) and fast traversal at extremes (large tilts).

### Dead Zone Ramp (Smoothstep)

#### Cubic Hermite Formula

```python
def dead_zone_ramp(magnitude: float, inner: float, outer: float) -> float:
    """Smooth ramp from 0 at inner threshold to full magnitude at outer threshold.
    
    Uses cubic Hermite interpolation (smoothstep) for C1 continuity:
    zero first-derivative at both boundaries.
    """
    if magnitude <= inner:
        return 0.0
    if magnitude >= outer:
        return magnitude  # full pass-through above outer
    
    # Normalized position within ramp [0, 1]
    t = (magnitude - inner) / (outer - inner)
    
    # Smoothstep: 3t² - 2t³ (zero derivative at t=0 and t=1)
    s = t * t * (3.0 - 2.0 * t)
    
    # Scale: at t=1 (outer boundary), output = magnitude = outer
    # Interpolate from 0 to the magnitude value at this point
    return s * magnitude
```

#### Configuration

| Parameter | Default | Range | Description |
|-----------|---------|-------|-------------|
| `inner_threshold` | 0.05 rad/s | [0.01, 0.2] | Below this: zero output |
| `ramp_multiplier` | 1.5 | [1.0, 3.0] | `outer = inner * (1 + ramp_multiplier * inner)` → default outer = 0.125 rad/s |
| `outer_threshold` | 0.125 rad/s | computed | Above this: full pass-through |

#### Continuity Guarantees

- **At inner boundary**: output = 0, first derivative = 0 (no sudden onset)
- **At outer boundary**: output = magnitude, first derivative matches the linear pass-through slope
- **Between boundaries**: monotonically increasing, smooth cubic interpolation
- **Adjacent samples**: cursor velocity difference bounded by ramp function evaluation difference + 0.5 px/s tolerance (Req 8.5)

### Gyro Bias Calibration

#### State Machine

```
                    ┌─────────────────┐
                    │  UNCALIBRATED   │
                    │  bias = (0, 0)  │
                    │  suppress < 0.05│
                    └────────┬────────┘
                             │ stationary detected
                             │ (all axes < 0.02 rad/s for 1s)
                             ▼
                    ┌─────────────────┐
                    │   COLLECTING    │
                    │  accumulate     │
                    │  50–200 samples │
                    └────────┬────────┘
                             │ collection complete
                             ▼
                    ┌─────────────────┐
          ┌────────│   CALIBRATED    │◄──────────┐
          │        │  bias applied   │           │
          │        │  via lerp 500ms │           │
          │        └────────┬────────┘           │
          │                 │ motion detected    │ new stationary
          │                 │ (any axis > 0.02)  │ period detected
          │                 ▼                    │
          │        ┌─────────────────┐           │
          │        │    FROZEN       │───────────┘
          │        │  bias held at   │
          │        │  last estimate  │
          │        └─────────────────┘
          │
          │ (first calibration from UNCALIBRATED
          │  also transitions through COLLECTING)
          └──────────────────────────────────────
```

#### Stationary Detection Algorithm

```python
class GyroBiasCalibrator:
    STATIONARY_THRESHOLD = 0.02  # rad/s
    STATIONARY_DURATION = 1.0    # seconds
    MIN_SAMPLES = 50
    MAX_SAMPLES = 200
    LERP_DURATION = 0.5          # seconds for smooth transition
    SUPPRESS_THRESHOLD = 0.05    # rad/s — suppress when uncalibrated
    
    def __init__(self):
        self.state = "uncalibrated"
        self.bias_x = 0.0
        self.bias_y = 0.0
        self._target_bias_x = 0.0
        self._target_bias_y = 0.0
        self._lerp_start_time: float | None = None
        self._lerp_start_bias = (0.0, 0.0)
        self._stationary_start: float | None = None
        self._samples: list[tuple[float, float]] = []
```

#### Smooth Bias Transition

When a new bias estimate differs from the current by more than 0.005 rad/s, the transition uses linear interpolation over 500ms:

```python
def get_current_bias(self, now: float) -> tuple[float, float]:
    if self._lerp_start_time is None:
        return (self.bias_x, self.bias_y)
    
    elapsed = now - self._lerp_start_time
    t = min(1.0, elapsed / self.LERP_DURATION)
    
    bx = self._lerp_start_bias[0] + t * (self._target_bias_x - self._lerp_start_bias[0])
    by = self._lerp_start_bias[1] + t * (self._target_bias_y - self._lerp_start_bias[1])
    
    if t >= 1.0:
        self.bias_x = self._target_bias_x
        self.bias_y = self._target_bias_y
        self._lerp_start_time = None
    
    return (bx, by)
```

#### Uncalibrated Suppression

Before the first calibration completes, angular velocities below 0.05 rad/s are suppressed entirely (Req 7.6). This prevents drift from uncorrected bias while still allowing intentional large movements.

### Ratcheting System

#### iPad-Side (TiltSensor)

**Gravity capture with motion-delay logic:**

```swift
func ratchet() {
    // 1. Debounce: ignore if within 500ms of last ratchet
    guard lastRatchetTime == nil || Date().timeIntervalSince(lastRatchetTime!) >= 0.5 else {
        return
    }
    
    // 2. Check angular velocity — delay capture if device is moving
    guard let data = motion.deviceMotion else { return }
    let rot = data.rotationRate
    let angVel = max(abs(rot.x), abs(rot.y), abs(rot.z))
    
    if angVel > 0.5 {
        // Device moving — schedule delayed capture (up to 300ms)
        scheduleDelayedRatchet(deadline: Date().addingTimeInterval(0.3))
        return
    }
    
    // 3. Capture gravity as new neutral
    let g = data.gravity
    neutralGravity = SIMD3(g.x, g.y, g.z)
    persistNeutral(g)
    lastRatchetTime = Date()
    
    // 4. Send ratchet message to PC
    ws?.sendRatchet()
    
    // 5. Visual confirmation: brief icon highlight (200-400ms, no sound)
    showRatchetConfirmation()
}
```

**Delayed capture** polls at 30ms intervals until angular velocity drops below 0.5 rad/s or 300ms elapses, then captures at that moment.

#### PC-Side (FusionEngine)

On receiving `tilt_ratchet`:

1. Record current cursor pixel position as `held_position`
2. Set `ratchet_active = True`
3. Reset tilt position 1-Euro filter with initial value at center (0.5, 0.5)
4. On each subsequent tilt frame:
   - If `ratchet_active` and displacement from new neutral < dead zone threshold (2°): hold cursor at `held_position`
   - If displacement exceeds dead zone after passing through Dead_Zone_Ramp: set `ratchet_active = False`, resume normal cursor movement

#### Trigger Sources

| Source | Mechanism |
|--------|-----------|
| iPad UI button | Dedicated ratchet button in sensor control area |
| Sound action | Configurable mouth sound (default: tongue click) mapped via SoundDetector |

#### WebSocket Message

```json
{"type": "tilt_ratchet", "ts": 1234567890.123}
```

### Axis Correction (Gaze & Head)

#### Current Sign Convention Analysis

**GazeTracker (current):**
- `rawDx = gazeDir.x - prev.x` — horizontal delta from eye forward vector X component
- `rawDy = gazeDir.y - prev.y` — vertical delta from eye forward vector Y component
- ARKit eye transform: -Z is forward, X is right, Y is up
- Looking down → eye rotates forward (pitch down) → Y component of forward vector increases (more negative Z becomes less negative, Y goes more negative) → **rawDy is negative for looking down**
- This is inverted: looking down should produce positive dy (cursor moves down)

**HeadTracker (current):**
- `pitch = asin(-m.columns.2.y)` — head pitch from face transform
- `yaw = atan2(m.columns.2.x, m.columns.2.z)` — head yaw from face transform
- `dPitch = α * (pitch - prevPitch)` — delta pitch
- In FusionEngine: `dy = int(-pitch * head_sensitivity)` — **negated**
- Head tilts forward (chin to chest) → ARKit pitch increases → dPitch positive → FusionEngine negates → cursor moves UP
- This is inverted: forward head tilt should move cursor DOWN

#### Required Sign Flips

**GazeTracker fix:**
```swift
// After computing rawDx, rawDy:
let correctedDx = rawDx   // look right → positive dx → cursor right ✓
let correctedDy = -rawDy   // look down → negative rawDy → flip → positive dy → cursor down ✓
```

**HeadTracker fix:**
```swift
// Send pitch without negation:
ws?.sendHeadPose(pitch: Double(dPitch * toDeg), yaw: Double(dYaw * toDeg))
```

**FusionEngine fix (head):**
```python
# Remove the negation on pitch:
dy = int(pitch * self._cfg.head_sensitivity)   # positive pitch → cursor down
dx = int(yaw * self._cfg.head_sensitivity)     # positive yaw → cursor right
```

#### Orientation-Aware Rotation Matrix

For iPad orientation changes, the gaze/head deltas must be rotated to maintain world-aligned mapping:

```swift
private func orientationCorrectedDelta(dx: Float, dy: Float) -> (Float, Float) {
    switch UIDevice.current.orientation {
    case .portrait:
        return (dx, dy)
    case .landscapeLeft:
        return (dy, -dx)   // 90° CW rotation
    case .landscapeRight:
        return (-dy, dx)   // 90° CCW rotation
    case .portraitUpsideDown:
        return (-dx, -dy)  // 180° rotation
    default:
        return (dx, dy)    // fallback to portrait
    }
}
```

Applied after sign correction, before 1-Euro filtering. On orientation change, no spurious delta is produced because the previous frame's direction is also rotated (or the filter is reset if orientation changed between frames).

### Saccade Detection & Fixation Slowdown (Gaze)

#### Velocity Threshold State Machine

```
                ┌──────────────┐
                │   TRACKING   │ (normal output)
                └──────┬───────┘
                       │ velocity > 100°/s
                       ▼
                ┌──────────────┐
                │   SACCADE    │ (output = 0)
                └──────┬───────┘
                       │ velocity < 50°/s for ≥ 30ms
                       ▼
                ┌──────────────┐
                │  RAMP_IN     │ (0→full over 50ms)
                └──────┬───────┘
                       │ 50ms elapsed
                       ▼
                ┌──────────────┐
                │   TRACKING   │
                └──────────────┘
```

#### Hysteresis Thresholds

- **Enter saccade**: velocity > 100°/s (any single frame)
- **Exit saccade**: velocity < 50°/s sustained for 30ms (~2 frames at 60fps)
- **Ramp-in duration**: 50ms linear ramp from 0 to full output after saccade ends

The hysteresis prevents rapid toggling at the boundary. The 50ms ramp prevents a cursor jump to the saccade endpoint.

#### Velocity Computation (iPad-side)

```swift
// Approximate angular velocity from frame-to-frame delta:
let frameRate: Double = 60.0  // ARKit nominal rate
let velocityDegPerSec = sqrt(dx * dx + dy * dy) * (180.0 / .pi) * frameRate
```

#### Fixation Detection

- **Enter fixation**: gaze velocity < 5°/s sustained for 100ms (~6 frames)
- **Exit fixation**: gaze velocity > 20°/s sustained for 30ms (~2 frames)
- **Effect**: cursor movement speed reduced by 50% during fixation (assists target acquisition per Fitts' Law)

#### Confidence Weighting Formula

```swift
// Each frame's cursor contribution is scaled by confidence:
let weightedDx = filteredDx * confidence
let weightedDy = filteredDy * confidence

// Confidence below 0.3 for >500ms → freeze cursor (handled PC-side)
// Resume when confidence ≥ 0.6 for ≥200ms
```

This means a frame at confidence 0.3 contributes half the displacement of a frame at confidence 0.6, providing graceful degradation during partial tracking loss.

### Head Tracker Acceleration Curve

#### Three-Zone Model

```
Zone 1: Tremor Suppression    |input| < 1°/s     → output = 0
Zone 2: Fine Control          1°/s ≤ |input| ≤ 15°/s  → power curve with exponent
Zone 3: Accelerated Traversal |input| > 15°/s    → power curve continues (produces >800 px/s)
```

The zones are not discrete steps — the power curve is continuous across all zones. The tremor suppression zone is a hard cutoff at 1°/s (below which output is exactly zero), but the transition from zone 1 to zone 2 uses hysteresis to prevent oscillation.

#### Power Curve with Configurable Exponent

```python
def head_acceleration_curve(velocity_deg_s: float, exponent: float = 1.8,
                            sensitivity: float = 80.0) -> float:
    """Map head angular velocity to cursor pixels/tick.
    
    Zone 1: |v| < 1.0 → 0 (tremor suppression)
    Zone 2-3: sign(v) * |v|^exponent * sensitivity / 60.0
    
    At default settings:
      3°/s  → |3|^1.8 * 80/60 ≈ 9.7 px/tick → ~580 px/s  (within 50 px/s target? No)
      
    Adjusted: sensitivity calibrated so that:
      3°/s  → ~50 px/s  (fine control)
      15°/s → ~800 px/s (fast traversal)
    
    Solving: sensitivity = 800 / (15^1.8 / 60) ≈ 800 / (132.7/60) ≈ 362
    But at 3°/s: 3^1.8 * 362/60 ≈ 7.2 * 6.0 ≈ 43 px/s ✓ (< 50)
    """
    if abs(velocity_deg_s) < 1.0:
        return 0.0
    
    sign = 1.0 if velocity_deg_s > 0 else -1.0
    magnitude = abs(velocity_deg_s)
    
    # Subtract tremor threshold to ensure continuity at zone boundary
    effective = magnitude - 1.0  # starts at 0 when magnitude = 1.0
    
    return sign * (effective ** exponent) * sensitivity / 60.0  # per-tick displacement
```

#### Stationary Lock with Hysteresis

```python
class HeadStationaryLock:
    LOCK_THRESHOLD = 0.5    # °/s — lock when below this for 200ms
    UNLOCK_THRESHOLD = 1.5  # °/s — unlock when above this
    LOCK_DELAY = 0.2        # seconds of stillness before locking
    
    def __init__(self):
        self.locked = False
        self._still_since: float | None = None
    
    def update(self, velocity_deg_s: float, now: float) -> bool:
        """Returns True if cursor should be locked (no movement)."""
        if self.locked:
            if abs(velocity_deg_s) > self.UNLOCK_THRESHOLD:
                self.locked = False
                self._still_since = None
            return self.locked
        
        if abs(velocity_deg_s) < self.LOCK_THRESHOLD:
            if self._still_since is None:
                self._still_since = now
            elif now - self._still_since >= self.LOCK_DELAY:
                self.locked = True
        else:
            self._still_since = None
        
        return self.locked
```

The lock prevents micro-drift from sensor noise when the user's head is still. The hysteresis gap (lock at 0.5, unlock at 1.5) prevents rapid lock/unlock cycling at the boundary.

### Toggle & Pause System

#### Mutual Exclusion State Machine

```
                    ┌─────────────────┐
                    │   NO_CURSOR     │ (all cursor sensors disabled)
                    └────────┬────────┘
                             │ user enables sensor X
                             ▼
                    ┌─────────────────┐
                    │  SWITCHING      │ (200ms hold window)
                    │  hold cursor    │
                    │  discard data   │
                    └────────┬────────┘
                             │ 200ms elapsed
                             ▼
                    ┌─────────────────┐
                    │  ACTIVE(X)      │ (sensor X driving cursor)
                    └────────┬────────┘
                             │ user enables sensor Y
                             ▼
                    ┌─────────────────┐
                    │  SWITCHING      │ → stop X, start Y, hold 200ms
                    └────────┬────────┘
                             │ 200ms elapsed
                             ▼
                    ┌─────────────────┐
                    │  ACTIVE(Y)      │
                    └─────────────────┘
```

#### SensorManager Implementation (iPad-side)

```swift
/// Cursor sensor mutual exclusion. Only one of tilt/gaze/head can be active.
/// Switching disables the old sensor, enables the new one, and notifies PC
/// to hold cursor for 200ms.
func selectCursorSensor(_ sensor: CursorSensor) {
    guard sensor != activeCursorSensor else { return }
    
    // Disable current
    if let current = activeCursorSensor {
        switch current {
        case .tilt: settings.tiltEnabled = false
        case .gaze: settings.gazeEnabled = false
        case .head: settings.headEnabled = false
        }
    }
    
    // Notify PC to hold cursor
    ws?.sendSensorSwitch(from: activeCursorSensor, to: sensor)
    
    // Enable new (triggers Combine subscription → start)
    activeCursorSensor = sensor
    switch sensor {
    case .tilt: settings.tiltEnabled = true
    case .gaze: settings.gazeEnabled = true
    case .head: settings.headEnabled = true
    }
    
    // Persist selection
    settings.activeCursorSensor = sensor.rawValue
}

enum CursorSensor: String {
    case tilt, gaze, head
}
```

#### 200ms Cursor Hold on Sensor Switch

PC-side in FusionEngine:

```python
def on_sensor_switch(self, from_sensor: str, to_sensor: str) -> None:
    """Hold cursor for 200ms during sensor switch to prevent jump."""
    self._switch_hold_until = time.monotonic() + 0.2
    # Reset relevant filter state for the new sensor
    self._reset_sensor_state(to_sensor)
```

During `_tick()`, if `time.monotonic() < self._switch_hold_until`, all cursor-sensor data is discarded.

#### Sound-Action Pause/Resume

```python
class CursorPauseState:
    def __init__(self):
        self.paused = False
        self._pause_time: float | None = None
        self._last_toggle_time: float = 0.0
        self.AUTO_RESUME_TIMEOUT = 60.0  # seconds
        self.DEBOUNCE = 0.5  # seconds
    
    def toggle(self, now: float) -> bool:
        """Toggle pause state. Returns new paused state."""
        if now - self._last_toggle_time < self.DEBOUNCE:
            return self.paused  # ignore rapid toggles
        
        self._last_toggle_time = now
        self.paused = not self.paused
        
        if self.paused:
            self._pause_time = now
        else:
            self._pause_time = None
        
        return self.paused
    
    def check_auto_resume(self, now: float) -> bool:
        """Returns True if auto-resume should trigger."""
        if self.paused and self._pause_time:
            if now - self._pause_time > self.AUTO_RESUME_TIMEOUT:
                self.paused = False
                self._pause_time = None
                return True
        return False
```

#### Sensor Re-Zeroing on Resume

When resuming from pause, each sensor's reference point is reset to current physical state:
- **Tilt**: capture current gravity as new neutral (same as ratchet)
- **Gaze**: reset 1-Euro filter, set prevGazeDir to current
- **Head**: reset 1-Euro filter, set prevPitch/prevYaw to current

This ensures the cursor stays at its pre-pause position with no jump on resume.

## API Changes

### WebSocket Messages (New or Modified)

#### `tilt_ratchet` (iPad → PC, new)

Triggers re-centering of tilt neutral point. PC holds cursor until dead zone exceeded.

```json
{"type": "tilt_ratchet", "ts": 1234567890.123}
```

#### `cursor_pause` / `cursor_resume` (iPad → PC, new)

Synchronizes pause state between iPad and PC. Sent when sound action triggers pause/resume.

```json
{"type": "cursor_pause", "ts": 1234567890.123}
{"type": "cursor_resume", "ts": 1234567890.123}
```

#### `sensor_switch` (iPad → PC, new)

Notifies PC of cursor sensor change for 200ms hold window.

```json
{
  "type": "sensor_switch",
  "from": "tilt",    // or "gaze", "head", null
  "to": "head",      // or "tilt", "gaze"
  "ts": 1234567890.123
}
```

#### `gaze_delta` (iPad → PC, modified)

Add optional confidence field for PC-side fixation slowdown and freeze logic.

```json
{
  "type": "gaze_delta",
  "dx": 0.0023,
  "dy": -0.0011,
  "conf": 0.85,
  "saccade": false
}
```

#### `head_pose` (iPad → PC, unchanged format)

Sign convention changes are internal — the message format stays the same but values will have corrected signs.

```json
{"type": "head_pose", "pitch": 1.2, "yaw": -0.5}
```

### FusionConfig Changes

```python
@dataclass
class FusionConfig:
    # ... existing fields ...
    
    # 1-Euro filter — tilt velocity
    tilt_vel_min_cutoff: float = 1.0
    tilt_vel_beta: float = 0.007
    tilt_vel_d_cutoff: float = 1.0
    
    # 1-Euro filter — tilt position
    tilt_pos_min_cutoff: float = 0.5
    tilt_pos_beta: float = 0.004
    tilt_pos_d_cutoff: float = 1.0
    
    # Power curve
    tilt_vel_exponent: float = 2.0      # [1.0, 4.0]
    tilt_pos_exponent: float = 1.5      # [1.0, 3.0]
    head_accel_exponent: float = 1.8    # [1.0, 3.0]
    
    # Dead zone ramp
    dead_zone_inner: float = 0.05       # rad/s
    dead_zone_ramp_mult: float = 1.5    # outer = inner * (1 + mult * inner)
    
    # Gyro bias calibration
    bias_stationary_threshold: float = 0.02   # rad/s
    bias_stationary_duration: float = 1.0     # seconds
    bias_lerp_duration: float = 0.5           # seconds
    bias_uncalibrated_suppress: float = 0.05  # rad/s
    
    # Head acceleration
    head_tremor_threshold: float = 1.0        # °/s
    head_fine_max: float = 15.0               # °/s
    head_lock_threshold: float = 0.5          # °/s
    head_unlock_threshold: float = 1.5        # °/s
    head_lock_delay: float = 0.2              # seconds
    
    # Gaze (PC-side thresholds for fixation/confidence)
    gaze_fixation_enter: float = 5.0          # °/s
    gaze_fixation_exit: float = 20.0          # °/s
    gaze_fixation_duration: float = 0.1       # seconds
    gaze_fixation_slowdown: float = 0.5       # multiplier
    gaze_conf_freeze_threshold: float = 0.3
    gaze_conf_freeze_duration: float = 0.5    # seconds
    gaze_conf_resume_threshold: float = 0.6
    gaze_conf_resume_duration: float = 0.2    # seconds
    
    # Pause system
    pause_auto_resume_s: float = 60.0
    pause_debounce_s: float = 0.5
    
    # Sensor switch
    switch_hold_duration: float = 0.2         # seconds
```

### SettingsStore Changes (iPad)

New persisted properties:

```swift
// MARK: - 1-Euro Filter Parameters (per sensor)

@AppStorage("gaze_filter_min_cutoff") var gazeFilterMinCutoff: Double = 1.5
@AppStorage("gaze_filter_beta") var gazeFilterBeta: Double = 0.01
@AppStorage("gaze_filter_d_cutoff") var gazeFilterDCutoff: Double = 1.0

@AppStorage("head_filter_min_cutoff") var headFilterMinCutoff: Double = 1.2
@AppStorage("head_filter_beta") var headFilterBeta: Double = 0.008
@AppStorage("head_filter_d_cutoff") var headFilterDCutoff: Double = 1.0

// MARK: - Cursor Sensor Selection (mutual exclusion)

@AppStorage("active_cursor_sensor") var activeCursorSensor: String = "tilt"

// MARK: - Saccade Detection

@AppStorage("gaze_saccade_enter_threshold") var gazeSaccadeEnter: Double = 100.0  // °/s
@AppStorage("gaze_saccade_exit_threshold") var gazeSaccadeExit: Double = 50.0     // °/s

// MARK: - Head Acceleration

@AppStorage("head_accel_exponent") var headAccelExponent: Double = 1.8
```

Removed properties (replaced by 1-Euro filter):
- `gazeStabilityThreshold` (was EMA alpha) — replaced by `gazeFilterMinCutoff` + `gazeFilterBeta`
- `headSmoothingFactor` (was EMA alpha) — replaced by `headFilterMinCutoff` + `headFilterBeta`

## File Changes

### iPad-Side (Swift)

| File | Changes |
|------|---------|
| `iPadApp/DesktopAgent/Sensors/OneEuroFilter.swift` | **NEW** — Shared 1-Euro filter class used by GazeTracker and HeadTracker |
| `iPadApp/DesktopAgent/Sensors/GazeTracker.swift` | Replace EMA with 1-Euro filter; add axis correction; add saccade detection state machine; add confidence weighting; remove hard dead zone; add blink/tracking-loss reset logic |
| `iPadApp/DesktopAgent/Sensors/HeadTracker.swift` | Replace EMA smoothing factor with 1-Euro filter; fix pitch sign convention; add axis correction for orientation |
| `iPadApp/DesktopAgent/Sensors/TiltSensor.swift` | Add `ratchet()` method with debounce and motion-delay; add `sendRatchet()` WebSocket call; add delayed gravity capture logic |
| `iPadApp/DesktopAgent/SensorManager.swift` | Add mutual exclusion logic (`selectCursorSensor`); add `CursorSensor` enum; add 200ms hold notification; subscribe to cursor sensor selection changes; add pause/resume forwarding |
| `iPadApp/DesktopAgent/SettingsStore.swift` | Add 1-Euro filter parameters; add `activeCursorSensor`; add saccade thresholds; remove `gazeStabilityThreshold` and `headSmoothingFactor` |
| `iPadApp/DesktopAgent/Network/WebSocketManager.swift` | Add `sendRatchet()`, `sendSensorSwitch()`, `sendCursorPause()`, `sendCursorResume()` methods; modify `sendGazeDelta()` to include confidence and saccade flag |
| `iPadApp/DesktopAgent/UI/SensorDashboardView.swift` | Add cursor sensor picker (segmented control: tilt/gaze/head); add pause indicator; add ratchet button |

### PC-Side (Python)

| File | Changes |
|------|---------|
| `one_euro_filter.py` | **NEW** — `OneEuroFilter` class for Python-side filtering |
| `gyro_bias_calibrator.py` | **NEW** — `GyroBiasCalibrator` class with state machine |
| `fusion_engine.py` | Replace EMA smoothing with 1-Euro filter instances (tilt vel + tilt pos); add dead zone ramp function; add power curve function; add gyro bias calibration integration; add ratchet handler; add head acceleration curve + stationary lock; add gaze fixation slowdown + confidence freeze; add pause system; add sensor switch hold; fix head axis sign; update `FusionConfig` with all new parameters |
| `ipad_bridge.py` | Add handlers for `tilt_ratchet`, `cursor_pause`, `cursor_resume`, `sensor_switch` message types; pass confidence field from `gaze_delta` messages |

## Testing Strategy

### Unit Tests (Python — pytest + hypothesis)

| Component | Test Approach |
|-----------|--------------|
| `OneEuroFilter` | Property: output bounded by input range; monotonic response to step input; convergence to steady-state; alpha clamping at extremes; reset produces no jump |
| `dead_zone_ramp` | Property: zero below inner; equals magnitude above outer; monotonically increasing; smoothstep continuity (finite differences); C0 at boundaries |
| `power_curve` | Property: sign preservation; monotonically increasing for positive input; zero at zero; composition with ramp is C0-continuous |
| `GyroBiasCalibrator` | State machine transitions; bias averaging correctness; lerp timing; freeze on motion; uncalibrated suppression |
| `HeadStationaryLock` | Hysteresis: lock/unlock at correct thresholds; no oscillation at boundary; timing requirements |
| `CursorPauseState` | Toggle debounce; auto-resume timing; state consistency |
| Head acceleration curve | Zone boundaries; continuity across zones; output magnitude at reference velocities (3°/s < 50px/s, 15°/s > 800px/s) |

### Unit Tests (Swift — XCTest)

| Component | Test Approach |
|-----------|--------------|
| `OneEuroFilter` (Swift) | Same properties as Python version; verify identical output for same input sequence |
| Axis correction | Known input orientations produce expected sign; orientation rotation matrix correctness |
| Saccade state machine | State transitions at threshold velocities; hysteresis behavior; ramp-in timing |
| Confidence weighting | Linear scaling verified; boundary behavior at 0.0 and 1.0 |
| Ratchet debounce | Rapid triggers within 500ms produce single capture; motion-delay logic |

### Integration Tests

| Scenario | Verification |
|----------|-------------|
| Tilt velocity full pipeline | Inject known rotation rates → verify cursor displacement matches expected (bias + filter + ramp + curve) |
| Tilt position full pipeline | Inject known gravity vectors → verify pixel position matches expected (filter + curve + mapping) |
| Sensor switch | Enable tilt → switch to head → verify 200ms hold → verify head drives cursor |
| Pause/resume | Trigger pause → inject sensor data → verify no cursor movement → resume → verify cursor stays in place |
| Ratchet | Move cursor via tilt → trigger ratchet → verify cursor holds → tilt past dead zone → verify cursor resumes |
| Gaze confidence freeze | Drop confidence below 0.3 → verify cursor freezes after 500ms → raise above 0.6 → verify resume after 200ms |

### Manual Tests (UX Feel)

| Test | What to Evaluate |
|------|-----------------|
| Jitter at rest | Hold iPad still — cursor should not drift (tilt velocity with bias cal) or wobble (tilt position with 1-Euro) |
| Responsiveness | Quick intentional movement — cursor should follow without perceptible lag |
| Dead zone transition | Slowly increase tilt from rest — cursor onset should feel gradual, not snappy |
| Ratchet feel | Trigger ratchet while holding position — cursor should not jump; subsequent tilt should feel natural |
| Head tracking stability | Head still → no drift; slow turn → fine control; fast turn → rapid traversal |
| Gaze saccade | Look quickly to new target — cursor should not overshoot; should arrive smoothly after saccade |
| Sensor switching | Switch between tilt/gaze/head — no cursor jump, brief hold feels natural |
| Pause/resume | Pause → reposition iPad → resume — cursor stays put, no jump |
