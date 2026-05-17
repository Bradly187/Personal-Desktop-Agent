# Daily Review — 2026-05-17

Automated housekeeping run. No user present.

---

## Yesterday's Work (2026-05-16) — Summary

Ten commits across five themes. All work is on the `feat/tilt-position-mapping` branch.

### Theme 1 — Tilt position-mapped cursor control (commit `a19f135`)

The central feature landed: replaced velocity-based tilt (streaming `rx/ry` deltas) with absolute position mapping. The iPad now computes normalized `(x, y)` coordinates from its gravity vector relative to a calibrated neutral orientation and sends a `tilt_position` WebSocket message. The PC-side `FusionEngine` applies EMA smoothing and calls `pyautogui.moveTo`.

**Files changed:**
| File | What changed |
|------|-------------|
| `iPadApp/DesktopAgent/Sensors/TiltSensor.swift` | New `tiltPositionMode` branch in `handle(_:)`: `computePosition()` pure function, stationary lock (200ms at ≤0.01 rad/s), message suppression (delta < 0.001), `calibrate()` with 500ms debounce + haptic |
| `iPadApp/DesktopAgent/SettingsStore.swift` | `tiltRange` (default 25°, clamped [5,60]), `tiltPositionMode` (default true), neutral gravity persistence (`neutralGravityX/Y/Z`, `hasPersistedNeutral`) |
| `iPadApp/DesktopAgent/Network/WebSocketManager.swift` | `sendTiltPosition(x:y:)` sends `{"type":"tilt_position","x":...,"y":...}` |
| `iPadApp/DesktopAgent/UI/SettingsView.swift` | Tilt range slider + position mode toggle + "Calibrate Neutral" button |
| `ipad_bridge.py` | `tilt_position` message handler → `FusionEngine.on_tilt_position(x, y)` |
| `fusion_engine.py` | `_tilt_position` state, `on_tilt_position()`, Rule 6a absolute positioning with EMA + gaze suppression |
| `.kiro/specs/tilt-position-mapping/` | Full spec directory: design.md (algorithm, mermaid diagrams, 12 correctness properties), requirements.md, tasks.md (all 9 tasks checked off) |

**New property-based tests added:**

| File | Property tested | Coverage |
|------|----------------|---------|
| `iPadApp/Tests/TiltRangeClampingTests.swift` | Property 5: tiltRange always clamped [5, 60] | Req 3.3 |
| `iPadApp/Tests/CalibrationPersistenceTests.swift` | Property 3: neutral gravity persists round-trip | Req 2.1–2.3 |
| `iPadApp/Tests/LinearMappingPropertyTests.swift` | Property 1: linear mapping produces correct normalized output | Req 1.3–1.4 |
| `iPadApp/Tests/DeadZoneCenterTests.swift` | Property 7: dead zone maps to (0.5, 0.5) | Req 6.1 |
| `iPadApp/Tests/InversionSymmetryTests.swift` | Property 11: (x,y) and inverted = (1-x, 1-y) | Req 7.1 |
| `iPadApp/Tests/RecalibrationRecentersTests.swift` | Property 4: recalibration always outputs (0.5, 0.5) | Req 2.5 |
| `iPadApp/Tests/TapIndependenceTests.swift` | Property 12: userAcceleration doesn't affect computePosition | Req 8.3 |
| `iPadApp/Tests/MessageSuppressionTests.swift` | Property 8: suppression when delta < 0.001 | Req 4.3 |
| `iPadApp/Tests/StationaryLockTests.swift` | Property 10: stationary lock freezes output after 200ms | Req 6.3 |
| `tests/test_prop_ema_smoothing.py` | Property 9: EMA recurrence + variance reduction | Req 5.2, 6.2 |
| `tests/test_prop_tilt_range_effect.py` | Property 6: tilt range immediately changes mapping | Req 3.4 |

### Theme 2 — SharedFaceSession (uncommitted working changes)

Resolved a silent bug: `GazeTracker` and `HeadTracker` each previously created their own `ARSession`. ARKit only supports one face-tracking session per device — whichever started second stole the camera from the first. The fix introduces `SharedFaceSession`, which owns a single `ARFaceTrackingConfiguration` ARSession and fans out `ARFaceAnchor` updates to all registered consumers via a reference-counted `addConsumer`/`removeConsumer` API.

**Files changed (uncommitted):**
| File | Change |
|------|--------|
| `iPadApp/DesktopAgent/Sensors/SharedFaceSession.swift` | New: owns ARSession, consumer registry, session lifecycle |
| `iPadApp/DesktopAgent/Sensors/GazeTracker.swift` | Migrated from `private let session = ARSession()` to `sharedFaceSession.addConsumer/removeConsumer` |
| `iPadApp/DesktopAgent/Sensors/HeadTracker.swift` | Same migration; ARSessionDelegate extension removed |
| `iPadApp/DesktopAgent/SensorManager.swift` | Creates `SharedFaceSession`, injects into GazeTracker and HeadTracker; docstring updated to "7 sensors" |
| `.kiro/steering/structure.md` | Added rule 6 to sensor integration guide; `SharedFaceSession` noted in Sensors/ comment |

### Theme 3 — AudioStreamer + SoundDetector concurrency hardening (uncommitted)

Both audio sensors were calling `analyze()` / `processBuffer()` directly on the AVAudioEngine render thread. Added dedicated serial `DispatchQueue` in each class and dispatched processing off the render thread:

- **AudioStreamer**: `processQueue` captures `converter` and `outputFormat` at registration time to avoid accessing `self` on render thread.
- **SoundDetector**: `processQueue` with `prevMag` and `lastFireTime` moved to queue-only access. Debounce duration corrected from comment "500 ms" (was already 0.2 in code — comment was wrong) to "200 ms".

### Theme 4 — LiDARStreamer row-packing fix (uncommitted, minor)

`depthBytes` row packing used `withUnsafeBytes(of: Array(...))` which was appending bytes of an `Array` struct header, not just the element bytes. Replaced with `Data(bytes: rowPtr, count: w * MemoryLayout<Float32>.size)` — correct packed send with no row-stride padding.

### Theme 5 — CI fix (commit `b98cea1`)

Xcode 16.3 on `macos-15` GitHub Actions runners requires an iOS simulator runtime for `actool` thinning even when archiving for device only. Added `xcodebuild -downloadPlatform iOS` step to `build-ipad-app.yml`.

---

## Housekeeping Performed Today (2026-05-17)

### Stale reference fixes

| File | Issue | Fix |
|------|-------|-----|
| `CLAUDE.md` L24 | Missing `SharedFaceSession` in Swift file list; count said "37 Swift files" (now 38 with SharedFaceSession) | Added `SharedFaceSession`, updated count to 38 |
| `CLAUDE.md` L130 | `routes 13 incoming message types` — stale after `tilt_position` was added yesterday | Changed to 14 |
| `CLAUDE.md` L205 | `iPad → PC (13 types):` list missing `tilt_position` | Changed to 14, added `tilt_position` to list |
| `.kiro/steering/structure.md` L30 | `SensorManager.swift # Lifecycle hub: starts/stops all 6 sensors` | Updated to 7, added SharedFaceSession mention |

### No code bugs found

- All Python files parse cleanly (syntax-checked: `fusion_engine.py`, `ipad_bridge.py`, `chatterbox_tts.py`, `health_viz.py`, `polly_stream.py`, `command_executor.py`, `approval_hook.py`, `tests/test_prop_ema_smoothing.py`, `tests/test_prop_tilt_range_effect.py`)
- `ScientificKeypadView.swift` diff removes `try?` from `NSExpression(format:)` — correct, since that initializer is non-throwing in Swift and `try?` was a no-op; actual nil-safety comes from the `guard let v = e.expressionValue(...)` check
- `LiDARStreamer.swift` CRLF warning is cosmetic (Windows git config); not a bug

### Design doc vs implementation gap (noted, not actioned)

`design.md` lists `tilt_pos_enabled: bool` and `tilt_pos_alpha: float` as `FusionConfig` fields. In the actual implementation, `_tilt_pos_alpha` is an instance attribute hardcoded to `0.4` and there is no `tilt_pos_enabled` field — `tilt_position` is processed whenever present. This is a minor spec/implementation divergence that does not affect behaviour.

### Open items (user decision required)

| Item | Notes |
|------|-------|
| Working branch changes uncommitted | `SharedFaceSession.swift` (untracked) + 8 modified files should be committed when ready |
| `tilt_pos_alpha` not in `FusionConfig` | Currently hardcoded 0.4; could be moved to FusionConfig to make it configurable |
| Stale references in worktrees | `lucid-chatelet-524921` and `trusting-joliot-9dc592` worktrees have pre-`tilt_position` CLAUDE.md; these are isolated worktrees and do not affect the active branch |

---

## Test count (as of 2026-05-17)

262 pytest tests + 30 standalone integration scripts + 15 Swift XCTest files = **307 total**

_(Up from 234 on 2026-05-16: +64 pytest cases from 2 new hypothesis test files, +9 Swift property test files)_
