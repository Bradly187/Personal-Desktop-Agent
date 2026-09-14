# Spec: iPad Trackpad Ergonomics — Pointer Acceleration & Momentum Scroll

## 1. Background — the "Why"

Two requests from Brad, 2026-08-16, both about the same thing: **sustained contact is the expensive
part of using the trackpad, and both current gestures demand too much of it.**

**Crossing the desktop costs too many drags.** `Coordinator.accumulateMove` applies pure linear
gain — `accumX += dx * speed`, where `trackpadSpeed` defaults to 2.0. A 100 pt drag moves the
cursor 200 px, so traversing a 3840 px display needs ~1920 pt of finger travel. The trackpad
surface is a few hundred points wide, so a single traversal means repeatedly lifting and
re-dragging (clutching). Every clutch is a separate joint actuation.

**Scrolling requires holding a two-finger drag** for the whole distance. There is no coasting: the
scroll stops the instant the fingers lift. Long pages mean long sustained contact.

Both are fixed by making the trackpad's *transfer functions* smarter rather than asking for more
movement. Neither needs a PC change — the iPad already computes final pixel deltas and scroll
click counts, and `core/ipad_bridge.py` accepts both as-is.

**Status:** ~~Draft~~ → ~~In Progress~~ → **Building** → Shipped (PR #___)
**Approved:** Brad, 2026-08-16 — Gate 1 ("go ahead and promote") and Gate 2 ("go ahead with
everything"), both in conversation.
**Owner / author session:** Claude Code

---

## 2. Glossary

- **Power_Curve**: the transfer function already defined by `../sensor-refinement/` R5 —
  `output = sign(input) · |input|^exponent`. Exponent 1.0 is exactly linear; above 1.0 gives fine
  control at small inputs and fast traversal at large ones. Reused here rather than reinvented, so
  the trackpad and tilt sensor share one concept and one tuning vocabulary.
- **Gain**: the multiplier from finger travel (points) to cursor travel (pixels). Today a constant
  (`trackpadSpeed`); this spec makes it a function of pointer speed.
- **Clutch**: lifting and repositioning the finger to continue a drag that ran out of surface. The
  cost this spec exists to reduce.
- **`OneEuroFilter`**: the existing adaptive low-pass filter at
  `iPadApp/DesktopAgent/Sensors/OneEuroFilter.swift` (Casiez et al.) — strong smoothing when slow,
  light smoothing when fast. Already in the codebase; reused, not rewritten.
- **Coast**: continued scrolling after the fingers lift, decaying to a stop.
- **Fling velocity**: pointer speed at the moment of lift, in points/second, which seeds a coast.

---

## 3. Requirements (EARS acceptance criteria)

### Requirement 1: Cursor gain rises with pointer speed

**User Story:** As Brad, I want a fast drag to cover far more screen than a slow one, so that
crossing the desktop takes one motion instead of several clutches.

#### Acceptance Criteria

1. THE `Coordinator` SHALL compute pointer speed per move sample as `hypot(dx, dy) / dt` in
   points/second, using the gesture-callback timestamps.
2. THE `Coordinator` SHALL apply a Power_Curve to *normalized pointer speed* to derive an
   acceleration factor, and multiply the base `trackpadSpeed` gain by it.
3. THE effective gain SHALL be clamped to `[trackpadSpeed × 1.0, trackpadSpeed × accelMaxFactor]`
   so acceleration can never reduce gain below today's behaviour, nor exceed a bounded ceiling.
4. WHEN `accelExponent` is 1.0, THE resulting cursor motion SHALL be **byte-identical** to the
   current linear behaviour — this is the off switch, and no separate toggle SHALL be added.
5. WHEN pointer speed is at or below `accelLowSpeedThreshold`, THE gain SHALL equal the unmodified
   `trackpadSpeed`, preserving today's precision for slow, deliberate positioning.
6. THE acceleration SHALL be applied to the **combined** speed magnitude and the resulting gain
   applied equally to both axes, so a diagonal drag does not curve.
7. IF `dt` is zero, negative, or implausibly large (> 100 ms, i.e. a stalled callback), THEN THE
   `Coordinator` SHALL fall back to unaccelerated gain for that sample rather than compute a
   spurious speed.

### Requirement 2: Filter before accelerating — tremor must not be amplified

**User Story:** As Brad, I want acceleration to help me cross the screen without amplifying my
tremor, because tremor *is* fast small movement and that is exactly what acceleration rewards.

#### Acceptance Criteria

1. THE `Coordinator` SHALL apply `OneEuroFilter` smoothing to the pointer-speed estimate **before**
   that estimate is fed to the Power_Curve, mirroring the ordering mandated by
   `../sensor-refinement/` R5.1 for tilt.
2. THE filter SHALL be applied to the speed *magnitude*, not to the raw per-axis deltas, so
   smoothing cannot introduce directional bias.
3. THE filter state SHALL be reset on gesture end, so a new drag does not inherit the previous
   drag's velocity.
4. FOR ALL oscillating inputs at or above `tremorFrequencyHz` with amplitude at or below
   `tremorAmplitudePoints`, the accelerated output displacement SHALL NOT exceed the unaccelerated
   output displacement for the same input.

> **This is the requirement most likely to be skipped and most damaging to skip.** Naive pointer
> acceleration multiplies gain by instantaneous speed; a tremor spike is a high instantaneous
> speed over a tiny distance, so the cursor jumps. R2.4 is the property test that prevents
> shipping that.

### Requirement 3: A two-finger flick coasts

**User Story:** As Brad, I want to flick two fingers and have the page keep scrolling, so that a
long page costs one motion instead of a sustained drag.

#### Acceptance Criteria

1. WHEN a two-finger scroll gesture ends with fling velocity at or above `momentumMinVelocity`,
   THE `Coordinator` SHALL continue emitting scroll events without further contact.
2. THE coast velocity SHALL decay geometrically by `momentumDecayPerTick` on each emission tick.
3. THE coast SHALL stop when velocity falls below `momentumStopVelocity`, and SHALL emit nothing
   further until a new gesture begins.
4. THE coast SHALL emit through the **existing** rate limiter and click quantisation from
   `../ipad-assistive-tech-compat/` R9, so momentum cannot bypass flood protection or emit
   fractional clicks.
5. WHEN fling velocity is below `momentumMinVelocity`, THE gesture SHALL end exactly as it does
   today with no coast — a slow, deliberate scroll must not drift on release.
6. WHEN `momentumDecayPerTick` is 0, THE behaviour SHALL be identical to today's — the off switch.
7. THE coast SHALL preserve the direction resolved at gesture end and SHALL NOT change axis
   mid-coast.

### Requirement 4: A coast is interruptible and cannot outlive its context

**User Story:** As Brad, I want to stop a coast by touching the trackpad, so that momentum never
takes control away from me.

#### Acceptance Criteria

1. WHEN any new touch begins on the trackpad surface WHILE a coast is running, THE `Coordinator`
   SHALL cancel the coast immediately and SHALL NOT emit the touch as a click.
2. WHEN the view disappears, the app backgrounds, or the WebSocket disconnects, THE coast SHALL be
   cancelled.
3. THE coast SHALL be cancelled in `deinit`, so a torn-down coordinator cannot keep a timer alive.
4. THE total coast duration SHALL NOT exceed `momentumMaxDurationSeconds` regardless of decay
   settings, as a backstop against a mistuned decay scrolling indefinitely.

> R4.1 matters more than it looks: tap-to-stop is the universally learned gesture for killing
> momentum, and a user reaching to stop a runaway scroll must not instead issue a click into
> whatever the runaway scroll landed on.

### Requirement 5: Both behaviours adapt on a flare day

**User Story:** As Brad, I want acceleration to back off when my hands are worse, so that I am not
re-tuning sliders on the day I can least afford to.

#### Acceptance Criteria

1. WHEN `SettingsStore.manualPainDay` is true or a PC pain-day signal is active, THE effective
   `accelExponent` SHALL be reduced toward 1.0 by `flareAccelDamping`, making the pointer more
   predictable and less tremor-sensitive.
2. WHEN a pain-day signal is active, THE `momentumMinVelocity` SHALL be raised by
   `flareMomentumThresholdScale`, so an unsteady release is less likely to trigger an unintended
   coast.
3. THE pain-day values SHALL NOT be hardcoded at their use sites; they SHALL resolve through the
   same flare path as other adaptive thresholds (AGENTS.md #5).

> This is the first iPad-side implementation of pain-day adaptation. Audit finding G7 records that
> the iPad *reports* flare state to the PC but never adapts its own thresholds. This closes that
> for the trackpad specifically; tilt/tap/palm remain open.

### Requirement 7: Momentum has a reachable toggle, repurposed from dead UI

**User Story:** As Brad, I want momentum on/off within reach on a bad day, and I do not want a
button in my accessibility toolbar that silently does nothing.

#### Acceptance Criteria

1. THE `DwellActionToolbar` scroll button SHALL toggle momentum scrolling, replacing its current
   `edgeScrollEnabled` behaviour. It already carries a scroll icon and a 44 pt target.
2. THE backing setting SHALL be renamed `edgeScrollEnabled` → `momentumScrollEnabled`, and the
   abandoned `UserDefaults` key SHALL **NOT** be migrated — it stored a preference for a feature
   that never functioned, so carrying its value forward would carry meaningless state.
3. THE setting SHALL NOT be synced to the PC. Momentum is entirely iPad-side, so
   `FeatureToggleSyncer`'s `edgeScrollEnabled` subscription and its `"edge_scroll"` wire mapping
   SHALL be removed.
4. `FeatureToggleSyncer` itself SHALL be **retained** with zero subscriptions — send, queue, and
   flush machinery intact — mirroring the PC's deliberate `FusionEngine.VALID_FEATURES = set()`,
   which keeps the `set_feature_toggle` path wired "without special-casing" for a future toggle.
   The class SHALL carry a comment saying it is intentionally subscription-free, so it is not
   mistaken for dead code and deleted.
5. THE button's `accessibilityLabel` and `accessibilityHint` SHALL describe momentum, not edge
   scroll.
6. WHEN `momentumScrollEnabled` is false, THE scroll behaviour SHALL be identical to today's,
   consistent with R3.6.

> **Why this is in scope rather than a separate cleanup.** The button is not merely dead — it is
> *misleading*. It renders an active state, occupies a 44 pt target in the toolbar built for the
> worst days, and every press logs `Unknown feature toggle: edge_scroll` on the PC while changing
> nothing. Leaving it while adding a momentum feature it visually implies it controls would be
> worse than either fixing or removing it.

### Requirement 6: Existing behaviour is preserved and testable

#### Acceptance Criteria

1. FOR ALL settings at their defaults-off values (`accelExponent` = 1.0,
   `momentumDecayPerTick` = 0), the emitted `TrackpadEvent` stream SHALL be identical to the
   current implementation for identical input.
2. THE B0 guarantees SHALL be preserved: fractional move accumulation (no `Int` truncation loss),
   scroll magnitude proportional to travel, coalescing rather than dropping, and order-independent
   tap disambiguation.
3. THE palm-rejection path SHALL be unchanged.

---

## 4. Technical Design

- **Entry point:** `TrackpadGestureView.Coordinator` in `iPadApp/DesktopAgent/UI/TrackpadView.swift`
  — the same class B0 made the owner of interaction state. Both features land there.
- **New `Command` fields:** none.
- **Protocol:** **no change.** Acceleration alters the integer `dx`/`dy` already sent in
  `trackpad`/`move`; momentum emits the `trackpad`/`scroll` events that already carry `clicks`.
  `core/ipad_bridge.py` needs no edit, so AGENTS.md #3 does not apply.
- **Persistence:** new settings are `UserDefaults` via `SettingsStore`; no `agent.db` change, no
  migration, no `PRAGMA user_version` bump.
- **Reuse:** `OneEuroFilter.swift` as-is (`init(minCutoff:beta:dCutoff:)` / `filter(_:timestamp:)`
  / `reset()`), and the Power_Curve definition from `../sensor-refinement/` R5.
- **Coast timer:** a `CADisplayLink` or repeating `DispatchSourceTimer` owned by the Coordinator,
  invalidated per R4. Emissions still pass through the B0 rate limiter, so the timer's tick rate is
  decoupled from the emission rate.

### Configuration (flat YAML)

```yaml
ipad_trackpad_ergonomics:
  # Requirement 1 — pointer acceleration
  accel_exponent: 1.6              # 1.0 == today's linear behaviour (the off switch)
  accel_max_factor: 6.0            # ceiling on the gain multiplier
  accel_low_speed_threshold: 120   # pt/s below which gain is untouched (precision zone)
  accel_reference_speed: 900       # pt/s that normalizes to 1.0 before the curve

  # Requirement 2 — tremor safety
  speed_filter_min_cutoff: 1.0     # OneEuroFilter defaults; tune on device
  speed_filter_beta: 0.007
  tremor_frequency_hz: 4.0         # R2.4 property-test bound
  tremor_amplitude_points: 8.0

  # Requirement 3/4 — momentum scroll
  momentum_decay_per_tick: 0.94    # 0 == off
  momentum_min_velocity: 350       # pt/s fling floor to start a coast
  momentum_stop_velocity: 40       # pt/s at which the coast ends
  momentum_max_duration_seconds: 3.0

  # Requirement 5 — flare adaptation (never inline; AGENTS.md #5)
  flare_accel_damping: 0.5         # pulls accel_exponent toward 1.0
  flare_momentum_threshold_scale: 1.5
```

> All numbers are **starting points, not findings.** Pointer feel cannot be derived from first
> principles; expect to retune `accel_exponent` and `momentum_decay_per_tick` on device.

---

## 5. Behavior Verification

- **Swift unit tests** (`iPadApp/Tests/`, existing simulator CI job), one per numbered criterion.
  The B0 refactor makes this straightforward: `Coordinator` methods take injectable `now:`
  timestamps and emit through a capturable closure, so velocity, acceleration, and coast decay are
  all assertable without a device or a real timer.
- **R2.4 is the important test.** Drive a synthetic oscillation at `tremorFrequencyHz` /
  `tremorAmplitudePoints` and assert accelerated displacement does not exceed unaccelerated. Write
  it *before* the acceleration code, so it is a gate rather than a rubber stamp.
- **R6.1 regression guard** — with both features at their off values, assert the event stream is
  identical to the current implementation. Land this first, as with the settings-compliance spec.
- **Not eval-suite work** — deterministic UI/geometry, no model behaviour.
- **Manual device pass — required before Shipped.** Numbers above are guesses. Verify: one drag
  crosses the desktop; slow positioning still lands on a small target; a flick coasts and stops
  naturally; a tap during coast stops it without clicking; a slow scroll release does not drift.

---

## 6. Resolved Decisions

1. **Dead "Scroll" toggle repurposed as the momentum switch** (Brad, 2026-08-16) — option (a).
   Specified as Requirement 7. Fixes live-but-dead UI and gives momentum a reachable toggle in the
   same change.
