# Tasks: iPad Assistive-Technology Compatibility

> **Gate 2 (AGENTS.md #11).** This plan is DRAFT. No task below may execute until Brad approves
> both `requirements.md` (Gate 1) and this file (Gate 2). Do not self-promote `Status:`.

Criterion references are to `requirements.md` §3.

---

## Phase A — Transport (unblocks everything else)

- [ ] **A1. Add `cursor_absolute` to the Swift sender** — `WebSocketManager.sendCursorAbsolute(x:y:)`;
  explicitly NOT added to `_sensorFrameTypes`. Satisfies R2.1, R2.2.
- [ ] **A2. Add `cursor_absolute` handler to `core/ipad_bridge.py`** — clamp to [0,1], reject
  non-finite, dispatch to `FusionEngine`. Satisfies R2.4, R2.5.
- [ ] **A3. Add `FusionEngine.on_cursor_absolute(x, y)`** — priority-1 path, no One-Euro filtering,
  no active-sensor gate. Satisfies R2.3.
- [ ] **A4. Python tests** — `tests/test_cursor_absolute.py`: non-droppable, unfiltered, clamped,
  works with tilt disabled. One test per R2.x.
- [ ] **A5. Protocol docs** — `docs/websocket-protocol.md` + CLAUDE.md counts 24 → 25 outbound.

## Phase B0 — Trackpad correctness (must land before Phase B)

> From the 2026-08-16 trackpad best-practices review. These are preconditions, not cleanup: Phase B
> layers AT click/scroll actions and 49 accessibility elements onto this exact surface, so its
> phantom-click path and per-frame view invalidation must go first or the AT work is unverifiable.
>
> **Already fixed, not a task:** the palm-radius stale-capture bug (`updateUIView` never refreshed
> `Coordinator.palmRadius`, making the Settings slider inert for the whole session) — fixed
> 2026-08-16 with `TrackpadGestureView.applyLiveSettings` + `Tests/PalmRadiusLiveUpdateTests.swift`.
> The dead `speed` property was removed in the same change.

- [x] **B0.1 Order-independent tap disambiguation** — `onSingleTapRecognized` now defers via a
  cancellable `DispatchWorkItem` (ordering B) *and* drops stragglers via `lastTap2Time`
  (ordering A). `tapDisambiguationWindow` 80 ms. Satisfies R8.1–R8.4.
- [x] **B0.2 Scroll magnitude** — fractional scroll accumulator + `scrollPointsPerClick`; emits
  real `clicks`. Swift-only, as predicted — the PC already read `int(msg.get("clicks", 3))`.
  Satisfies R9.1, R9.2, R9.4, R9.5.
- [x] **B0.3 Scroll rate limit** — `maxScrollMessagesPerSecond` 30, with leftover travel carried
  forward and flushed on `endGesture`. Satisfies R9.3.
- [x] **B0.4 Move accumulators into the Coordinator** — `@State accumX/accumY` deleted from the
  view; `TrackpadEvent` now carries whole units so `handle(_:)` is a thin dispatcher.
  Satisfies R10.1, R10.2, R10.4.
- [x] **B0.5 Refresh `onEvent` in `updateUIView`** — `applyLiveSettings` now refreshes
  `palmRadius`, `speed`, and `onEvent` together. Satisfies R10.3.
- [ ] **B0.6 Collapse the two scroll-arbitration hacks to one** — **DEFERRED, not attempted.**
  See below.
- [x] **B0.7 Swift tests** — `Tests/TrackpadCorrectnessTests.swift`, 15 tests. R8.1 asserts **both**
  recognizer orderings. R11 has no tests because B0.6 was not attempted.

> **Why B0.6 was deferred.** It is the only B0 item whose correctness cannot be established by
> unit test — it changes live gesture arbitration between the trackpad pan and the `TabView` page
> swiper, and the failure modes (cursor drags page the tab, or paging dies app-wide) are only
> observable on a device. This session has no simulator or device: the host is Windows, UIKit is
> unavailable, and CI builds but does not exercise gestures. Changing it blind risked breaking
> working cursor control to tidy an internal seam.
>
> There is also a specific technical doubt worth recording: `.scrollDisabled(_:)` sets
> `\.isScrollEnabled` in the environment, and it is **not documented to affect `TabView`'s
> `.page` style**, which owns its `UIScrollView` privately. R11.2 assumes it works; that assumption
> is unverified. R11.5 already permits keeping one traversal fallback — likely the outcome.
>
> Everything else in B0 is behaviour-preserving under unit test and safe to land without a device.

### Deferred from the review — logged, not scheduled

Recorded so they are not rediscovered. None block Phase B.

- **Palm rejection is initial-touch-only.** `shouldReceive` fires once per touch delivery, so a palm
  landing mid-gesture is never filtered; `touch.type` is ignored, so an Apple Pencil contact
  (tiny radius) always passes. Revisit if false activations are observed in practice.
- **Swift 6 concurrency.** `Coordinator` is an unannotated `NSObject` calling `@MainActor` state.
  Correct at runtime; unverifiable by the compiler. Revisit when moving off `SWIFT_VERSION 5.10`.
- **`default:` in the pan state switch** catches `.possible` alongside `.ended`/`.cancelled`/
  `.failed`. Harmless today; `case .ended, .cancelled, .failed:` is clearer.
- **Diagonal scrolling.** Would require a `mouse_scroll` protocol change (compass string → 2-axis).

## Phase B — Trackpad AT elements (the structural core)

- [ ] **B1. Custom actions on `TrackpadGestureView`** — 8 actions: 4 directional nudges, left/right
  click, scroll up/down. Satisfies R1.1, R1.2.
- [ ] **B2. Coarse grid overlay** — `atGridDivisions`² child accessibility elements with positional
  labels, each emitting `cursor_absolute`. Satisfies R1.3.
- [ ] **B3. Disconnect behaviour** — classify AT-initiated cursor actions as *deliberate commands*
  in the `../ipad-command-delivery-integrity/` taxonomy so they inherit its rejection path. No
  local implementation. **Gated on that spec landing** (Brad chose loud rejection + separate spec,
  2026-08-16). Satisfies R1.4.
- [ ] **B4. Regression guard** — assert direct-touch pan/tap/palm-rejection behaviour is byte-identical.
  Satisfies R1.5.
- [ ] **B5. Swift tests** — one per R1.x; invoke each custom action, assert the emitted message.

## Phase C — Remaining AT surfaces

- [ ] **C1. Floating toolbar reposition actions** — 5 actions through `constrainToSafeArea`,
  persisting to `SettingsStore`. Satisfies R3.1–R3.3.
- [ ] **C2. Tab bar semantics** — `.isTab` trait, `accessibilityValue` position, container grouping,
  full-screen exit reachability. Satisfies R4.1–R4.4.
- [ ] **C3. Swift tests** for C1–C2.

## Phase D — AT arbitration

- [ ] **D1. Observe AT status notifications** — `voiceOverStatusDidChangeNotification` + Switch
  Control equivalent, no polling. Satisfies R6.4.
- [ ] **D2. Suspend/restore tilt cursor + dwell under AT** in `SensorManager`. Satisfies R6.1, R6.2.
- [ ] **D3. Surface the suspension reason** in `SensorDashboardView`. Satisfies R6.3.
- [ ] **D4. Pain-day routing** for `atNudgeStepPoints` and AT dwell — through the flare path, never
  inline. Satisfies R7.1, R7.2.
- [ ] **D5. Swift tests** for D1–D4.

## ~~Phase E — System settings compliance~~ *(SPLIT OUT 2026-08-16)*

> Moved to `../ipad-system-settings-compliance/tasks.md` at Brad's direction. Independent of every
> phase here — it can land before, after, or in parallel. Phase letters below are unchanged so
> existing references stay valid.

## Phase F — Verification and close-out

- [ ] **F1. Manual device pass** — complete "open an app on the PC and click a target" using Switch
  Control only, then Voice Control only. (Eye Tracking dropped — confirmed unavailable on the
  device, 2026-08-16.) Record in the PR. **Required before Shipped.**
- [ ] **F2. Decision entry** — log the `tilt_position`-rejection rationale in `docs/decisions.md`
  (AGENTS.md #12); it has a real rejected alternative and will otherwise be re-derived.
- [ ] **F3. Doc update** — refresh the audit's G13/G14/G15 status; run `/doc-update`.

---

## Sequencing note

**`../ipad-command-delivery-integrity/` ships before this spec** (Brad, 2026-08-16) — it closes the
audit's highest-severity finding and task B3 consumes its rejection path.

**A → B0 → B.** Phase A gates B because the grid cannot exist before its transport. B0 gates B
because the AT actions attach to the same recognizers and the same view whose state handling B0
repairs — adding 49 accessibility children to a view that already re-evaluates `body` on every
cursor move (R10.2) would compound the problem, and adding AT click actions to a surface with a
phantom-click path (R8.1) would make the Phase F1 manual pass impossible to interpret.

Phases C and D are independent of each other and of B. `../ipad-system-settings-compliance/` is
independent of all of it.

**Phase F1 is the only gate on calling this done.** Every phase above it is necessary but not
sufficient — none can tell you whether the result is actually usable.

B0 is also the natural standalone first PR: it is pure correctness on existing specced behaviour,
carries no new surface area, and is valuable even if the AT work is never scheduled.
