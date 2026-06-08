# RealSense L515 — Mounting & Ergonomics (Workstream A4)

Physical placement of the Intel RealSense L515 for "minority report" gesture control
at the fixed desk. Mirrors the fixed-chair calibration assumption: the camera and the
user's hand-rest position are stable, so the gesture working volume can be tuned once.

## Sensor characteristics (relevant to placement)

- **Type:** solid-state LiDAR (MEMS) + RGB, USB-C (use a **USB3** port + cable).
- **Range:** 0.25 m – 9 m. **Accuracy is best at 0.3–1 m** — this is the gesture sweet-spot.
- **Depth FoV:** ~70° × 55°. **RGB FoV:** ~70° × 43°.
- **Indoor only** — the L515's IR LiDAR is washed out by direct sunlight. Avoid pointing
  it at a window; avoid strong IR sources in frame.
- L515 returns **0** for no-return / out-of-range pixels (handled as invalid → NaN in the
  pipeline via `synth_confidence`).

## Target working volume

Tune for the hand resting at **~0.3–0.8 m** from the camera, so the 13-gesture vocabulary
(peace-swipe, two-finger grab/release, grab-snap, monitor push/pull, pinch) sits squarely
in the high-accuracy band. The hand should not have to leave the armrest/keyboard plane to
enter frame — minimal reach is the RA-accessibility goal.

## Mounting options (to evaluate when the unit arrives)

| Option | Pros | Cons |
|--------|------|------|
| **Monitor-top, angled down** | natural, hand-over-keyboard volume; stable | may clip a tall reach; verify keyboard plane is in FoV |
| **Desk arm / small tripod beside monitor** | precise aim into the 0.3–0.8 m volume | desk footprint; cable run |
| **Low, angled up from desk edge** | clean view of raised hand, no keyboard clutter | requires lifting the hand (less RA-friendly) |

Default starting point: **monitor-top, tilted down ~20–30°** so the resting-hand plane is
centered vertically in the depth FoV.

## Setup checklist (fill in after physical install)

- [ ] USB3 port confirmed (`--list-devices` shows the L515; `dmesg`/Device Manager = SuperSpeed)
- [ ] Mounting position + tilt angle: __________
- [ ] Hand-rest distance to camera: ______ m (target 0.3–0.8 m)
- [ ] No window / strong IR in frame
- [ ] Verified in `sensor_viewer` (`python main.py --viewer`): hand fully in both RGB and
      depth frames across the full gesture range of motion
- [ ] Depth at fingertips reads sane metres (not NaN) throughout the working volume

## Calibration findings (this unit — serial f1061244, fw 1.5.4.1)

Verified live 2026-06-08 with `scripts/realsense_preview.py` + `scripts/validate_realsense.py`:

- **depth_scale = 0.000250 m/unit** (standard L515).
- **Orientation: camera is mounted upside down → `--rotate 180` for COLOR.**
  On this unit `rs.align(color)` returns **depth rotated 180° relative to color**, so
  **DEPTH needs `--depth-rotate 0`**. Net: `color=180, depth=0` makes both upright AND
  aligned. Verified by cross-check: with a hand at ~0.7 m, MediaPipe's landmark and the
  nearest-depth centroid coincide, and `get_depth_at(landmark)` returns the hand's true
  ~0.7 m (with `depth=180` it wrongly returned the ~3 m background). These are the
  publisher defaults now; do not "simplify" to rotating both the same.
- **USB link: currently USB2 (`usb=2.1`)** → depth capped at 320×240 and the publisher
  auto-clamps to 15 fps (30 fps resets the device on USB2). A true USB3 cable (ordered)
  unlocks 640×480 depth @ 30 fps; the publisher auto-detects and unclamps — no flag change.
- Sweet spot confirmed: hand at **~0.5–0.7 m** gives valid depth on the hand. Hand filling
  the frame (<~0.25 m) reads invalid/NaN (below L515 min range).

## Notes

- The capture sidecar (`sensors/realsense_publisher.py`) runs in `.venv-realsense` (Python 3.10);
  see `requirements-realsense.txt`. Start the main app first, then `start_realsense.bat`.
- Once placement is fixed, run Workstream A3 calibration so `ContinuousTrainer` learns velocity
  floors for *this* mounting geometry. Re-run if the camera is moved.
