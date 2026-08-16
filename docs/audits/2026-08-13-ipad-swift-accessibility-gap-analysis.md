# iPad Swift App — State Evaluation & Accessibility Gap Analysis

**Date:** 2026-08-16
**Scope:** `iPadApp/` (45 production Swift files, 10,583 LOC; 20 XCTest files)
**Baseline specs:** `specs/ipad-sensor-focus/requirements.md` (R1–R19), `specs/ipad-app-hardening/`,
`specs/tilt-position-mapping/`, `specs/sensor-refinement/`, `specs/ipad-ui-touch-debug/`
**Method:** static read of all Swift sources + protocol diff against `core/ipad_bridge.py`
+ requirement-by-requirement trace. No device run.

---

## 1. Current State

### What exists and works

| Area | State |
|---|---|
| **Shell** | 6 tabs — Commands, Trackpad, Write, Agent, Settings, Sensors. First 3 page-swipeable; custom tab bar with drag-to-switch. |
| **Transport** | `WebSocketManager` (800 LOC): exponential backoff (5 s cap), 10 s connect timeout, serial send queue with per-type backpressure, binary audio framing, pairing-token auth, mDNS discovery + manual IP. |
| **Sensors** | 3 live: `TiltSensor` (60 Hz, One-Euro filter, position/velocity/joystick modes, ratchet, dwell-to-click), `KeywordListener` (Speech framework), `AudioStreamer` (PCM → Whisper). |
| **Touch surfaces** | `CommandPadView` (editable grid, 80 pt targets), `TrackpadView` (1-finger move / 2-finger scroll / 1- and 2-finger tap, palm rejection via `majorRadius`), full-screen trackpad mode. |
| **Pencil** | `HandwritingCanvasView` — `PKCanvasView` `.pencilOnly`, PNG → `handwriting_image` → pix2tex → editable unicode → DICTATE. Fully meets R19. |
| **Calibration UX** | Voice profiling, quick recal, gesture assessment, flare profile, tilt calibration, 10-step onboarding. |
| **Agent surface** | A2UI renderer + canvas store — PC-pushed approval prompts and dashboards. |
| **Design system** | `DesignTokens` (80 pt primary / 64 pt compact targets), `AppTheme` on system semantic colors (inherits dark mode + Increase Contrast). |
| **CI** | `build-ipad-app.yml` on macos-26 / Xcode 26.4.1: XcodeGen → archive → IPA → TestFlight, **plus** a parallel simulator test job. Genuinely good. |

### Protocol reality vs. documentation

CLAUDE.md documents 26 iPad→PC message types. The Swift app **sends 24**. The two it never
sends — `camera_frame` and `depth_frame` — have live handlers in `core/ipad_bridge.py`.
PC→iPad: the bridge sends 13 types, Swift decodes 16 (the extras arrive from other PC modules);
**`gesture_assessment` is sent PC→iPad but has no Swift decode case.**

**Net: the entire vision half of the sensor design has a PC-side receiver and no iPad-side
producer.** `Info.plist` has no `NSCameraUsageDescription` — the app cannot open a camera at all.

---

## 2. Design Goals vs. Reality

| Req | Goal | Verdict |
|---|---|---|
| R1 | Native SwiftUI, WS, 44 pt+ targets, Dynamic Type, reconnect | **Met**, except Dynamic Type is nominal (see G7) |
| R2 | Tilt-to-navigate + table-tap click | **Exceeded** — position mode, joystick mode, ratchet, dwell all shipped beyond spec |
| R3/R4 | Gaze / head tracking | **Removed by design** (no TrueDepth) — correctly struck through in the spec |
| R5 | On-device keyword listener, 9 default keywords, confidence threshold, reorder | **Partial** — see G4 |
| R6 | Sound actions (cluck/pop/hiss) | **Not implemented** — see G1 |
| R7 | LiDAR depth streaming | **Not implemented** on iPad — see G2 |
| R8 | Command pad, trackpad, **dwell activation on buttons**, palm rejection, settings | **Partial** — 8.4 missing, see G3 |
| R9 | Full-screen trackpad | **Mostly met**; 9.6 tap-to-click toggle and 9.7 gesture-exit missing, see G6 |
| R10 | Camera gesture recognition | **Not implemented** on iPad — see G2 |
| R11 | Audio → WhisperStream | Path exists; **off by default** — see G5 |
| R12–R14 | Fusion / routing / learning | PC-side; spec text is stale (see §4) |
| R15 | Discovery + QR pairing | **Partial** — Bonjour + manual IP work; **no QR scanner**, see G8 |
| R16 | Graceful degradation | **Partial** — hardware checks exist; connection loss is not handled, see G9 |
| R17 | Constrained action vocabulary | Not enforced client-side — see G12 |
| R18 | Scientific keypad | **Not implemented** — but marked `[x]` in `tasks.md`, see G10 |
| R19 | Pencil handwriting | **Fully met** |

---

## 3. Gap Analysis

Ordered by impact on the app's job: being a dependable control plane on the days the
user's hands work least well.

### Tier 1 — Reliability of the control plane itself

#### G9. Connection loss silently discards every command
`WebSocketManager.send()` opens with `guard let task, state == .connected else { return }`.
A tap on a command-pad button while the socket is down is a **silent no-op**. There is a
send-or-queue path for `set_dwell_action` (`DwellActionSyncer`), `set_feature_toggle`
(`FeatureToggleSyncer`) and `gesture_assessment` (`_pendingGestureAssessment`) — but **not
for `touch_command`, `trackpad`, or `handwriting_image`**, i.e. exactly the deliberate,
user-initiated actions.

The connection banner does show state, but nothing tells the user *this specific command
did not land*. For a user whose alternative is physically reaching the PC, a silent drop is
the worst possible failure mode.

**Resolution (2026-08-16): loud rejection, specced at `specs/ipad-command-delivery-integrity/`.**
Brad chose loud rejection over the bounded-queue option this section originally floated. The
queue was rejected because a replayed command executes against a desktop that has moved on, and
`WebSocketManager` reconnects with backoff capped at 5 s — **a delayed click is worse than no
click**. Loud rejection also carries no queue state, no TTL, and no window in which a stale
command can still fire. Ships first of the iPad programme; the AT spec's task B3 consumes its
rejection path.

#### G1. Sound actions (R6) — ~~the zero-hand modality — do not exist~~ **RESOLVED: struck (D032)**

> **Correction to this finding (2026-08-16).** The original text claimed *"it is unclear whether
> this was dropped deliberately or simply never built"* and rated it the last open build-or-strike
> call. **Both claims were wrong, and the evidence was in a file this audit had already opened.**
> `core/fusion_engine.py:17` states in its module docstring: *"Mouth-sound control (cluck/pop/hiss)
> was removed — the sounds fired incidentally."* This audit read that file to check
> `on_tilt_position` and did not read the header above it.

R6 was **built, shipped, used, and withdrawn on evidence** — removed end-to-end 2026-06-04 by
`54a4f00` (iPad: `SoundDetector.swift`, `SoundTrainingSheet.swift`, `soundMappings`, sensor card,
onboarding step, Settings editor) and `7b0d7ee` (PC: `on_sound_action`, the `_sound` tick slot,
priority/cooldown, bridge handler, bypass source). The reason given: *"the sounds fired
incidentally and were not wanted."*

**Its replacement shipped in the same commits** — magnetic click (tilt-tap snaps to the nearest
clickable within `DA_SNAP_RADIUS_PX`) plus a tap threshold lowered 1.2 g → 0.6 g. The low-effort
trigger moved from *make a sound* to *tap the table lightly*, which is far harder to fire by
accident.

**Successor for the user story:** iPadOS Switch Control **Sound Actions**, reached via
`specs/ipad-assistive-tech-compat/`. See D032. This raises that spec's priority — with Eye Tracking
unavailable on the device, it is now the only route back to a genuinely zero-hand modality.

`flareSoundDegrades` / `flare_sound_degrades` remain **deliberately dormant** to avoid a
behavioural-twin schema change; they are not vestigial oversights and should not be deleted.

#### G3. Dwell activation on iPad buttons (R8.4) is missing
Dwell exists — but only for the **PC cursor** (`TiltSensor.updateDwell` → `dwell_click`).
R8.4 specifies a different thing: *resting a finger on an on-screen iPad button activates it
without a tap press, with a ring countdown*. `CommandPadView` uses plain `Button`s;
`DwellProgressRing` is wired only to `TiltSensor.dwellInProgress`.

This is the specced fallback for the day the user cannot reliably execute a press-and-release.
`specs/accessibility-agent/tasks.md` task 2.12 (integration test for exactly this) is still
open — consistent with it never having been built.

#### G7. Pain-day adaptation is one-way
The iPad *reports* flare state to the PC (`pain_day_override`, `flare_profile`) and the PC
adapts. **The iPad never adapts its own thresholds.** `TiltSensor.swift` contains zero
references to pain-day or flare state. On a flare day, `tiltDeadZone`, `tiltRange`,
`tiltDwellDuration`, `tapThreshold`, and `palmRejectRadius` stay exactly where they were —
so the user must hand-tune sliders on the day their hands work worst.

AGENTS.md #5 ("never hardcode interaction thresholds; wire through `BehavioralTwinState.apply_pain_day()`")
is honoured on the PC and unenforced on the client.

**Fix:** a `flareThresholdMultiplier` applied to the snapshotted tilt/tap/palm constants when
`manualPainDay` or a PC-pushed pain-day signal is active. Cheap — the snapshot mechanism
already exists in `TiltSensor.updateSettings()`.

### Tier 2 — Standard iOS accessibility affordances

None of the following appear anywhere in the codebase. For an app whose entire purpose is
accessibility, these are conspicuous.

#### G13. No VoiceOver or Switch Control awareness
Zero hits for `UIAccessibility.isVoiceOverRunning`, `isSwitchControlRunning`,
`accessibilityCustomAction`, or `accessibilityRotor`.

Concretely:
- With VoiceOver on, tilt cursor and dwell-click keep firing while VoiceOver owns the gesture
  layer — no arbitration.
- `TrackpadGestureView` calls `scrollView.panGestureRecognizer.require(toFail: pan)` on every
  ancestor scroll view. Under VoiceOver and Switch Control this custom UIKit gesture surface
  is opaque — there is no accessible equivalent for "move the cursor".
- The custom tab bar's `simultaneousGesture(DragGesture(minimumDistance: 40))` has no
  Switch Control path.

Switch Control (head switch, sip-and-puff) is the standard escalation when an RA flare takes
hands out of play entirely.

> **Correction (2026-08-16): "the app has no story for it" was overstated.** Every *button*
> surface is a real SwiftUI `Button` carrying an `accessibilityLabel` — `CommandPadView`, the
> custom tab bar, `MicMuteIndicator`, `DwellActionToolbar`, `TrackpadView`'s click/shortcut/scroll
> rows, the handwriting controls, and A2UI approval prompts via `DAButton`. Switch Control can
> scan and activate all of them **today**, and Voice Control can address them by label, with no
> code change.
>
> What is genuinely unreachable is **cursor positioning** — the three bullets above are all about
> the gesture surface, and they stand. The accurate framing is **you can already click, you cannot
> aim**; aiming is the app's core function, so this remains the decisive gap, but it is narrower
> than the original sentence implied. It is also measurable on-device for free: enable Switch
> Control, try the command pad, then try to move the cursor.

#### G14. No Reduce Motion support
Zero references to `accessibilityReduceMotion`. The app ships a `repeatForever` pulsing
border on the drag button, dwell ring animations, 0.15 s tab cross-fades, and page-swipe
transitions. Reduce Motion is a one-line environment read per animation site.

#### G15. Dynamic Type is declared, not delivered
`DesignTokens.Typography` correctly uses relative styles (`.system(.body)`, `.system(.caption)`) —
good. But:
- Every icon is `.font(.system(size: DesignTokens.Size.iconSize))` — **fixed 24 pt, never scales.**
- Touch targets are fixed 80/64 pt frames that do not grow with text size.
- `DwellActionToolbar` uses `.lineLimit(1).minimumScaleFactor(0.8)` — this *actively shrinks
  text away from* the size the user asked for.
- `CommandPadView`'s grid caps items at 160 pt with `lineLimit(2)` — 2-line labels will clip
  at AX3+.

R1.3 claims Dynamic Type support. At accessibility text sizes the app will not hold up.

#### G16. No non-visual confirmation of PC-side outcome
Haptics fire on *local* events (tab switch, dwell fire). The PC's `ack`/error comes back as a
visual `CommandToast` only. A user watching the desktop rather than the iPad gets no signal
that a command landed or failed. There is no `AVSpeechSynthesizer` and no
`UIAccessibility.post(notification: .announcement)` anywhere — all TTS lives on the PC.

**Fix:** distinct success/failure haptics on `ack`, plus an optional VoiceOver announcement.

### Tier 3 — Coverage and setup

#### G5. Cold-start defaults leave the app nearly inert
```
keywordList        = []      // KeywordListener never starts (SensorManager gates on non-empty)
audioStreamEnabled = false   // no audio reaches WhisperStream
```
Fresh install = **tilt + touch only**. Voice — the headline modality of the whole system — is
off. Onboarding can enable it, but offers only `["click", "scroll", "open"]` against R5.1's
specified nine (`Select, Click, Scroll Up, Scroll Down, Open, Close, Back, Undo, Dictate`).

The `keywordList = []` default carries a justifying comment ("WhisperStream handles all voice
input more naturally — keywords are opt-in"), which is a reasonable position — but it is
paired with `audioStreamEnabled = false`, so *neither* voice path is on. That combination
looks unintended.

#### G4. Keyword matching is substring-based and threshold-free
`KeywordListener` line 209: `if newContent.contains(kw)`. "open" fires inside "reopen",
"happened", "opening". Confidence is hardcoded `0.9` at the send site regardless of what
Speech actually reported — so R5.2's *"confidence above the configured threshold"* is not
implemented. R5.4's reorder is also missing (Settings offers add + delete only).

For a control plane, false-positive `OPEN`/`CLOSE` verbs are not benign.

**Fix:** word-boundary matching, and pass through `SFTranscriptionSegment.confidence` against
a configurable floor. Compare `core/approval_keywords.classify_confirmation` on the PC side,
which already gets this right for the approval gate.

#### G2. Camera and LiDAR (R7, R10) are absent on the iPad — **RESOLVED 2026-08-16: struck (D030)**
No capture code, no `NSCameraUsageDescription`.

> **Correction to this finding.** The first draft stated *"the PC half of gesture recognition is
> complete and starved of input."* That is wrong. `sensors/realsense_publisher.py` connects to
> the same bridge as a WebSocket client and emits the **same** `camera_frame` and `depth_frame`
> message types from the RealSense L515. `main.py:1036-1037` wires `LiDARReceiver` and
> `GestureProcessor` unconditionally, and `tests/test_gesture_*.py` / `test_lidar_receiver.py`
> cover them. The receivers are **live on the L515 path** — only the *iPad* producer was
> missing. Deleting them as cleanup would have broken hand-pointer control and D7 flick-to-snap.

**Resolution:** R7 and R10 struck; PC-side receivers retained and their documentation retargeted
to name RealSense as the producer. See D030.

**Knock-on:** R15.3 QR pairing (G8) is *not* resolved by this — a one-shot
`AVCaptureMetadataOutput` scan is a different capability from a sensor stream. Left open.

#### G6. Full-screen trackpad is a soft trap
R9.7 requires *"a gesture or edge swipe to switch between full-screen trackpad mode and the
command pad view **without requiring precise taps**."* The only exit is a circular button
(24 pt icon + 12 pt padding ≈ 48 pt) at top-left — under half the 80 pt target the rest of
the app enforces, and it is precisely the precise tap the requirement rules out. Tab bar and
nav bar are both hidden in this mode.

Also missing: R9.6's tap-to-click enable/disable (no `tapToClick` setting exists), so a user
who rests fingers on the surface cannot turn off accidental clicks.

#### G8. No QR pairing scanner
R15.3 has the PC print a QR code *"that the user can scan with the iPad camera."* The app has
no scanner (and no camera permission). The remaining setup path is typing an IP address and a
pairing token by hand — on a device whose users have arthritis. This is the highest-friction
moment in the entire product and it is unmitigated.

#### G12. Action strings are unvalidated free text
`CommandPadEditorView` exposes a raw `TextField` for the action verb. Nothing checks it
against the 16-verb vocabulary. A typo produces a button that silently does nothing until the
PC rejects it. A picker over the known verbs is a small change with a real reliability payoff.

---

## 4. Documentation Integrity

**All six fixed 2026-08-16** — struck through below, with what was done.

1. ~~`tasks.md` 2.14 marked `[x]` for a `ScientificKeypadView` that does not exist.~~
   **Fixed:** re-marked `[~]` STRUCK with a pointer to D031; the stale "Keypad" sibling-tab
   reference in 2.15 removed.
2. ~~`Tests/OverlayPreservationTests.swift:297` `testScientificKeypadSendExpression` tests
   `sendCommand(action: "DICTATE")` and has nothing to do with a keypad.~~
   **Fixed:** renamed `testWriteTabDictateSendExpression`, with a comment recording why.
3. ~~R12's fusion priority list is stale (10 levels incl. gaze/head).~~ **Fixed:** see below.
4. ~~R17 lists 8 action verbs; the live vocabulary is 16.~~ **Fixed:** see below.
5. ~~R6, R7, R10 have no removal marker despite being unimplemented.~~
   **Partially fixed:** R7 and R10 struck (D030); R18 struck (D031). **R6 (sound actions)
   deliberately left live** — it is unbuilt but not yet decided, and marking it would imply a
   call that has not been made.
6. ~~`gesture_assessment` (PC→iPad) has no Swift decode case.~~ **Documented**, not yet fixed —
   now listed explicitly in the PC→iPad table in `docs/websocket-protocol.md` (13 types, was
   miscounted as 12) so it stops being invisible. The decode case is still a real one-line gap.

Also corrected in the same pass: `docs/websocket-protocol.md` claimed `camera_frame`/`depth_frame`
were *"sent by `LiDARStreamer.swift` (enabled via `lidarEnabled` toggle)"*. Those symbols were
real but are **12 weeks stale** — `LiDARStreamer.swift` (370 lines) shipped 2026-05-16 and was
stripped 2026-05-24 in `64eec10`. Retargeted to name `sensors/realsense_publisher.py`, with the
deletion recorded. The iPad→PC count in that file and CLAUDE.md corrected 26 → 24.

> **Correction to §2 of this report.** The requirements table originally listed R7 and R10 as
> *"Not implemented"*. More precisely: they **were** implemented and then deliberately removed.
> `LiDARStreamer.swift` emitted both `depth_frame` (5 fps) and `camera_frame` (10 fps) before
> `64eec10` deleted it — because the device has no LiDAR scanner and
> `ARWorldTrackingConfiguration.supportsFrameSemantics` crashed the Settings tab on iOS 26 on
> non-LiDAR hardware. This is the same root cause as the R3/R4 gaze/head removal: **the spec was
> written against iPad Pro hardware the project does not own.** That makes the strike a
> ratification of an existing engineering decision, not a new scope cut.

---

## 5. Recommended Sequence

**Phase 1 — make the control plane trustworthy (highest value / lowest cost)**
1. G9 — queue-or-loudly-reject `touch_command` on disconnect
2. G16 — success/failure haptic on `ack`
3. G4 — word-boundary keyword match + real confidence threshold
4. G5 — ship the 9 specced default keywords; reconcile the two voice-path defaults
5. G6 — 80 pt exit target + edge-swipe exit for full-screen trackpad

**Phase 2 — close the standard-AX gaps**
6. G14 — Reduce Motion
7. G15 — scale icons and targets with Dynamic Type; drop `minimumScaleFactor`
8. G13 — VoiceOver arbitration for tilt/dwell; `accessibilityCustomAction`s on the trackpad
   and tab bar for Switch Control
9. G7 — pain-day threshold multiplier on the iPad side

**Phase 3 — decide the deferred modalities (needs a human call, not a patch)**
10. ~~G2 — camera / LiDAR~~ — **DONE 2026-08-16: struck (D030).** PC receivers retained.
11. ~~R18 — scientific keypad~~ — **DONE 2026-08-16: struck (D031).** Pencil canvas covers it.
12. G1 — sound actions: **still open.** The zero-hand modality remains the one unresolved
    strike-or-build call, and the highest-value one on a severe flare day.
13. G8 — QR pairing scanner: **still open**, and explicitly *not* settled by D030.

**Phase 0 — free, do now:** ~~the six documentation-integrity fixes in §4~~ — **DONE 2026-08-16.**

---

## 6. Summary Judgement

The iPad app is **well-engineered on the axes it has actually been built along** — the tilt
pipeline is more sophisticated than its own spec asks for, the transport layer handles
backpressure and auth properly, the design system enforces 80 pt targets, and CI both builds
and tests on a real simulator. The hardening and touch-debug specs are fully executed.

The gaps cluster in two places:

1. **Breadth was traded for depth.** Three of the seven designed input modalities (sound,
   camera gesture, LiDAR) do not exist on the client, and a fourth (voice) is off by default.
   The app is effectively a **tilt-and-touch** controller. That may be the right call — but it
   was never recorded as a call, so the specs and the PC-side code still act as if the other
   modalities are coming.

2. **Standard iOS accessibility APIs are largely unused.** VoiceOver arbitration, Switch
   Control, Reduce Motion, and genuine Dynamic Type are all absent. The app's accessibility
   story is *bespoke* (large targets, dwell, palm rejection, flare profiles) and strong on
   that axis — but it does not compose with the assistive technology the user's own iPad
   already ships, which is what they will reach for when the bespoke path is not enough.

The single most consequential defect is **G9**: a command that silently does nothing when the
socket is down, in an app that is the user's means of reaching the machine.
