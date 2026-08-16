# Spec: iPad Command Delivery Integrity

## 1. Background — the "Why"

`WebSocketManager.send()` opens with `guard let task, state == .connected else { return }`. A tap on
a command-pad button while the socket is down is a **silent no-op** — nothing sent, nothing said,
nothing logged to the user. The connection banner shows that the app is disconnected, but nothing
tells Brad that *this specific thing he just did did not happen*.

The audit
(`docs/audits/2026-08-13-ipad-swift-accessibility-gap-analysis.md`, G9) rates this the single most
consequential defect in the app. The reasoning: for a user whose fallback is physically reaching the
PC, a command that silently does nothing is the worst available failure mode — worse than an error,
because an error can be acted on.

Three message types already have send-or-queue handling (`set_dwell_action` via `DwellActionSyncer`,
`set_feature_toggle` via `FeatureToggleSyncer`, `gesture_assessment` via
`_pendingGestureAssessment`). The gap is that these are all *settings sync*. The deliberate,
user-initiated actions — `touch_command`, `trackpad` taps, `handwriting_image` — have none.

**Status:** Draft → [Brad approves spec] → In Progress → [Brad approves tasks.md] → Building → Shipped (PR #___)
**Approved:** <!-- set to "Brad, YYYY-MM-DD" when approving this spec; do NOT self-promote -->
**Owner / author session:** Claude Code

---

## 2. Decision: loud rejection, not queue-and-replay

Brad chose **loud rejection** (2026-08-16) over a bounded queue with replay-on-reconnect.

The rejected alternative was a short-TTL queue: hold deliberate commands for a few seconds and flush
them if the socket recovers. It loses less work on a brief Wi-Fi blip. It was rejected because a
replayed command executes against a desktop that has moved on — different window focused, different
cursor position — and `WebSocketManager` reconnects with exponential backoff capped at 5 s, so a
replay can land well after intent. **A delayed click is worse than no click.** Loud rejection also
carries no queue state, no TTL to tune, no replay ordering to reason about, and no residual window
in which a stale command can still fire. It matches the fail-safe-to-DENY posture in AGENTS.md #4.

The cost is real and accepted: Brad redoes the action once reconnected.

---

## 3. Glossary

- **Deliberate command**: a discrete, user-initiated message with a side effect on the desktop —
  `touch_command`, `trackpad` `tap`, `handwriting_image`, `dwell_click`, `mic_mute`. Each one
  corresponds to an action Brad consciously took.
- **Continuous stream**: a sampled sensor or motion message where only the newest value matters and
  a stale one is meaningless — `tilt`, `tilt_position`, `trackpad` `move`/`scroll`, `audio_stream`.
- **Settings sync**: idempotent, last-write-wins state mirroring that already queues —
  `set_dwell_action`, `set_feature_toggle`, `gesture_assessment`.
- **Rejection notice**: the multi-channel signal that a deliberate command was not delivered.

---

## 4. Requirements (EARS acceptance criteria)

### Requirement 1: Deliberate commands never fail silently

**User Story:** As Brad, I want to be told when a command did not reach the PC, so that I am not
left believing the desktop did something it did not do.

#### Acceptance Criteria

1. WHEN a deliberate command is submitted WHILE the WebSocket is not connected, THE
   `WebSocketManager` SHALL emit a rejection notice and SHALL NOT silently discard the message.
2. IF a deliberate command fails to JSON-encode, THEN THE `WebSocketManager` SHALL emit a rejection
   notice, treating non-encodable the same as non-deliverable.
3. IF the underlying `URLSessionWebSocketTask.send` completes with an error, THEN THE
   `WebSocketManager` SHALL emit a rejection notice for that message.
4. THE rejection notice SHALL name the action that failed (e.g. "Close Window not sent"), not merely
   report that the app is disconnected — the connection banner already conveys connection state.
5. THE `WebSocketManager` SHALL NOT attempt to deliver the rejected command later.

### Requirement 2: Continuous streams stay silent

**User Story:** As Brad, I want a dropped tilt frame to stay silent, so that the disconnect warning
means something when it fires.

#### Acceptance Criteria

1. WHEN a continuous stream message is dropped for any reason (disconnected, or sensor backpressure
   per the existing `_sensorFrameTypes` path), THE `WebSocketManager` SHALL NOT emit a rejection
   notice.
2. THE classification of each message type as deliberate, continuous, or settings-sync SHALL be a
   single explicit declaration in `WebSocketManager`, mirroring the existing `_sensorFrameTypes`
   pattern, so a newly added message type must be classified rather than defaulting silently.
3. IF a new message type is added without classification, THEN it SHALL be treated as deliberate —
   an unnecessary warning is a better failure than a silent drop.

### Requirement 3: The rejection is perceivable without looking at the iPad

**User Story:** As Brad, I want to notice a failed command while I am watching the PC screen, so
that the warning reaches me where my attention actually is.

#### Acceptance Criteria

1. THE rejection notice SHALL fire an error haptic (`UINotificationFeedbackGenerator`, `.error`),
   distinct from the `.impact` haptics used to confirm successful actions.
2. THE rejection notice SHALL surface a visible message through the existing `errorFeed` /
   `CommandToast` path.
3. WHEN VoiceOver is running, THE rejection notice SHALL additionally post
   `UIAccessibility.post(notification: .announcement)` so the failure is spoken.
4. THE rejection notice SHALL NOT require dismissal or block further input.

> This closes the audit's G16 finding (no non-visual confirmation of PC-side outcome) for the
> failure direction. The success direction remains out of scope here.

### Requirement 4: Repeated rejections coalesce

**User Story:** As Brad, I want one clear warning rather than a burst, so that a disconnected
session is not a haptic drum solo.

#### Acceptance Criteria

1. WHEN multiple deliberate commands are rejected within `rejectionCoalesceWindow`, THE
   `WebSocketManager` SHALL emit one rejection notice reporting the count (e.g. "3 commands not
   sent") rather than one notice per command.
2. THE first rejection in a window SHALL fire immediately, not be delayed to accumulate a count.
3. WHEN the connection is restored, THE coalescing state SHALL reset.

### Requirement 5: Existing queue paths are preserved

**User Story:** As Brad, I want my settings to keep syncing across a reconnect, so that this change
does not regress behaviour that already works.

#### Acceptance Criteria

1. THE existing send-or-queue behaviour of `DwellActionSyncer`, `FeatureToggleSyncer`, and
   `_pendingGestureAssessment` SHALL be unchanged.
2. THE settings-sync message types SHALL NOT emit rejection notices, because they are not lost —
   they are queued and flushed on reconnect.
3. FOR ALL message types, exactly one of {rejection notice, silent drop, queue-and-flush} SHALL
   apply — the three behaviours SHALL be mutually exclusive and collectively exhaustive.

---

## 5. Technical Design

- **Entry point:** `WebSocketManager.send()` — the single existing choke point. No new call sites.
- **Classification:** a `_deliberateCommandTypes` / `_settingsSyncTypes` pair of `static let Set<String>`
  alongside the existing `_sensorFrameTypes`, plus a default-to-deliberate fallback per R2.3.
  `trackpad` needs sub-type discrimination: `event == "tap"` is deliberate, `move`/`scroll` are
  continuous.
- **No protocol change.** Nothing new crosses the wire; this is entirely iPad-side. No AGENTS.md #3
  obligation, no PC changes, no `agent.db` change.
- **Reuses:** `errorFeed` (`PassthroughSubject<String, Never>`) and `CommandToast` already exist and
  already render error strings.

### Explicitly out of scope

- **Ack timeouts.** A command that sends successfully but is never acked by the PC is a third
  failure class, not addressed here. Named so it is not mistaken for covered.
- **Success confirmation.** Non-visual confirmation of *successful* delivery (audit G16's other half)
  is a separate change.
- **Retry or queueing of deliberate commands.** Explicitly rejected — see §2.

### Configuration (flat YAML)

```yaml
ipad_command_delivery:
  rejection_coalesce_window_s: 3.0   # R4.1 — one notice per window, with a count
  announce_via_voiceover: true       # R3.3
```

---

## 6. Behavior Verification

- **Swift unit tests** (`iPadApp/Tests/`, existing simulator CI job), one per numbered criterion:
  - R1.1–R1.3: submit each failure mode against a disconnected manager, assert `errorFeed` emits.
  - R1.5: assert no retry occurs after reconnect.
  - R2.1: assert dropped `tilt` / `trackpad move` produce no emission.
  - R2.3: assert an unclassified type is treated as deliberate.
  - R4.1–R4.3: assert N rejections inside the window produce one notice carrying count N.
  - R5.3: **the exhaustiveness property** — for every type in the union of the three classification
    sets plus a synthetic unknown, assert exactly one behaviour applies. This is the test that keeps
    the classification honest as message types are added.
- **Manual device pass:** disconnect Wi-Fi, tap three command-pad buttons, confirm one coalesced
  haptic + toast naming the first action and a count; repeat with VoiceOver on. Record in the PR.

---

## 7. Relationship to the AT spec

`../ipad-assistive-tech-compat/` R1.4 requires AT-initiated cursor actions to surface non-delivery
"per the G9 disconnect contract". That contract is this spec. Task **B3** there consumes the
rejection path defined here rather than building its own — which is why this spec ships first.
