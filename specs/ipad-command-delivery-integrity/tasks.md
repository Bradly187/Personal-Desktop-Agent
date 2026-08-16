# Tasks: iPad Command Delivery Integrity

> **Gate 2 (AGENTS.md #11).** DRAFT. No task executes until Brad approves `requirements.md`
> (Gate 1) and this file (Gate 2). Do not self-promote `Status:`.

Criterion references are to `requirements.md` §4.

---

## Phase 1 — Classification

> Do this first and alone. Getting the taxonomy right is most of the work; the notice itself is
> mechanical. Landing it separately also means the exhaustiveness test (R5.3) exists before any
> behaviour depends on it.

- [ ] **1.1 Declare the message-type taxonomy** — `_deliberateCommandTypes` and `_settingsSyncTypes`
  beside the existing `_sensorFrameTypes`, with a default-to-deliberate fallback. Satisfies R2.2, R2.3.
- [ ] **1.2 Sub-type discrimination for `trackpad`** — `event == "tap"` is deliberate;
  `move`/`scroll` are continuous. The type string alone is insufficient. Satisfies R2.1.
- [ ] **1.3 Exhaustiveness test** — for every classified type plus a synthetic unknown, assert
  exactly one of {reject, drop, queue} applies. Satisfies R5.3. **This is the load-bearing test.**

## Phase 2 — The rejection notice

- [ ] **2.1 Rejection path in `send()`** — replace the bare `guard … else { return }` with a
  classified rejection. Satisfies R1.1, R1.5.
- [ ] **2.2 Encode and transmit failures** route to the same path. Satisfies R1.2, R1.3.
- [ ] **2.3 Action naming** — carry enough context to say "Close Window not sent" rather than
  "disconnected". Satisfies R1.4.
- [ ] **2.4 Coalescing** — one notice per `rejectionCoalesceWindow` carrying a count; first fires
  immediately; reset on reconnect. Satisfies R4.1–R4.3.
- [ ] **2.5 Tests** for 2.1–2.4.

## Phase 3 — Perceptibility

- [ ] **3.1 Error haptic** — `UINotificationFeedbackGenerator(.error)`, distinct from the existing
  `.impact` success haptics. Satisfies R3.1.
- [ ] **3.2 Visible notice** through `errorFeed` / `CommandToast`. Satisfies R3.2.
- [ ] **3.3 VoiceOver announcement** when VoiceOver is running. Satisfies R3.3, R3.4.
- [ ] **3.4 Tests** for 3.1–3.3.

## Phase 4 — Regression guard

- [ ] **4.1 Assert the three existing queue paths are untouched** — `DwellActionSyncer`,
  `FeatureToggleSyncer`, `_pendingGestureAssessment` still queue and flush on reconnect, and emit
  no rejection notices. Satisfies R5.1, R5.2.

## Phase 5 — Close-out

- [ ] **5.1 Manual device pass** — Wi-Fi off, tap three command-pad buttons, confirm one coalesced
  haptic + toast naming the first action with a count; repeat with VoiceOver on. Record in the PR.
  **Required before Shipped.**
- [ ] **5.2 Decision entry** — log loud-rejection-over-queue-and-replay in `docs/decisions.md`
  (AGENTS.md #12). It has a real rejected alternative and a non-obvious rationale (a delayed click
  is worse than no click), so it will otherwise be re-derived.
- [ ] **5.3 Unblock AT spec task B3** — repoint it at this rejection path.
- [ ] **5.4 Doc update** — mark audit G9 resolved and G16's failure-direction half closed; run
  `/doc-update`.

---

## Sequencing note

Phase 1 gates everything — the notice cannot be correct before the taxonomy is. Phases 2 and 3 are
sequential (3 decorates what 2 emits). Phase 4 is independent and could run at any point.

This spec is small and self-contained: iPad-side only, no protocol change, no PC change, no schema
change. It is the recommended first PR of the whole iPad programme — it closes the audit's
highest-severity finding, and `../ipad-assistive-tech-compat/` task B3 depends on it.
