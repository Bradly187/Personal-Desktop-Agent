# Spec: iPad System Accessibility Settings Compliance

## 1. Background — the "Why"

Split out of `../ipad-assistive-tech-compat/` (was its Requirement 5) at Brad's direction,
2026-08-16. That spec makes the app *drivable* by assistive technology; this one makes the app
*respect the settings Brad has already chosen* on the device.

The app reads **zero** accessibility environment values today. It ships 71 fixed
`.font(.system(size:))` call sites across 17 files and no `@ScaledMetric`, a `repeatForever`
pulse animation that plays regardless of Reduce Motion, and a `minimumScaleFactor(0.8)` that
actively shrinks label text *away from* the size the user asked for. `DesignTokens.Typography`
correctly uses relative text styles — the foundation is right; the sizing layer never followed.

This work is mechanical, wide, and shares no code with the AT-element work. Kept separate so
neither review is dominated by the other's diff.

**Status:** Draft → [Brad approves spec] → In Progress → [Brad approves tasks.md] → Building → Shipped (PR #___)
**Approved:** <!-- set to "Brad, YYYY-MM-DD" when approving this spec; do NOT self-promote -->
**Owner / author session:** Claude Code

---

## 2. Glossary

- **Dynamic Type**: the system text-size setting, exposed to SwiftUI as `\.dynamicTypeSize` and to
  layout via `@ScaledMetric`. Accessibility sizes are `.accessibility1`–`.accessibility5`.
- **`DesignTokens`**: `iPadApp/DesktopAgent/DesignSystem/DesignTokens.swift` — the single source of
  sizing/spacing/typography constants. `Typography` already uses relative styles; `Size` is fixed.
- **Reduce Motion**: `\.accessibilityReduceMotion`. Signals that large or repeating motion should
  be replaced with instant or cross-fade transitions.
- **Differentiate Without Color**: `\.accessibilityDifferentiateWithoutColor`. Signals that no
  state may be conveyed by hue alone.

---

## 3. Requirements (EARS acceptance criteria)

> Numbered R1–R5 here. These were R5.1–R5.6 in the parent spec; the mapping is noted per
> requirement so existing references resolve.

### Requirement 1: Reduce Motion is honoured *(was R5.1)*

**User Story:** As Brad, I want repeating and sliding animation to stop when I have asked the
system for less motion, so that the app does not add visual fatigue on a bad day.

#### Acceptance Criteria

1. WHEN `accessibilityReduceMotion` is enabled, THE `DwellActionToolbar` SHALL NOT run the
   `repeatForever(autoreverses:)` pulse on the drag button; it SHALL convey the dragging state by a
   static cue instead.
2. WHEN `accessibilityReduceMotion` is enabled, THE dwell progress ring SHALL update without
   animated interpolation.
3. WHEN `accessibilityReduceMotion` is enabled, THE tab transition SHALL be instant rather than the
   0.15 s cross-fade, and the `TabView` page-slide SHALL be suppressed.
4. THE iPadApp SHALL read the setting reactively via `@Environment`, so a mid-session change in
   Settings takes effect without an app restart.

### Requirement 2: Dynamic Type drives sizing, not just text *(was R5.2, R5.6)*

**User Story:** As Brad, I want icons and buttons to grow with my chosen text size, so that turning
up Dynamic Type actually helps rather than clipping labels.

#### Acceptance Criteria

1. THE iPadApp SHALL scale icon glyph sizes with Dynamic Type via `@ScaledMetric`, replacing fixed
   `.font(.system(size: DesignTokens.Size.iconSize))` usage.
2. THE iPadApp SHALL size control frames to their content, with the existing size token as an
   absolute floor — `touchTargetMin` (80 pt) for primary actions, `touchTargetCompact` (64 pt) for
   secondary. A frame SHALL grow exactly as much as its content requires and SHALL NOT be multiplied
   by a Dynamic Type factor, capped at a ceiling, or shrunk below its floor.
3. FOR ALL controls, at `dynamicTypeSize` `.accessibility3` and above, the control's label SHALL
   remain legible without truncation or clipping.
4. THE `CommandPadView` grid SHALL relax its `maximum: 160` column cap as Dynamic Type increases,
   so two-line labels are not clipped at accessibility sizes.
5. WHERE a fixed glyph size is genuinely required for layout stability, the call site SHALL carry a
   comment stating why, so the exception is deliberate and reviewable.
6. WHEN `dynamicTypeSize` is at the system default, THE rendered layout SHALL be visually identical
   to the pre-change layout — at default size every control's content is smaller than its floor, so
   the floor governs and nothing moves.

> **Why content-sizing rather than a multiplier or a ceiling** (Brad, 2026-08-16). R2.3 forbids
> truncation and R3.1 forbids `minimumScaleFactor`, so the frame *must* grow enough to hold its
> label — that part is forced, not chosen. The only real question was whether it grows beyond what
> the text needs. Content-sizing answers it by construction: R2.3 cannot be violated because the
> frame is derived from the content that must fit. A fixed multiplier (~1.5×) was rejected because
> it can still clip — if a two-line label at `.accessibility5` needs more than 120 pt, a capped
> frame truncates, which is the exact defect this spec exists to remove. Uncapped proportional
> scaling was rejected as wasteful: it grows frames past what the content needs, costing visible
> items and forcing scrolling, which has its own joint cost on a flare day.
>
> R2.6 is the regression guard that makes this cheap to review: nothing changes until Brad actually
> raises the text size.

### Requirement 3: Text is never shrunk below the chosen size *(was R5.3)*

**User Story:** As Brad, I want the app to stop second-guessing my text-size choice.

#### Acceptance Criteria

1. THE iPadApp SHALL NOT apply `minimumScaleFactor` to any control label.
2. WHERE a label cannot fit at large Dynamic Type sizes, THE layout SHALL reflow (wrap, stack
   vertically, or scroll) rather than scale the text down.

### Requirement 4: Bold Text is honoured *(was R5.4)*

#### Acceptance Criteria

1. WHEN `legibilityWeight` is `.bold`, THE iPadApp SHALL render control labels at semibold weight
   or heavier.
2. THE setting SHALL be read reactively via `@Environment`.

### Requirement 5: No state is conveyed by colour alone *(was R5.5)*

**User Story:** As Brad, I want to read the app's state without relying on hue discrimination.

#### Acceptance Criteria

1. WHEN `accessibilityDifferentiateWithoutColor` is enabled, THE iPadApp SHALL accompany every
   colour-coded state with a shape, glyph, or text cue.
2. THE connection state indicator SHALL remain distinguishable without colour. *(Already partly
   satisfied — `DAConnectionBanner` renders a text status title alongside the colour.)*
3. THE `DwellActionToolbar` active-action state SHALL remain distinguishable without colour.
   *(Already partly satisfied — a 2.5 pt border accompanies the accent fill.)*
4. THE `SensorActivityBar` running/stopped state SHALL remain distinguishable without colour.

---

## 4. Technical Design

- **Entry point / pipeline boundary:** none — this is presentation-layer only. No WebSocket
  messages, no `Command` DTO changes, no PC-side changes, no protocol sync obligation.
- **Persistence:** none. No `agent.db` change, no migration, no `PRAGMA user_version` bump.
- **Primary surface:** `DesignTokens.swift` gains Dynamic-Type-aware accessors; call sites migrate
  to them. Doing it in the token layer is what keeps this from being 71 unrelated edits.
- **Risk:** this touches nearly every view file. The mitigating factor is that it is mechanical and
  the existing 20-file XCTest suite plus the simulator CI job will catch layout regressions early.

### Configuration

None. These are system settings; adding app-level toggles for them would defeat the purpose.

---

## 5. Behavior Verification

- **Swift unit tests** (`iPadApp/Tests/`, existing simulator CI job): one test per numbered
  criterion, named citing it. Environment-driven behaviour is testable by injecting
  `\.dynamicTypeSize`, `\.accessibilityReduceMotion`, and `\.legibilityWeight` into a hosted view.
- **Snapshot-style assertion for R2.3** — render each primary control at `.accessibility3` and
  `.accessibility5` and assert the label is not truncated.
- **Not eval-suite work** — deterministic UI, no model behaviour involved.
- **Manual device pass:** set Dynamic Type to `.accessibility3`, enable Reduce Motion, Bold Text,
  and Differentiate Without Color simultaneously, then walk all six tabs. Record in the PR.

---

## 6. Resolved Decisions

1. **Touch-target floor semantics (R2.2) — RESOLVED** (Brad, 2026-08-16): **content-sized frames
   with the existing size token as an absolute floor.** No Dynamic Type multiplier, no ceiling.
   Rationale and rejected alternatives are recorded inline under R2.2. Phase 2 is unblocked.

   **Interpretation note.** "80 pt floor" is read as endorsing the *mechanism* — floor plus
   content-sizing — with the existing two-token distinction preserved: 80 pt for primary actions
   (`touchTargetMin`), 64 pt for secondary (`touchTargetCompact`). Collapsing both to a flat 80 pt
   would make the five-button shortcut row in `TrackpadView` substantially taller for no stated
   benefit, so the distinction is kept. If a flat 80 pt everywhere was actually intended, that is a
   one-line change to R2.2 — say so and it is done.
