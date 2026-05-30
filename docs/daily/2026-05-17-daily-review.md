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

### Round 1 (prior automated run — earlier today)

| File | Issue | Fix |
|------|-------|-----|
| `CLAUDE.md` L24 | Swift file list missing `SharedFaceSession`; count said "37 Swift files" | Added `SharedFaceSession`, bumped count to 38 |
| `CLAUDE.md` L130 | `routes 13 incoming message types` — stale after `tilt_position` added | Changed to 14 |
| `CLAUDE.md` L205 | `iPad → PC (13 types):` list missing `tilt_position` | Changed to 14, added `tilt_position` |
| `.kiro/steering/structure.md` L30 | `SensorManager.swift # Lifecycle hub: starts/stops all 6 sensors` | Updated to 7, added SharedFaceSession mention |

### Round 2 (this run)

| File | Issue | Fix |
|------|-------|-----|
| `.kiro/specs/ipad-sensor-focus/diagrams/00-index.md` L19 | `(13 types, 11 action verbs)` — 13 was stale; `tilt_position` makes 14 | Changed to 14 |
| `.kiro/specs/ipad-app-hardening/requirements.md` L10 | `all 6 sensors (... AudioStreamer)` — LiDAR added to SensorManager post-spec | Updated description to 7 sensors, added LiDARStreamer and SharedFaceSession note |
| `.kiro/specs/ipad-app-hardening/tasks.md` L25 | `instantiate all 6 sensors` | Updated to 7 sensors with SharedAudioSession + SharedFaceSession mention |
| `.kiro/specs/ipad-app-hardening/tasks.md` L28 | Combine subscriptions list missing `lidarEnabled` | Added `lidarEnabled` to subscription list |
| `iPadApp/DesktopAgent/SensorManager.swift` L408 | `SensorState.id` comment listed 6 IDs, missing `"lidar"` | Added `"lidar"` to the comment |
| `CLAUDE.md` L24 | `(38 Swift files)` — count was inaccurate; actual is 33 source files + 15 test files | Updated to `(33 Swift source files, 15 Swift test files)` |
| `CLAUDE.md` L64 | Test suite count stale: `234 total (2026-05-16)` | Updated to `307 total (2026-05-17)`: 262 pytest + 30 integration + 15 Swift XCTest |
| `CLAUDE.md` L15 | Status header still showed `2026-05-16` | Updated to reflect tilt-position-mapping + SharedFaceSession (2026-05-17) |

### Code review — no bugs found

- All 5 Python files syntax-checked clean: `fusion_engine.py`, `ipad_bridge.py`, `command_executor.py`, `approval_hook.py`, `chatterbox_tts.py`
- `tests/test_prop_ema_smoothing.py`, `tests/test_prop_tilt_range_effect.py` — syntax clean
- `SharedFaceSession.swift`: `@MainActor` class with `nonisolated` delegate methods + `DispatchQueue.main.async` fan-out — correct pattern for ARKit delegate thread model
- `GazeTracker.swift` + `HeadTracker.swift`: Both use `sharedFaceSession.addConsumer/removeConsumer` correctly; consumer IDs are static string constants — no registration collision possible
- `SensorManager.swift`: All 7 sensors started/stopped; Combine subscriptions for all 7 toggles; `SharedFaceSession` injected correctly
- `AudioStreamer.swift`: `processQueue` captures `converter` + `outputFormat` at start time — avoids render-thread races correctly
- `SoundDetector.swift`: `prevMag`, `lastFireTime` accessed only from `processQueue` — correct; `Task { @MainActor }` for WebSocket sends — correct
- `LiDARStreamer.swift`: Row-packing fixed (`Data(bytes: rowPtr, ...)` per row); `_LiDARThrottle` `@unchecked Sendable` pattern correct for serial ARSessionDelegate
- `lidar_receiver.py`: `is_fresh()` uses `_recv_mono` (monotonic) vs `time.monotonic()` — fix confirmed in place
- `gesture_processor.py`: `pinch_z_delta_mm` rename confirmed throughout

### Design doc / implementation gap (noted — user decision required)

`design.md` for tilt-position-mapping lists `tilt_pos_enabled: bool` and `tilt_pos_alpha: float` as `FusionConfig` fields. In the actual implementation, `_tilt_pos_alpha` is an instance attribute hardcoded to `0.4` and `tilt_pos_enabled` does not exist — `tilt_position` messages are always processed when present. This is harmless but the spec and implementation diverge. If the user wants these configurable, `_tilt_pos_alpha` should move to `FusionConfig`.

### Open items (user decision required)

| Item | Notes |
|------|-------|
| Uncommitted working changes | `SharedFaceSession.swift` (untracked) + 8 modified files should be committed when ready |
| `tilt_pos_alpha` not in `FusionConfig` | Currently hardcoded 0.4; spec listed it as configurable |
| Stale worktrees | `lucid-chatelet-524921` and `trusting-joliot-9dc592` have pre-`tilt_position` CLAUDE.md and pre-SharedFaceSession SensorManager — isolated, do not affect active branch |
| Diagram `08-bridge-message-routing.md` | Diagram contents inside the file may also reference 13 types; only the index was updated today |

---

## Test count (as of 2026-05-17)

262 pytest tests + 30 standalone integration scripts + 15 Swift XCTest files = **307 total**

_(Up from 234 on 2026-05-16: +64 pytest cases from 2 new hypothesis test files, +9 Swift property test files)_
