# Regression Test Report — 2026-05-23

**Scope:** work performed on **2026-05-22** (uncommitted on master) plus a re-check of the outstanding action items from the 2026-05-22 sweep
**Reviewer:** automated regression sweep (scheduled task)
**Suite status:** ✅ `pytest tests/` — **557 passed, 0 failed** in 281.65s (13 warnings, all pre-existing 3rd-party deprecations)
**Runtime log:** `logs/agent_startup.log` — session 47 started today 08:47, current uptime ~22 min, 0 ERROR/CRITICAL events on the working tree as it stands

---

## Yesterday's Work (2026-05-22)

There are **no git commits** dated 2026-05-22. All of yesterday's work is still in the working tree (staged for a future commit). It covers the Sprint G1–G4 gaze-to-monitor calibration feature and the housekeeping pass documented in `docs/2026-05-22-daily-review.md`.

| Change | File(s) |
|---|---|
| Sprint G1 — angular gaze-to-pixel solver | `gaze_calibrator.py` (NEW, 310 LOC) |
| Sprint G2 — full-screen 5-dot tkinter overlay | `calibration_overlay.py` (NEW, 213 LOC) |
| Sprint G3 — `gaze_monitor_calibration` table + upsert/get methods | `db.py` (+82 LOC) |
| iPad world-ray export at 10 Hz | `iPadApp/.../GazeTracker.swift` (+39 LOC), `WebSocketManager.swift` (+6 LOC) |
| `gaze_ray` / `gaze_calibration_sample` handlers; ray attachment to `gaze_dwell`; docstring update (19→29 message types) | `ipad_bridge.py` (+102 / -34 LOC) |
| `FusionEngine.set_gaze_calibrator()`; `on_gaze_dwell(ray_dir=…)` override path | `fusion_engine.py` (+22 LOC) |
| Calibrator load + startup table row + bridge/fusion wiring | `main.py` (+23 LOC) |
| 22 new tests for calibrator | `tests/test_gaze_calibrator.py` (NEW) |
| 29 + 22 tests for UIAutomation + ActionVerifier (carried over from 05-21 work) | `tests/test_ui_automation.py`, `tests/test_action_verifier.py` (NEW) |
| Housekeeping: docstring 19→29, CLAUDE.md table counts 21→27, status header, WebSocket protocol section rewrite, dead `_GAZE_RAY_MAX_AGE_S` removed | `ipad_bridge.py`, `CLAUDE.md`, `.kiro/specs/.../tasks.md`, `docs/2026-05-21-daily-review.md` |

---

## Findings

### A. NEW issues introduced in 2026-05-22 work

#### A1. [iPadApp/DesktopAgent/Sensors/GazeTracker.swift:285–296](iPadApp/DesktopAgent/Sensors/GazeTracker.swift#L285) — `gaze_ray` only fires while the eyes are moving (HIGH, logic error)

The 10 Hz `sendGazeRay()` call is placed **after** the early-return guard:

```swift
// Only suppress truly zero output to avoid unnecessary WebSocket traffic.
guard abs(dx) > 0.01 || abs(dy) > 0.01 else { return }

// Send world-space gaze ray at ~10 Hz (every 6th frame) for monitor calibration.
rayFrameCounter += 1
if rayFrameCounter >= Self.rayFrameInterval {
    rayFrameCounter = 0
    if let ray = currentWorldRay {
        ws?.sendGazeRay(...)
    }
}
```

When the user **dwells** on a target — exactly the precondition for a `gaze_dwell` event — `dx`/`dy` collapse to near-zero, the guard returns, and `gaze_ray` is never sent. Meanwhile `ipad_bridge.py` only attaches a ray to `gaze_dwell` if the latest sample is **< 300 ms old** ([ipad_bridge.py:382–386](ipad_bridge.py#L382)).

**Net effect:** at the exact moment the calibrated absolute-pixel projection is required, the freshest ray is typically 600 ms – 2 s old and is rejected by the freshness check. The calibration code path is effectively dead in production.

**Fix:** hoist the `rayFrameCounter` block above the delta guard so the ray is sent unconditionally on every 6th frame regardless of whether a `gaze_delta` is emitted.

#### A2. [gaze_calibrator.py:248](gaze_calibrator.py#L248) / [gaze_calibrator.py:220](gaze_calibrator.py#L220) — JSON I/O uses platform default encoding (LOW, portability)

```python
_JSON_PATH.write_text(json.dumps(data, indent=2))   # save
data = json.loads(_JSON_PATH.read_text())           # load
```

`Path.write_text` / `read_text` use the OS default encoding (cp1252 on most Windows installs), but JSON is specified as UTF-8. If a future field ever contains non-ASCII (e.g. a comment field or a user note), save/load can break across hosts.

**Fix:** add `encoding="utf-8"` to both calls.

#### A3. [gaze_calibrator.py:167](gaze_calibrator.py#L167) — Synchronous file I/O inside `solve()` (LOW, concurrency)

`solve()` is documented as CPU-bound and "should be wrapped in `asyncio.to_thread` if called from async context". The call to `self._save_json()` at the end performs disk I/O on the same thread. The docstring is correct; whoever wires the voice-trigger path ("hey agent calibrate monitor") must remember to `to_thread` the whole `solve()` call. **Action:** add a one-line reminder in the eventual wiring code so this doesn't silently block the event loop the day the feature ships.

#### A4. [fusion_engine.py:564–572](fusion_engine.py#L564) — `on_gaze_dwell` normalises pixels back into [0,1] (INFO, design smell)

```python
if ray_dir is not None and self._gaze_calibrator is not None:
    result = self._gaze_calibrator.project(ray_dir)
    if result is not None:
        x = result[0] / self._w
        y = result[1] / self._h
self._gaze_dwell = (x, y)
```

The calibrator returns clamped *integer pixels*; this code immediately divides by `self._w`/`self._h` to fit the dataclass's existing normalised representation, and the consumer presumably re-multiplies further downstream. Not a bug, but each round-trip introduces sub-pixel quantisation. Worth eventually adding a separate `_gaze_dwell_px: Optional[tuple[int,int]]` field to bypass the conversion.

#### A5. [ipad_bridge.py:459–478](ipad_bridge.py#L459) — `gaze_calibration_sample` does no per-session de-dup (LOW)

The handler accepts an unlimited number of samples with the same `dot_index`. If iPad UX retries on a missed dwell, multiple readings for one dot will be silently appended and tilt the least-squares fit. The eventual UI sheet should clear pending samples for the dot before sending a retry, or the bridge should pop/replace by index.

#### A6. [main.py:204–214](main.py#L204) — startup `_check_gaze_calibration` calls `cal.get_status()["calibrated_at"]` even when 0 (TRIVIAL)

If `calibrated_at` was never set (corrupt sidecar that parses but is missing the field, defaulted to 0.0 by `load()`), `age_days = (now - 0)/86400` reports ~20,000 days. Not crash-worthy because the surrounding `check()` wrapper traps exceptions, but the displayed string is misleading. Add a `calibrated_at > 0` check before computing age.

#### A7. [iPadApp/DesktopAgent/Sensors/GazeTracker.swift:46–49](iPadApp/DesktopAgent/Sensors/GazeTracker.swift#L46) — `currentWorldRay` read concurrency (LOW)

```swift
nonisolated(unsafe) var currentWorldRay: (origin: simd_float3, dir: simd_float3)?
```

Tuple of two `simd_float3` is **6 floats = 24 bytes**, not a single-word write. The "single-word assignment" claim in the comment is wrong. In practice writes and reads happen on the same `processQueue` serial queue (the comment line below confirms it), so it is safe — but the *justification given* is incorrect. Replace the misleading comment, or wrap the field in an `OSAllocatedUnfairLock` for defence in depth.

### B. Items carried over from the 2026-05-22 sweep — still unfixed

| # | Item | Status | Evidence |
|---|---|---|---|
| 1 | Watchdog Hz unit confusion ([main.py:347](main.py#L347)) — `hz = current_ticks - _last_tick_count` over a 60 s window, mislabelled as Hz | ❌ **Still broken.** Today's log [logs/agent_startup.log:30](logs/agent_startup.log#L30) reads `WATCHDOG: FusionEngine 1922 Hz` consistently; the real tick rate is ~32 Hz, below the 60 Hz target. The 50-Hz guardrail never fires. |
| 2 | `_ipad_log_tasks` untracked fire-and-forget ([ipad_bridge.py:825](ipad_bridge.py#L825) — pre-yesterday code) | ❌ **Unchanged.** No `add_done_callback` / set tracking added. |
| 3 | `agent.db-shm`, `agent.db-wal` not in `.gitignore` | ❌ **Confirmed by today's `git status`** — both appear as `??`. |
| 4 | `subsystem` field on `ipad_log` not whitelisted | ❌ Unchanged. |
| 5 | `start_agent.bat` log truncate-on-restart | ❌ Unchanged. |
| 6 | Dead code in `_watchdog` (`_orig_tick`, `hasattr` branch) | ❌ Unchanged. |

### C. Soak / runtime health (POSITIVE)

- Today's session 47 started 08:47 with all uncommitted Sprint G1–G4 code loaded.
- Startup table now includes a `Gaze monitor calibration` row (renders as `WARN: not calibrated` since `gaze_calibration.json` does not yet exist — expected).
- `FusionEngine: GazeCalibrator wired (calibrated=False)` log line confirms bridge↔fusion↔calibrator wiring is exercised on every boot.
- No new exceptions, no Whisper death, no Tracker crashes after wiring the new module paths.
- GPU VRAM steady at 26.2 GB free across the run.
- Pre-existing `Ollama unreachable` warning every 10 min — environmental on this host.

---

## Security Review (new surface only)

| Concern | Status |
|---|---|
| **`gaze_ray` payload validation** ([ipad_bridge.py:362–371](ipad_bridge.py#L362)) | ✅ All three components are `float()`-coerced inside `try/except (ValueError, TypeError)`. Magnitude < 1e-9 guard prevents NaN/inf division. Bridge stores the normalised vector only. |
| **`gaze_calibration_sample` validation** ([ipad_bridge.py:462–477](ipad_bridge.py#L462)) | ✅ All fields coerced (`int(dot_index)`, `int(px_x/px_y)`, `float(ray_*)`); negative dot_idx silently discarded. Possible abuse vector below ↓ |
| **No bound on dot_index / per-session sample count** | ⚠️ A LAN attacker could spam thousands of `gaze_calibration_sample` messages and bloat memory in `GazeCalibrator._samples` (Python list, ~80 B per `_CalibSample`). Bounded by `solve()` consuming them on success, but an attacker could indefinitely add samples without ever calling solve. Trust boundary is the iPad WebSocket — acceptable on a single-user LAN but should be hardened (cap `len(self._samples)` at e.g. 50) before any multi-user exposure. |
| **GazeCalibrator JSON load** ([gaze_calibrator.py:212–234](gaze_calibrator.py#L212)) | ✅ Wraps everything in `try/except`. `json.loads` is safe (no `eval`). No path traversal — `_JSON_PATH` is a module constant. |
| **`gaze_monitor_calibration` DB insert** ([db.py:807](db.py#L807)) | ✅ All values parameterised; JSON-serialised structures are stored as TEXT. Parameter list matches schema. |
| **`gaze_ray` does not assert `confidence ∈ [0, 1]`** ([ipad_bridge.py:362](ipad_bridge.py#L362)) | ℹ️ Field is read but not used (only `dx/dy/dz` go into `_latest_gaze_ray`). Not a security concern. |

---

## Dependency / Concurrency Audit

| Risk | Verdict |
|---|---|
| `gaze_calibrator.py` imports `numpy` lazily inside each method | ✅ Good — module is importable on systems without numpy, fails gracefully at solve/project time. |
| `calibration_overlay.py` imports `tkinter` lazily inside `_run_tk` | ✅ Good — daemon-thread degradation is correct. |
| `currentWorldRay` Swift cross-thread write | ⚠️ Comment misleading (see A7) but practically safe (single serial queue). |
| `_latest_gaze_ray` Python read/write across handlers | ✅ Single asyncio loop, no race. |
| `solve()` synchronous disk I/O in a future async caller | ⚠️ Not wired yet; flagged for the trigger work (A3). |
| New tables vs WAL mode | ✅ `gaze_monitor_calibration` declared inside the same `_SCHEMA` string applied at startup; benefits from WAL mode automatically. |
| `numpy.linalg.lstsq` `rcond=None` | ✅ Correct — silences NumPy 1.14+ FutureWarning and uses the new default. |
| Test suite | ✅ 557/557 pass. No flakes observed in this run. |

---

## Action Items (prioritised)

1. **[NEW, HIGH] Move the `gaze_ray` sender above the delta-zero guard in `GazeTracker.swift`** — the calibrated dwell path is currently broken when the user actually dwells (A1).
2. **[carried, HIGH] Fix watchdog Hz math in `main.py:347`** — divide by 60 and re-check threshold; the soak guardrail remains non-functional. *FusionEngine is currently running at ~32 Hz vs configured 60 Hz — investigate cause separately once the watchdog reports honest numbers.*
3. **[carried, LOW] Track `_ipad_log_tasks`** in `ipad_bridge.py` to prevent GC and silent exception loss.
4. **[carried, LOW] Add `agent.db-shm` / `agent.db-wal` to `.gitignore`.**
5. **[NEW, LOW] Add `encoding="utf-8"`** to `gaze_calibrator.py` JSON read/write calls (A2).
6. **[NEW, LOW] Cap `GazeCalibrator._samples` length** (e.g. 50) and replace-by-`dot_index` instead of append (A5).
7. **[NEW, TRIVIAL] Guard `calibrated_at > 0`** in `main.py` startup-table age display (A6).
8. **[NEW, TRIVIAL] Replace the incorrect "single-word assignment" comment** on `currentWorldRay` (A7).
9. **[carried, LOW] Whitelist iPad-log `subsystem` field** to `[A-Za-z0-9_.]{1,32}`.
10. **[carried, LOW] Preserve previous startup log** on `start_agent.bat` restart (copy-before-truncate).
11. **[carried, TRIVIAL] Remove dead code** in `_watchdog` (`_orig_tick`, `hasattr` branch).

---

## Regressions Detected

**None.** The 557-test pytest suite passes. The runtime agent boots cleanly with all Sprint G1–G4 code wired. The startup table row, fusion log line, and bridge handlers all execute. The watchdog still mislabels Hz exactly as it did before yesterday's work — the bug is **pre-existing**, not new.

## Logic Errors Detected

One real logic error (A1) that **silently breaks the calibrated-dwell path** as soon as the user stops moving their eyes — i.e., in the only situation the feature is meant to handle. This was missed because no integration test sends a real-world gaze stream; the unit tests of `GazeCalibrator` exercise the math in isolation. Recommend a future Swift XCTest or Python end-to-end test that drives gaze_delta → idle → gaze_dwell and asserts the bridge's `_latest_gaze_ray_ts` is fresher than 300 ms at the moment of dwell.
