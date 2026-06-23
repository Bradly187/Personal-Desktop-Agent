# Head-Pointer Calibration Handoff — 2026-06-23

## Session Goal
Fine-tune the Intel RealSense L515 head-pointer cursor control (`feat/realsense-l515` branch) for single-user accessibility use with rheumatoid arthritis.

## What Was Accomplished

### DPI Awareness Fix (not yet committed)
`main.py` now calls `_set_process_dpi_aware()` at startup — matches the validator's DPI setup so calibration coordinates are consistent (physical pixels, not logical). Change is in the working tree on `feat/realsense-l515`, uncommitted.

### `scripts/save_frame.py` (new debug tool)
Minimal aiohttp WS server that captures one `camera_frame` from the sidecar and saves it to `logs/debug_frame.jpg`. Use to verify camera orientation before starting calibration. Usage:
```powershell
# Terminal 1: start sidecar pointed at port 8800
.\.venv-realsense\Scripts\python.exe sensors/realsense_publisher.py --host 127.0.0.1 --port 8800 --rotate 0

# Terminal 2: capture frame
.\.venv\Scripts\python.exe scripts/save_frame.py 8800 logs/debug_frame.jpg
```

### Root Cause of `faces=0`
The camera was physically pointing at the bedroom wall/bed — NOT at the user's face. Both rotation modes (`--rotate 0` and `--rotate 180`) produced `faces=0` because MediaPipe never saw a face. The debug frame (`logs/debug_frame.jpg`) confirmed this.

The image was also rotated 90° sideways (needs `--rotate 90` to be right-side up based on the captured frame), but this is secondary — camera direction must be fixed first.

## Current State

### Processes
All stopped. Nothing is running.

### Files
| File | State |
|------|-------|
| `head_pointer_calibration.json` | Has `center_u=0.15` — needs re-tuning after camera repositioned |
| `main.py` | DPI awareness added, **uncommitted** |
| `scripts/save_frame.py` | New debug tool, untracked |
| `logs/debug_frame.jpg` | Debug capture showing bedroom (camera mispointed) |
| `logs/headtrack_live_err.log` | Last run log |
| `logs/sidecar_err.log` | Last sidecar log |

### Calibration JSON (current)
```json
{
  "monitors": {
    "3840x2160@0,0": {
      "in_x0": -0.18,
      "in_x1": 0.55,
      "in_y0": 0.05,
      "in_y1": 0.40,
      "center_u": 0.15,
      "center_v": 0.19,
      "invert_x": false,
      "invert_y": false,
      "depth_comp": 0.15,
      "nose_ref": [0.493, 0.670]
    }
  }
}
```

## Known-Good Reference (9:26 session — tracking worked)
```json
"center_u": 0.025,
"in_x0": -0.18,
"in_x1": 0.55,
"invert_x": false,
"invert_y": false
```
Cursor landed at ~x=2400 (physical pixels) when user looked forward. User's natural gaze center is ~x=2560 (right third of 4K, because portrait monitor CR245ZB sits to the right).

**Key formula for center_u tuning:** To map rest_yaw=R to target screen_x=T:
```
center_u = R - (T/3840 - 0.5) * (in_x1 - in_x0)
```
With rest_yaw≈0.15, target x≈2560: `center_u ≈ -0.05`

## Monitor Layout
- **Primary (left):** LG HDR 4K landscape — 3840×2160 physical at virtual (0,0)
- **Secondary (right):** Acer CR245ZB portrait — 1080×1920 physical at virtual (3840,0)
- User's natural forward gaze hits the **right third** of the 4K (≈x=2560) because the portrait monitor is to the right and they naturally look slightly right

## Architecture Quick Reference

```
L515 sidecar (.venv-realsense Python 3.10)
  sensors/realsense_publisher.py --port 8799
  streams: camera_frame (JPEG) + depth_frame (float32 + conf)
    ↓ WebSocket ws://127.0.0.1:8799/ws
Validator (main .venv Python 3.14)
  scripts/validate_headtrack.py 8799 --pointer
  uses: FaceTracker → head_angles → HeadPointer → SetCursorPos
```

Key files:
- `sensors/head_pointer.py` — all calibration math (`HeadPointerConfig`, `_seg`, `head_angles`)
- `sensors/face_tracker.py` — MediaPipe Face Landmarker (VIDEO mode)
- `scripts/validate_headtrack.py` — standalone test/calibration tool

## Sign Conventions (verified from 9:26 logs)
- **Yaw**: negative = looking LEFT → cursor LEFT; positive = looking RIGHT → cursor RIGHT
- **Pitch**: low (≈0.03) = chin UP → screen top; high (≈0.38) = chin DOWN → screen bottom
- `invert_x=false`, `invert_y=false` is the correct setting — do NOT invert

## Process Management
Always use WMI to check/kill processes in PowerShell 5.1:
```powershell
# Find
Get-WmiObject Win32_Process | Where-Object { $_.CommandLine -like "*realsense*" -or $_.CommandLine -like "*validate_headtrack*" } | Select-Object ProcessId, @{N='CMD';E={$_.CommandLine.Substring(0,100)}}

# Kill by PID
Stop-Process -Id <PID> -Force
```

The `.venv-realsense\Scripts\python.exe` (wrapper) spawns the real `C:\Users\bradt\AppData\Local\Programs\Python\Python310\python.exe` as a child — this is **normal** Windows venv behavior, not a duplicate sidecar.

## Next Session: Step-by-Step

### Step 1 — Reposition Camera
Physically place the L515 at the top of the monitor, **lens pointing toward your face**. The USB cable direction determines the rotation needed:
- Cable at bottom (typical monitor-top mount, cam right-side up): try `--rotate 0`
- Cable at top (cam upside down): try `--rotate 180`

### Step 2 — Verify Camera Frame
```powershell
# Terminal 1
cd E:\Personal_Desktop_Agent
.\.venv-realsense\Scripts\python.exe sensors/realsense_publisher.py --host 127.0.0.1 --port 8800 --rotate 0

# Terminal 2 (immediately after)
.\.venv\Scripts\python.exe scripts/save_frame.py 8800 logs/debug_frame.jpg
```
Open `logs/debug_frame.jpg` and confirm:
- Your face is visible and right-side-up
- If sideways, try `--rotate 90` or `--rotate 270` and re-capture
- If upside-down, try `--rotate 180`

### Step 3 — Start Tracker (observe mode first)
```powershell
# Terminal 1: sidecar (use the --rotate value confirmed in Step 2)
.\.venv-realsense\Scripts\python.exe sensors/realsense_publisher.py --host 127.0.0.1 --port 8799 --rotate 0

# Terminal 2: validator in observe mode (does NOT move cursor)
.\.venv\Scripts\python.exe scripts/validate_headtrack.py 8799
```
Watch the log. Confirm `faces=1` appears and `feat=(yaw, pitch)` values show movement when you move your head.

### Step 4 — Tune center_u (the key calibration parameter)
In observe mode, look directly at your natural resting position (right third of 4K, ~x=2560). Note the yaw value printed (e.g., `feat=(0.12, 0.22)`). That yaw is your `rest_yaw`.

Edit `head_pointer_calibration.json`:
```json
"center_u": <rest_yaw>
```
This puts the cursor at screen center (x=1920) when you look at your rest position. If your rest position is x=2560 (not center), use:
```python
# center_u such that _seg(rest_yaw, -0.18, center_u, 0.55) = 2560/3840 = 0.667
# Solve: 0.667 = 0.5 + 0.5 * (rest_yaw - center_u) / (0.55 - center_u)
# Approximate: center_u ≈ rest_yaw - 0.20  (for rest position at right third)
```

### Step 5 — Switch to Pointer Mode and Test
```powershell
.\.venv\Scripts\python.exe scripts/validate_headtrack.py 8799 --pointer
```
Move your head. Cursor should follow. If inverted, check logs — `invert_x/y` should both be `false`.

### Step 6 — Commit DPI Fix
```powershell
git add main.py
git commit -m "fix(headtrack): set per-monitor DPI awareness for physical-pixel cursor coords"
```

## Potential Issues & Fixes

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| `faces=0` after camera repositioned | Wrong rotation | Try each `--rotate` value, verify with `save_frame.py` |
| `faces=0` even with correct rotation | User not in camera FOV / too dark | Move closer, improve lighting |
| Cursor inverted X | `invert_x=true` in JSON | Set to `false` |
| Cursor inverted Y | `invert_y=true` in JSON | Set to `false` |
| Cursor drifts right/left of gaze | `center_u` wrong | Tune per Step 4 |
| Two sidecars (camera conflict) | Old sidecar still running | Kill by WMI before restarting |
| Smooth but lagging cursor | `min_cutoff` too low | Raise from 0.4 toward 0.8 |
| Jittery cursor | `beta` too high | Lower from 0.02 toward 0.005 |
