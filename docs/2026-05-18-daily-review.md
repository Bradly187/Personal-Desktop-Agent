# Daily Review — 2026-05-18

Automated housekeeping run. No user present.

---

## Yesterday's Work (2026-05-17) — Summary

24 commits across 6 themes on the `fix/ipad-regression-fixes` branch. Largest session on record by commit count.

### Theme 1 — iPad UX: First-run onboarding wizard

`OnboardingView.swift` (714 lines) — a 6-step wizard: Welcome → PC Connection (mDNS auto-discovery or manual IP) → Hardware Detection → Cursor Control (pick tilt/gaze/head/trackpad) → Calibration (tilt neutral) → Voice & Sound (keywords, mouth sounds, Whisper streaming) → Done. Persists `onboardingComplete` to UserDefaults so it only runs once; re-run from Settings.

### Theme 2 — iPad UX: Sensor status dashboard + calibration sheets

**`SensorDashboardView.swift`** (516 lines) — replaced the LiDAR-only Sensors tab with an all-sensor status dashboard showing per-sensor activity state, conflict detection, and live indicators.

**`SensorActivityBar.swift`** (70 lines) — compact horizontal strip showing which sensors are firing.

**Per-sensor calibration UX:**
| File | Purpose |
|------|---------|
| `GazeCalibrationSheet.swift` (296 lines) | 5-point gaze calibration with accuracy scoring |
| `TiltCalibrationSheet.swift` (153 lines) | Neutral gravity capture + range preview |
| `SoundTrainingSheet.swift` (308 lines) | Per-sound training: cluck / pop / hiss |

**`CursorConflictBanner.swift`** (166 lines) — inline banner shown when tilt + gaze are both active and fighting for cursor control; surfaces the conflict without blocking the UI.

**`CommandToast.swift`** (103 lines) — transient action feedback toast (e.g. "CLICK sent") with auto-dismiss.

Total new Swift UI files: **8** — bringing the SwiftUI app from 33 → 41 source files.

### Theme 3 — Gaze refactor: delta-based movement

Commit `8fc69d4`: `GazeTracker.swift` was rewritten to emit `gaze_delta` messages (relative eye movement) instead of absolute dwell coordinates. This removes the dwell-click behaviour from the gaze path entirely — gaze now controls cursor position; click is an explicit separate action. Added a configurable stability threshold to filter micro-tremors for glasses wearers.

Commit `5b29d05`: Glasses stability threshold exposed as a SettingsStore property with a SettingsView slider.

### Theme 4 — Sensor viewer desktop window

`sensor_viewer.py` (603 lines) — new tkinter desktop window running in a daemon thread:
- Camera + LiDAR depth panels side by side (aspect-ratio preserving)
- Connection status indicator (green=live, amber=stale, grey=no data)
- Hand landmark overlay from GestureProcessor
- Gaze cursor overlay on depth panel
- Freeze-frame (Space or button)
- Snapshot to disk (Ctrl+S → `snapshots/` directory)
- Depth-at-cursor readout on hover
- Always-on-top toggle (Ctrl+T)

Wired into `main.py` via `--viewer` (shows window alongside pipeline) and `--viewer-only` (connects to running bridge, no pipeline).

### Theme 5 — Tab navigation + regression fixes

Commit `4d3d4d7`: `ContentView.swift` — swipe-to-switch tabs (60pt threshold, first 4 tabs use `.tabViewStyle(.page)`); Settings and Sensors tabs are tap-only. Parent-driven scroll disable prevents scroll conflicts inside the page view.

Commit `2617e7e`: Resolved tab switching touch issues; custom tab bar confirmed always-on-top.

Commit `7c01d0e`: Resolved 8 regression issues across sensors and networking.

Commit `40159cb`: Fixed sensor cache/state/lifecycle issues across all sensors.

### Theme 6 — CI hardening

| Commit | Change |
|--------|--------|
| `abefa6d` | Switch to Xcode 16.4 — iOS 18.5 SDK/simulator match on macos-15 runners |
| `aeaf829` | Bump upload-artifact to v7 (Node 24) |
| `c7d115b` | Make TestFlight upload non-fatal (SDK version gate — build succeeds even if upload fails) |
| `e6a247a` | SharedFaceSession + LiDARStreamer CI blocker + audio render-thread safety |
| `9e71aa7` | Resolve CI build error — actor isolation + onChange deprecation |

---

## Housekeeping Performed Today (2026-05-18)

All fixes are working-tree changes not yet committed (consistent with prior housekeeping sessions).

### Stale reference fixes

| File | Line | Issue | Fix |
|------|------|-------|-----|
| `ipad_bridge.py` | L6 | Header said "14 total" and omitted `gaze_delta` from the type list, though `gaze_delta` was handled at L289 | Added `gaze_delta` entry; bumped count to "15 total" |
| `CLAUDE.md` | L7 | "constrained 9-verb action vocabulary" — stale since Phase 2 (now 16 verbs) | Changed to "16-verb action vocabulary (11 accessibility + 5 dev-agent)" |
| `CLAUDE.md` | L24 | "(33 Swift source files, 15 Swift test files)" — 8 new UI files added 2026-05-17 | Updated to "(41 Swift source files, 15 Swift test files)" and added all 8 new file names to the list |
| `CLAUDE.md` | L205 | "(14 types)" but listed 15 items (gaze_delta is listed but count not incremented) | Changed to "(15 types)" |
| `CLAUDE.md` | L130 | Key Files row for `ipad_bridge.py` said "routes 14 incoming message types" | Changed to "routes 15 incoming message types" |

### New entries added

| File | Addition |
|------|---------|
| `CLAUDE.md` Key Files | Added `sensor_viewer.py` row (tkinter desktop window with camera/depth/overlays) |
| `CLAUDE.md` Key Files | Updated `main.py` row to include `--viewer`/`--viewer-only` flags |
| `CLAUDE.md` Run Commands | Added `[--viewer] [--viewer-only]` to `main.py` usage line |
| `CLAUDE.md` Done blocks | Added "Done (iPad UX + gaze refactor + sensor viewer — 2026-05-17)" block with all 8 new UI files, sensor_viewer.py, gaze refactor, CI changes |

### Code review — Python

All Python files syntax-checked clean (`python -m py_compile`):
`ipad_bridge.py`, `fusion_engine.py`, `sensor_viewer.py`, `main.py`, `command_executor.py`, `gesture_processor.py`, `local_inference.py`, `hybrid_coordinator.py`, `continuous_trainer.py`, `db.py`, `domain_classifier.py`, `model_router.py`, `dev_agent.py`, `lidar_receiver.py`, `whisper_stream.py`, `polly_stream.py`, `chatterbox_tts.py`, `approval_hook.py`, `health_viz.py`

No logic issues found.

### Verified — resolved from yesterday's open items

| Item | Status |
|------|--------|
| `tilt_pos_alpha` not in FusionConfig | **Resolved** — commit `048b4c0` moved `_tilt_pos_alpha` into `FusionConfig.tilt_pos_alpha`; `self._cfg.tilt_pos_alpha` is used at L545 |

---

## Open Items (user decision required)

| Item | Notes |
|------|-------|
| `tilt_pos_enabled` not in FusionConfig | `tilt_position` messages are always processed; spec lists this as a configurable bool. Low impact — tilt_position without an enable gate is fine until user wants to disable it. |
| Uncommitted working-tree changes | `product.md` (gaze priority list updated to delta-based), `structure.md`, `figma-screen-spec.md` (swipe-to-switch tab gestures added), `docs/2026-05-08-daily-review.md` (GazeTracker description updated) — all correct changes, just need committing |
| Stale worktrees | `lucid-chatelet-524921`, `sleepy-bose-19425a`, `trusting-joliot-9dc592` — isolated pre-tilt_position state; do not affect active branch; safe to remove when convenient |

---

## Test count (as of 2026-05-18)

262 pytest tests + 30 standalone integration scripts + 15 Swift XCTest files = **307 total**

_(Unchanged from 2026-05-17 — no new tests added in today's housekeeping run)_
