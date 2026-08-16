# Tasks: iPad System Accessibility Settings Compliance

> **Gate 2 (AGENTS.md #11).** DRAFT. No task executes until Brad approves `requirements.md`
> (Gate 1) and this file (Gate 2). Do not self-promote `Status:`.

Criterion references are to this spec's `requirements.md` §3.

---

## Phase 1 — Token layer (do first; everything else depends on it)

- [ ] **1.1 Add Dynamic-Type-aware accessors to `DesignTokens`** — `@ScaledMetric`-backed icon size
  and touch-target helpers, so call sites migrate to a token rather than each solving it locally.
  Satisfies R2.1, R2.2 foundation.
- [ ] **1.2 Add environment-reading view modifiers** — small `ViewModifier`s wrapping Reduce Motion,
  Bold Text, and Differentiate-Without-Color reads, so 17 files do not each grow `@Environment`
  boilerplate. Satisfies R1.4, R4.2.
- [ ] **1.3 Tests for the token layer** — assert scaling behaviour at default, `.accessibility3`,
  `.accessibility5`.

## Phase 2 — Dynamic Type migration (the bulk)

> **Unblocked** (Brad, 2026-08-16): content-sized frames, existing token as floor, no ceiling.

- [ ] **2.1 Migrate icon glyph sizes** — 71 `.font(.system(size:))` call sites across 17 files to
  the Phase 1 token. R2.1, R2.5.
- [ ] **2.2 Convert touch-target frames from fixed to floored-content sizing** — replace
  `frame(minHeight: DesignTokens.Size.touchTargetMin)`-style constants with a floor that lets the
  frame grow to content. R2.2.
- [ ] **2.3 Relax `CommandPadView` column cap** at large sizes — required for 2.2 to have room to
  grow; without it the grid re-clips what content-sizing just fixed. R2.4.
- [ ] **2.4 Remove `minimumScaleFactor`** from all control labels; reflow instead. R3.1, R3.2.
- [ ] **2.5 Tests** — per-criterion, plus no-truncation assertions at `.accessibility3`/`.accessibility5`.
  R2.3.
- [ ] **2.6 Default-size regression guard** — assert the rendered layout at the default
  `dynamicTypeSize` is unchanged. R2.6. **Land this test before 2.1–2.4**, so the whole 17-file
  migration is provably a no-op until the text size is actually raised.

## Phase 3 — Motion, weight, colour

> Independent of Phase 2; can run in parallel.

- [ ] **3.1 Reduce Motion** — `DwellActionToolbar` pulse, dwell ring, tab transition, page slide.
  R1.1–R1.3.
- [ ] **3.2 Bold Text** — label weight bump under `legibilityWeight == .bold`. R4.1.
- [ ] **3.3 Differentiate Without Color** — audit every colour-only cue; `SensorActivityBar` is the
  known gap (connection banner and dwell toolbar already carry non-colour cues). R5.1–R5.4.
- [ ] **3.4 Tests** for 3.1–3.3.

## Phase 4 — Close-out

- [ ] **4.1 Manual device pass** — Dynamic Type `.accessibility3` + Reduce Motion + Bold Text +
  Differentiate Without Color all on, walk all six tabs. Record in the PR. **Required before Shipped.**
- [ ] **4.2 Doc update** — refresh audit G14/G15 status; run `/doc-update`.

---

## Sequencing note

Phase 1 gates Phase 2 and Phase 3 — both consume the token layer. Phase 3 is the cheaper half and
delivers visible benefit sooner, so if only one phase ships first, make it Phase 3.
