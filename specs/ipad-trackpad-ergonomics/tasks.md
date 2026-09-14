# Tasks: iPad Trackpad Ergonomics

> **Gate 2 (AGENTS.md #11).** DRAFT. No task executes until Brad approves `requirements.md`
> (Gate 1) and this file (Gate 2). Do not self-promote `Status:`.

Criterion references are to `requirements.md` §3. All work is in
`TrackpadGestureView.Coordinator`; no PC or protocol changes.

---

## Phase 1 — Guards first

> Both guards land before any behaviour changes, so the rest of the work is gated rather than
> retrospectively blessed.

- [x] **1.1 Off-state regression guard** — with `accelExponent` = 1.0 and
  `momentumDecayPerTick` = 0, assert the emitted `TrackpadEvent` stream is identical to today's for
  identical input. Satisfies R6.1, R6.2.
- [x] **1.2 Tremor-amplification property test** — synthetic oscillation at `tremorFrequencyHz` /
  `tremorAmplitudePoints`; assert accelerated displacement never exceeds unaccelerated. Satisfies
  R2.4. **Write this before 2.x.**

## Phase 2 — Pointer acceleration

- [x] **2.1 Speed estimation** — `hypot(dx, dy) / dt` from callback timestamps, with the R1.7 guard
  for zero/negative/> 100 ms `dt`. Satisfies R1.1, R1.7.
- [x] **2.2 Smooth the speed estimate** with `OneEuroFilter` **before** the curve; reset on gesture
  end. Satisfies R2.1, R2.2, R2.3.
- [x] **2.3 Power_Curve gain** — normalize against `accelReferenceSpeed`, apply exponent, clamp to
  `[1.0, accelMaxFactor]`, leave gain untouched below `accelLowSpeedThreshold`. Satisfies R1.2,
  R1.3, R1.5.
- [x] **2.4 Apply gain to combined magnitude**, equally on both axes, so diagonals stay straight.
  Satisfies R1.6.
- [x] **2.5 Verify exponent 1.0 is byte-identical to linear.** Satisfies R1.4.
- [x] **2.6 Settings UI** — expose `accelExponent` with 1.0 labelled as "off/linear".
- [x] **2.7 Tests** per R1.x and R2.x.

## Phase 3 — Momentum scroll

- [x] **3.1 Capture fling velocity** at scroll-gesture end from the existing scroll accumulator.
  Satisfies R3.1.
- [x] **3.2 Coast timer** owned by the Coordinator; geometric decay per tick; stop below
  `momentumStopVelocity`. Satisfies R3.2, R3.3.
- [x] **3.3 Route coast emissions through the B0 click quantisation; rate bounded by timer cadence**
  — coast ticks emit via `emitScrollIfWhole` (same whole-click quantisation and accumulators), and
  the coast timer's interval *equals* `1/maxScrollMessagesPerSecond`, so the emission rate is
  bounded to the same ceiling. **Caveat:** the bound comes from the timer schedule, not from the
  `lastScrollEmit` gate in `accumulateScroll` — equivalent rate, different mechanism. Satisfies
  R3.4 in effect; noted for the reviewer.
- [x] **3.4 Threshold and direction lock** — no coast below `momentumMinVelocity`; axis fixed at
  gesture end. Satisfies R3.5, R3.7.
- [x] **3.5 Cancellation** — new touch cancels the coast *and is swallowed, not emitted as a
  click*; also cancel on disappear, background, disconnect, and `deinit`; enforce
  `momentumMaxDurationSeconds`. Satisfies R4.1–R4.4.
- [x] **3.6 Verify decay 0 is identical to today.** Satisfies R3.6.
- [x] **3.7 Tests** per R3.x and R4.x, including the tap-to-stop-does-not-click case.

### Phase 3b — Repurpose the dead Scroll toggle (Brad, 2026-08-16)

- [x] **3b.1 Rename `edgeScrollEnabled` → `momentumScrollEnabled`** in `SettingsStore`; do **not**
  migrate the old `UserDefaults` key. Satisfies R7.2.
- [x] **3b.2 Strip the PC sync** — remove the subscription and the `"edge_scroll"` wire mapping from
  `FeatureToggleSyncer`; keep the class and its send/queue/flush machinery, with a comment marking
  it intentionally subscription-free. Satisfies R7.3, R7.4.
- [x] **3b.3 Repoint the `DwellActionToolbar` button** at the renamed setting; update its
  `accessibilityLabel` / `accessibilityHint` to describe momentum. Satisfies R7.1, R7.5.
- [~] **3b.4 Tests — PARTIAL.** `testMomentumDisabledByToggle` covers R7.6. The no-emission half of
  R7.3 has **no runtime test**: `WebSocketManager` has no send-spy seam, so coverage is structural —
  the subscription simply no longer exists in `FeatureToggleSyncer`, and the class comment forbids
  re-adding it. A runtime assertion needs a WS test seam first; deferred rather than faked.
- [x] **3b.5 Update `OverlayPreservationTests`** — it asserts on `edgeScrollEnabled` persistence
  (around line 327) and will not compile after the rename.

## Phase 4 — Flare adaptation

- [x] **4.1 Route `accelExponent` and `momentumMinVelocity` through the flare path**, never inline.
  Satisfies R5.1–R5.3. First iPad-side pain-day adaptation; closes audit G7 for the trackpad.
- [x] **4.2 Tests** — assert both values shift under `manualPainDay`.

## Phase 5 — Close-out

- [x] **5.1 Open Question 1 resolved** (Brad, 2026-08-16) — repurpose, not delete. Executed as
  Phase 3b; specified as R7.
- [ ] **5.2 Manual device pass** — one drag crosses the desktop; slow positioning still hits a small
  target; a flick coasts and settles; a tap during coast stops it without clicking; a slow release
  does not drift. **Required before Shipped, and expect to retune the constants here.**
- [ ] **5.3 Decision entry** if the tuning produces a non-obvious trade-off (AGENTS.md #12).
- [ ] **5.4 Doc update** — note G7 partially closed; run `/doc-update`.

---

## Sequencing note

Phase 1 gates everything. Phases 2 and 3 are independent and can land separately — **Phase 3
(momentum) is the smaller and lower-risk of the two**, so if only one ships first, make it that
one. Phase 2 changes how the cursor feels on every single drag, which is a much larger blast
radius and the one most likely to need retuning.

Phase 4 depends on both. Phase 5.2 is the only gate on calling this done: every constant in §4 of
the spec is a starting guess, and pointer feel is not derivable from first principles.
