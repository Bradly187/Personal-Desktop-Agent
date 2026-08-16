# Spec: iPad Assistive-Technology Compatibility

## 1. Background — the "Why"

The iPad app is Brad's control plane for a Windows desktop, built for rheumatoid arthritis. Its
accessibility story today is entirely **bespoke** — 80 pt targets, palm rejection, tilt dwell,
flare profiles — and genuinely strong on that axis. But it does not compose with the assistive
technology the iPad already ships, which is what gets reached for when the bespoke path is not
enough. A source audit (`docs/audits/2026-08-13-ipad-swift-accessibility-gap-analysis.md`, §G13–G15)
found the app uses **four** accessibility modifiers (`accessibilityLabel` ×46, `accessibilityHint`
×22, `accessibilityAddTraits` ×10, `accessibilityHidden` ×9) and reads **zero** accessibility
environment values.

Three assistive technologies — Switch Control, Voice Control, and Full Keyboard Access — are held
back by **one shared cause**: the app's custom gesture surfaces expose no actionable accessibility
elements. Closing that one cause serves all three. (System Eye Tracking would have been a fourth;
Brad confirmed 2026-08-16 that it is unavailable on the device.)

**Scoped precisely — corrected 2026-08-16.** An earlier draft of this section, and audit finding
G13, claimed there is "no way to drive this app at all" under Switch Control and that it is
"effectively unusable". **Both overstated the gap.** Every button surface in the app is a real
SwiftUI `Button` carrying an `accessibilityLabel` — `CommandPadView`, the custom tab bar,
`MicMuteIndicator`, `DwellActionToolbar`, `TrackpadView`'s click/shortcut/scroll rows, the
`HandwritingCanvasView` controls, and A2UI approval prompts via `DAButton`. All of those are
scannable and activatable by Switch Control and addressable by Voice Control **today**, with no
code change.

What is unreachable is **cursor positioning**. `TrackpadGestureView` is a bare `UIView` with raw
gesture recognizers and no accessibility elements, so an assistive technology can fire a click but
cannot aim it. The honest one-line statement of this spec's value is therefore:

> **You can already click. You cannot aim.** This spec is about aiming.

That is still the decisive gap — aiming is the app's core function and the reason it exists — but
it is a narrower and more defensible claim than "the app is undrivable", and it should be judged
on its real size.

> **This spec is also the successor to Requirement 6** (mouth-sound actions), struck 2026-08-16 as
> D032. R6 wanted "a zero-hand input method that doesn't require forming words"; the in-app
> detector was withdrawn 2026-06-04 because the sounds fired incidentally. iPadOS Switch Control
> **Sound Actions** provide that capability with Apple's tuned detector, system-wide, and with no
> extra always-on consumer of the shared microphone — but they only reach this app once the work
> below exposes actionable accessibility elements. With Eye Tracking confirmed unavailable on the
> device, this is now the **only** route back to a genuinely zero-hand modality, which raises this
> spec's priority relative to the original draft.

**Status:** Draft → [Brad approves spec] → In Progress → [Brad approves tasks.md] → Building → Shipped (PR #___)
**Approved:** <!-- set to "Brad, YYYY-MM-DD" when approving this spec; do NOT self-promote -->
**Owner / author session:** Claude Code

---

## 2. Glossary

- **AT**: Assistive technology built into iPadOS — Switch Control, Voice Control, VoiceOver,
  Eye Tracking, Full Keyboard Access, AssistiveTouch. Distinct from this project's own
  accessibility pipeline.
- **`TrackpadGestureView`**: `UIViewRepresentable` in `iPadApp/DesktopAgent/UI/TrackpadView.swift`
  wrapping a bare `UIView` with one `UIPanGestureRecognizer` and two `UITapGestureRecognizer`s.
  The app's only continuous cursor-control surface.
- **`DwellToolbarContainer`**: overlay in `iPadApp/DesktopAgent/UI/DwellToolbarContainer.swift`
  hosting `DwellActionToolbar`; in `.floating` mode it is repositioned by `DragGesture` only.
- **Nudge**: a single discrete cursor displacement emitted by one AT activation, in points of PC
  screen space.
- **Coarse grid**: an N×N overlay of accessibility elements on the trackpad surface, each mapping
  to a normalized absolute PC screen coordinate. The AT analogue of Voice Control's "Show grid".
- **`cursor_absolute`**: **new** WebSocket message type proposed by this spec (§4) carrying a
  deliberate, unfiltered, non-droppable absolute cursor move.
- **`tilt_position`**: existing message type carrying normalized cursor position from continuous
  tilt input. Evaluated and **rejected** as the transport for this feature — see §4.

---

## 3. Requirements (EARS acceptance criteria)

### Requirement 1: Trackpad surface is actionable by assistive technology

**User Story:** As Brad, I want to move and click the PC cursor using Switch Control, Voice
Control, or Eye Tracking, so that I can still drive the desktop on a day my hands cannot perform a
drag gesture.

#### Acceptance Criteria

1. THE `TrackpadGestureView` SHALL expose an accessibility element that responds to
   `accessibilityCustomAction` with, at minimum: Move Cursor Up, Move Cursor Down, Move Cursor
   Left, Move Cursor Right, Left Click, Right Click, Scroll Up, Scroll Down.
2. WHEN an AT activates a Move Cursor action, THE `TrackpadGestureView` SHALL emit a cursor
   displacement of `atNudgeStepPoints` in the named direction.
3. THE `TrackpadGestureView` SHALL expose a coarse grid of 7 × 7 accessibility elements, each
   labelled by its position (e.g. "Row 2, Column 3"), and WHEN one is activated SHALL emit an
   absolute cursor move to the corresponding normalized PC screen coordinate.
4. IF the WebSocket is not connected when an AT action fires, THEN THE `TrackpadGestureView` SHALL
   surface the failure through the rejection path defined in
   `../ipad-command-delivery-integrity/` and SHALL NOT fail silently. AT-initiated cursor moves,
   clicks, and scrolls are **deliberate commands** under that spec's taxonomy (its R2.2), not
   continuous stream messages — an AT activation is a conscious action even when it drives the same
   underlying cursor path as a touch drag.
5. THE existing touch behaviour (1-finger drag move, 2-finger drag scroll, 1- and 2-finger tap,
   palm rejection by `majorRadius`) SHALL be unchanged for direct touch input.

> **Rationale for 1.3.** Nudges alone are unusable for gross movement — crossing a 3840 px display
> at a 40 pt nudge is ~96 switch activations. The grid gets the cursor within one cell in a single
> activation; nudges then refine. This mirrors how Voice Control's own grid works, so the
> interaction is already familiar.

### Requirement 2: Deliberate absolute cursor moves are never filtered or dropped

**User Story:** As Brad, I want an AT-initiated cursor jump to land exactly where I asked, so that
I am not fighting smoothing meant for a continuous sensor.

#### Acceptance Criteria

1. THE iPadApp SHALL send AT-initiated absolute moves as a `cursor_absolute` message carrying
   normalized `x` and `y` in [0.0, 1.0], NOT as `tilt_position`.
2. THE `WebSocketManager` SHALL NOT classify `cursor_absolute` as a sensor frame type, so it is
   never discarded by the sensor backpressure path.
3. WHEN `FusionEngine` receives `cursor_absolute`, THE `FusionEngine` SHALL move the cursor to
   those coordinates without applying the One-Euro position filters and without requiring tilt to
   be the active cursor sensor.
4. IF `x` or `y` is non-finite or outside [0.0, 1.0], THEN THE `FusionEngine` SHALL clamp to range
   and log at DEBUG, consistent with `on_tilt_position` handling of non-finite input.
5. THE `cursor_absolute` payload SHALL be mirrored in both `core/ipad_bridge.py` and
   `iPadApp/DesktopAgent/Network/WebSocketManager.swift` in the same change (AGENTS.md #3).

### Requirement 3: Floating dwell toolbar is repositionable without dragging

**User Story:** As Brad, I want to move the floating toolbar without performing a drag, so that a
surface advertised as "Drag to reposition" is not unreachable exactly when dragging is what I
cannot do.

#### Acceptance Criteria

1. THE `DwellToolbarContainer` floating toolbar SHALL expose `accessibilityCustomAction`s to move
   it Up, Down, Left, and Right by `atNudgeStepPoints`, and to reset it to centre.
2. WHEN a reposition action fires, THE resulting offset SHALL pass through the existing
   `constrainToSafeArea` clamp so AT-driven movement cannot push the toolbar off-screen.
3. THE resulting offset SHALL persist to `SettingsStore.toolbarFloatingOffset` identically to a
   drag-produced offset.

### Requirement 4: Navigation exposes correct AT semantics

**User Story:** As Brad, I want my AT to understand the tab bar as a tab bar, so that navigating
between surfaces is one predictable gesture rather than a hunt.

#### Acceptance Criteria

1. THE custom tab bar SHALL mark each tab button with the `.isTab` accessibility trait and supply
   an `accessibilityValue` giving position ("3 of 6").
2. THE tab bar SHALL be grouped as an accessibility container so an AT can traverse tabs as a unit.
3. WHILE full-screen trackpad mode is active, THE exit control SHALL remain reachable by AT and
   SHALL carry the `.isButton` trait.
4. THE tab-bar `simultaneousGesture` drag SHALL remain a convenience only; no capability SHALL be
   reachable exclusively through it.

### ~~Requirement 5: System accessibility settings are honoured~~ *(SPLIT OUT 2026-08-16)*

> **Moved** to `../ipad-system-settings-compliance/requirements.md` at Brad's direction. The
> Reduce Motion / Dynamic Type / Bold Text / Differentiate-Without-Color work is mechanical, touches
> 71 call sites across 17 files, and shares no code with the AT-element work below — bundling them
> would make one unreviewably large diff. Numbering is preserved (this spec keeps R6 and R7 at their
> original numbers) so existing references stay valid.

### Requirement 8: Tap disambiguation is order-independent *(Phase B0)*

**User Story:** As Brad, I want a two-finger tap to produce exactly one right-click, so that a
tremor landing my fingers a few milliseconds apart does not fire a stray left-click into whatever
is on screen.

#### Acceptance Criteria

1. WHEN a two-finger tap is recognized, THE `TrackpadGestureView` SHALL emit exactly one
   `.tap(fingers: 2)` and SHALL NOT emit `.tap(fingers: 1)`, **regardless of whether the
   one-finger or two-finger recognizer reports first**.
2. THE one-finger tap SHALL be emitted no later than `tapDisambiguationWindowMs` after
   recognition, where the default is materially below the ~300 ms that `require(toFail:)` would
   impose.
3. IF a one-finger tap is deferred and no two-finger tap arrives within the window, THEN THE
   `TrackpadGestureView` SHALL emit the one-finger tap.
4. THE existing latency benefit of not using `tap1.require(toFail: tap2)` SHALL be preserved.

> **Why this is a precondition, not cleanup.** Today `tap1` suppresses itself only if `tap2`
> *already* fired (`lastTap2Time` is set in `tap2`). UIKit does not guarantee that ordering, so a
> two-finger tap can emit a left-click first. Layering AT click actions onto a surface that already
> emits phantom clicks would make the AT work unverifiable — a stray click during a Switch Control
> pass could not be attributed.

### Requirement 9: Scroll carries magnitude and cannot flood *(Phase B0)*

**User Story:** As Brad, I want a gentle two-finger drag to scroll gently and a firm one to scroll
far, so that scrolling is controllable rather than a fixed-rate stream.

#### Acceptance Criteria

1. THE `TrackpadGestureView` SHALL derive scroll magnitude from accumulated gesture delta, using
   the same fractional-accumulator technique already proven on the move path, rather than sending a
   constant.
2. THE emitted `clicks` value SHALL reflect that magnitude. *(No protocol change required — the PC
   bridge already reads `int(msg.get("clicks", 3))`; only the Swift caller hardcodes the default.)*
3. THE `TrackpadGestureView` SHALL NOT emit more than `maxScrollMessagesPerSecond` scroll messages,
   coalescing accumulated delta into the next emission rather than dropping it.
4. FOR ALL sustained two-finger drags, the total scrolled distance SHALL be proportional to total
   gesture displacement and SHALL NOT depend on display refresh rate.
5. IF the accumulated delta rounds to zero clicks, THEN THE `TrackpadGestureView` SHALL emit
   nothing rather than a zero-magnitude message.

> **Current behaviour.** Every `.changed` callback sends `clicks: 3` in one of four compass
> directions — up to 120 messages/second on ProMotion, each executing 3 scroll clicks on the PC.
> `trackpad` is not in `WebSocketManager._sensorFrameTypes`, so unlike tilt these are *not*
> eligible for backpressure dropping and queue unboundedly. Two seconds of fast scrolling enqueues
> roughly 720 scroll clicks.
>
> **Explicitly out of scope:** diagonal scrolling. `mouse_scroll(cx, cy, direction, clicks)` takes a
> compass string, so two-axis scroll *would* be a protocol change. Deferred.

### Requirement 10: Interaction state lives in the Coordinator *(Phase B0)*

**User Story:** As Brad, I want cursor movement to stay smooth once the trackpad also carries 49
accessibility elements, so that the AT work does not make the touch path worse.

#### Acceptance Criteria

1. THE cursor-move fractional accumulators SHALL live in `TrackpadGestureView.Coordinator`, not as
   `@State` on the SwiftUI view.
2. THE gesture path SHALL NOT invalidate the SwiftUI view graph on a per-frame basis during a drag.
3. THE `Coordinator`'s `onEvent` closure SHALL be refreshed in `updateUIView`, so it cannot go
   stale the way `palmRadius` did.
4. THE accumulators SHALL reset on gesture end, preserving current behaviour.

> **Why this is a precondition.** Writing `@State` from a UIKit gesture callback invalidates the
> view graph and re-runs `body` at gesture frequency, for data SwiftUI never renders. Phase B2 adds
> a 7×7 grid of accessibility elements to this same view — 49 children re-evaluated on every
> cursor move. Fix the churn before adding the children.
>
> R10.3 is the same class of defect as the palm-radius bug fixed 2026-08-16; the stale-capture
> pattern is fixed once, here, for every property.

### Requirement 11: One mechanism arbitrates the page-swipe conflict *(Phase B0)*

**User Story:** As Brad, I want tab paging and trackpad dragging to stop competing through two
different private-hierarchy hacks, so that an iPadOS update cannot silently break cursor control.

#### Acceptance Criteria

1. THE app SHALL use a single mechanism to prevent the `TabView` page-swiper from stealing trackpad
   drags, replacing the current pair (`ContentView.PageScrollDisabler` walking down-up to set
   `isScrollEnabled`, and `Coordinator.linkAncestorScrollViewsIfNeeded()` walking up to call
   `require(toFail:)`).
2. THE replacement SHALL prefer the supported SwiftUI API (`.scrollDisabled(_:)`, available at the
   app's iOS 17 floor) over `UIView.superview` traversal.
3. WHILE the trackpad tab is active, a drag beginning on the trackpad surface SHALL move the cursor
   and SHALL NOT page the TabView.
4. WHILE a non-trackpad tab is active, horizontal page-swiping SHALL continue to work unchanged.
5. IF the supported API proves insufficient, THEN exactly one traversal-based fallback SHALL
   remain, and the redundant one SHALL be deleted.

> **Current risk.** Tab 1 *is* the trackpad, so on that tab the parent already disables scrolling
> and the child's `require(toFail:)` is redundant. `require(toFail:)` is also permanent and retains
> the recognizer — and because ContentView deliberately keeps the TabView alive, those
> relationships accumulate across appearances.

### Requirement 6: AT and the app's own sensors do not fight

**User Story:** As Brad, I want tilt and dwell to stand down when an AT is driving, so that two
cursor controllers are not competing for the same pointer.

#### Acceptance Criteria

1. WHEN `UIAccessibility.isVoiceOverRunning` or `isSwitchControlRunning` is true, THE
   `SensorManager` SHALL suspend tilt-driven cursor output and tilt dwell-to-click.
2. WHEN the AT is dismissed, THE `SensorManager` SHALL restore the prior sensor state.
3. THE suspension SHALL be surfaced in `SensorDashboardView` with the reason, so the sensor
   appearing stopped is not mistaken for a fault.
4. THE iPadApp SHALL observe `UIAccessibility.voiceOverStatusDidChangeNotification` and the
   Switch Control equivalent rather than sampling on a timer.

### Requirement 7: Nudge and dwell thresholds are pain-day aware

**User Story:** As Brad, I want AT step sizes to adapt on a flare day, so that I am not re-tuning
sliders on the day my hands work worst.

#### Acceptance Criteria

1. THE `atNudgeStepPoints` and AT dwell durations SHALL NOT be hardcoded at their use sites; they
   SHALL resolve through the flare-adaptation path (AGENTS.md #5).
2. WHEN `SettingsStore.manualPainDay` is true or a PC pain-day signal is active, THE effective
   nudge step SHALL scale by `atFlareNudgeMultiplier`.

---

## 4. Technical Design

- **Entry point / pipeline boundary:** `FusionEngine` priority level 1 (alongside iPad touch
  command), bypassing sensor-priority gating entirely — an AT action is a deliberate command, not a
  sensor reading.
- **New `Command` fields:** none. `cursor_absolute` resolves to an existing cursor move.
- **Models / VRAM:** none — no inference involved.
- **Persistence:** none. New settings are `UserDefaults` via `SettingsStore`; no `agent.db`
  schema change, so no migration or `PRAGMA user_version` bump.
- **Cross-platform:** `cursor_absolute` must land in `core/ipad_bridge.py` and Swift
  `WebSocketManager` in the same change (AGENTS.md #3), plus `docs/websocket-protocol.md` and the
  CLAUDE.md protocol counts (24 → 25 outbound).

### Rejected: reusing `tilt_position` as the transport

The obvious shortcut is to reuse the existing `tilt_position` message, which already carries
normalized [0.0, 1.0] screen coordinates and already has a Swift sender (`sendTiltPosition(x:y:)`).
**Verified against the code and rejected on three independent grounds:**

1. **It is droppable.** `tilt_position` is a member of `WebSocketManager._sensorFrameTypes`
   (`WebSocketManager.swift:136-138`), so it is silently discarded when `_sendQueueDepth` exceeds
   `_maxSensorQueueDepth`. Correct for a 60 Hz sensor where only the newest sample matters;
   unacceptable for a deliberate AT activation, and it would compound the G9 silent-drop defect.
2. **It would do nothing when tilt is off.** `FusionEngine.on_tilt_position`
   (`core/fusion_engine.py:511`) only stores `self._tilt_position`; the value is consumed later at
   priority 3a, gated on tilt being the active cursor sensor. The flare scenario this spec exists
   for is precisely the one where tilt is disabled.
3. **It would be smoothed.** The tilt position path runs through `_tilt_pos_filter_x/y` One-Euro
   filters tuned for continuous input. A discrete jump through them arrives late and short of
   target — the opposite of "land exactly where I asked" (R2).

A distinct message type keeps sensor semantics and command semantics separate, which is the same
boundary that already justifies `touch_command` bypassing fusion.

### Configuration (flat YAML)

```yaml
ipad_assistive_tech:
  enabled: true              # additive and safe; no legacy behaviour changes when unused
  at_nudge_step_points: 40   # cursor displacement per AT activation, PC screen points
  at_grid_divisions: 7       # 7x7 = 49 elements; ~549px cells on a 3840px display (Brad, 2026-08-16)

  # Phase B0 — trackpad correctness
  tap_disambiguation_window_ms: 80   # R8.2 — deferral before a 1-finger tap commits.
                                     # ~4x better than require(toFail:)'s ~300ms.
  max_scroll_messages_per_second: 30 # R9.3 — coalesce, never drop (vs. up to 120/s today)
  at_flare_nudge_multiplier: 1.5   # routed through pain-day adaptation, never inline (AGENTS.md #5)
  suspend_tilt_under_at: true      # R6.1 — avoid two controllers on one pointer
```

---

## 5. Behavior Verification

- **Swift unit tests** (`iPadApp/Tests/`, run by the existing simulator job in
  `build-ipad-app.yml`): one test per numbered criterion in R1–R4 and R7, named citing the
  criterion. Custom actions are directly assertable — build the view, read
  `accessibilityCustomActions`, invoke, assert the emitted message.
- **Python tests** (`tests/test_fusion_engine.py` or a new `tests/test_cursor_absolute.py`):
  R2.2–R2.4 — non-droppable classification, no filtering, clamping of out-of-range and non-finite
  input.
- **Not eval-suite work.** `evals/` gates model/routing behaviour; this is deterministic UI and
  protocol logic, so XCTest + pytest are the right layer.
- **Manual device pass — required, not optional.** Whether Switch Control and Voice Control *feel*
  usable cannot be settled by static assertion. Before marking Shipped: complete "open an app on
  the PC and click a target" using (a) Switch Control only, (b) Voice Control only, and if the
  hardware supports it (c) Eye Tracking only. Record the result in the PR.

---

## 6. Resolved Decisions

1. **Eye Tracking — NOT available.** Brad confirmed 2026-08-16 that Settings → Accessibility →
   Eye Tracking is absent on the device. Consequence: the R1.3 grid is **not** de-scoped, but its
   justification shifts entirely onto Switch Control and Voice Control, and it loses the
   "hands-free without any hardware" upside. It is no longer a candidate to sequence first.
   *Worth one re-check at some point:* the feature shipped in iPadOS 18 and this device runs
   iPadOS 26, so absence points to a model-level hardware limit rather than an OS version — the
   same class of constraint that killed R3/R4 and R7/R10. Not worth blocking on.
2. **Grid granularity — 7×7** (Brad, 2026-08-16). 49 elements; ~549 px cells on a 3840 px display,
   so one activation lands within ~275 px and ~7 nudges refine. Trades more Switch Control scanning
   steps for materially less nudging.
3. **R5 split out** (Brad, 2026-08-16) → `../ipad-system-settings-compliance/`.

4. **G9 dependency — RESOLVED** (Brad, 2026-08-16). G9 gets its own spec,
   `../ipad-command-delivery-integrity/`, which ships **before** this one, and the chosen behaviour
   is **loud rejection** — not queue-and-replay. R1.4 now consumes that spec's rejection path rather
   than defining its own. Task B3 is unblocked in design and gated only on that spec landing.

## 8. Still Open

Nothing. All four questions raised at draft time are resolved.
