"""Headless RealSense L515 gesture-pipeline validator (SAFE — executes nothing).

Runs the REAL GestureProcessor + LiDARReceiver behind a minimal aiohttp WS
server at /ws (mirroring core/ipad_bridge.py), so the realsense_publisher.py
sidecar can stream into it exactly as it would into the real app. It logs:

  * frames received (camera / depth) and depth freshness
  * whether a hand is detected, and the index-fingertip depth in metres
    -> this is the rs.align() correctness check (the #1 integration risk)
  * any gesture Command the pipeline would have fired (but does NOT execute it)

With --show it also opens a live window: color (with hand landmarks + the last
fired gesture drawn on it) next to the aligned depth heatmap. This lets you watch
detection live WITHOUT a second camera owner (the frames come from the sidecar
stream, not a direct camera open).

Usage (Python 3.14 main env):
    python scripts/validate_realsense.py [PORT] [--show]
Then start the sidecar (Python 3.10 venv): start_realsense.bat --port PORT
Ctrl-C / q to stop. Nothing touches the desktop.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import os
import sys
import time


def _set_dpi_aware() -> None:
    """Make the process per-monitor DPI-aware. MUST run before cv2/mediapipe
    import (whichever lib touches the display first can lock the awareness) and
    before any geometry query, so SetCursorPos / GetSystemMetrics coords match
    UIAutomation's physical element bounds — otherwise cursor gravity snaps to
    the wrong place. Return values are checked (these APIs signal failure by
    return value, not exception); we fall through until one sticks."""
    import ctypes
    # shcore.SetProcessDpiAwareness(2)=PER_MONITOR: HRESULT, 0 == S_OK.
    try:
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:
            return
    except Exception:
        pass
    # SetProcessDpiAwarenessContext(PER_MONITOR_AWARE_V2 = -4): BOOL, !=0 == ok.
    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()   # legacy system-DPI aware
    except Exception:
        pass


if sys.platform == "win32":
    _set_dpi_aware()   # before cv2/mediapipe import below

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
CAL_PATH = os.path.join(REPO_ROOT, "hand_pointer_calibration.json")

import numpy as np
import cv2
from aiohttp import web

from sensors.gesture_processor import GestureProcessor
from sensors.lidar_receiver import LiDARReceiver
from sensors.hand_pointer import (
    HandPointer, HandPointerConfig, fingertip_centroid, ThumbClick, ThumbClickConfig,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("validate_realsense")

_INDEX_TIP = 8   # MediaPipe index fingertip
_MIDDLE_TIP = 12
_THUMB_TIP = 4
# Minimal hand skeleton connections (MediaPipe indices) for the overlay.
_BONES = [(0,1),(1,2),(2,3),(3,4),(0,5),(5,6),(6,7),(7,8),(5,9),(9,10),(10,11),(11,12),
          (9,13),(13,14),(14,15),(15,16),(13,17),(17,18),(18,19),(19,20),(0,17)]

stats = {"camera": 0, "depth": 0, "hands": 0, "gestures": 0}
_last_status = 0.0
_last_gesture = ""      # text of last fired gesture (for the overlay)
_last_gesture_t = 0.0

SHOW = "--show" in sys.argv
POINTER = "--pointer" in sys.argv   # Relaxed Pointer test: move real cursor, dwell-click (NOT executed)
CAL_ROM = "--calibrate-rom" in sys.argv   # record natural range of motion -> CAL_PATH
CAL_CORNERS = "--calibrate-corners" in sys.argv  # guided point-to-each-corner calibration -> CAL_PATH
NO_DWELL = "--no-dwell" in sys.argv       # pointer test: movement only, no dwell click
THUMB_CLICK = "--thumb-click" in sys.argv # click when thumb tip rests near index/middle tips
EXECUTE = "--execute" in sys.argv         # actually perform real mouse clicks (else just log)
FREEZE_RATIO = 0.95                        # freeze cursor while thumb ratio is below this (pinching)
ROM_SECONDS = 15.0
# Corner calibration: point to each monitor corner and HOLD still to capture it.
CORNER_LABELS = ("TOP-LEFT", "TOP-RIGHT", "BOTTOM-RIGHT", "BOTTOM-LEFT")


def _argval(flag: str, default: float) -> float:
    """Read a numeric value following `flag` in argv (e.g. --hold 4), else default."""
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            try:
                return float(sys.argv[i + 1])
            except ValueError:
                pass
    return default


CORNER_HOLD_S = _argval("--hold", 3.0)   # hold still this long to capture (--hold N to change)
CORNER_RADIUS = 0.045     # normalized stillness radius for the hold
CORNER_GAP_S = _argval("--gap", 1.5)     # "captured!" flash / settle gap before the next corner
WIN = "L515 validator  (color+landmarks | depth)   q to quit"

_pointer = None          # HandPointer instance (pointer mode)
_thumb = None            # ThumbClick instance (thumb-pinch click)
_tcache = None           # ClickableTargetCache (cursor gravity, pointer mode)
_monitors = []           # [(x, y, w, h), ...] sorted left-to-right
_active_mon = 0          # index of the monitor the hand currently controls
_cal_target = None       # (x, y, w, h) monitor being calibrated (calibration mode)
_cal_mon_idx = 0         # index of that monitor
_last_click_t = 0.0      # for the on-screen CLICK flash
_last_centroid = None    # (nx, ny) for drawing the dwell ring
_last_dwell = 0.0
_last_ratio = None       # thumb-finger ratio for the overlay

# Range-of-motion calibration state
_rom_samples = []        # list of (nx, ny) fingertip centroids
_rom_start = None        # monotonic time of first sample
_rom_done = False
_rom_box = None          # (x0, y0, x1, y1) fitted box

# Corner-calibration state
_corner_idx = 0                 # which corner we're capturing (0..3)
_corner_results = []            # captured (nx, ny) median per corner, in order
_corner_anchor = None           # (nx, ny) current hold anchor
_corner_anchor_t = 0.0          # monotonic time the hold started
_corner_hold = []               # centroids accumulated within the hold radius
_corner_captured_t = 0.0        # monotonic time of the last capture (for the gap/flash)
_corner_progress = 0.0          # 0..1 hold progress for the overlay ring
_corner_done = False
_corner_bad = False             # last capture was non-convex (rejected; redo)
_corner_jitter = None           # (jitter_x, jitter_y) std dev of current hold window


def _mon_key(m) -> str:
    """Stable per-monitor calibration key: 'WxH@X,Y' (resolution + position)."""
    return f"{m[2]}x{m[3]}@{m[0]},{m[1]}"


def _load_cal_entry(mon_key=None) -> dict | None:
    """Return the calibration dict for `mon_key` from CAL_PATH, or None.

    Prefers the per-monitor entry (data['monitors'][mon_key]); falls back to a
    legacy top-level single-monitor calibration only when there's no per-monitor
    table at all (so old files still load for the primary)."""
    try:
        with open(CAL_PATH) as f:
            d = json.load(f)
    except Exception:
        return None
    mons = d.get("monitors")
    if mon_key and isinstance(mons, dict):
        return mons.get(mon_key)   # None if this monitor isn't calibrated
    if "corners" in d or "in_x0" in d:   # legacy single-monitor file
        return d
    return None


def load_pointer_config(mon_key=None) -> HandPointerConfig:
    """Load the input box + corner calibration for a specific monitor."""
    cfg = HandPointerConfig()
    e = _load_cal_entry(mon_key)
    if not e:
        log.info("no calibration for monitor %s — using default box", mon_key)
        return cfg
    try:
        cfg.in_x0, cfg.in_y0 = float(e["in_x0"]), float(e["in_y0"])
        cfg.in_x1, cfg.in_y1 = float(e["in_x1"]), float(e["in_y1"])
        cc = e.get("corners")
        if isinstance(cc, dict) and all(k in cc for k in CORNER_LABELS):
            cfg.corners = [list(map(float, cc[k])) for k in CORNER_LABELS]
            log.info("loaded 4-corner perspective calibration for monitor %s", mon_key)
        else:
            log.info("loaded box calibration for monitor %s", mon_key)
    except Exception as e2:  # noqa: BLE001
        log.warning("failed to parse calibration for %s: %s", mon_key, e2)
    return cfg


def _nearest_centroid(depth):
    """Normalized (nx, ny) centroid of the nearest depth region (alignment check)."""
    if depth is None:
        return None, None
    if np.all(np.isnan(depth)):
        return None, None
    dmin = float(np.nanmin(depth))
    mask = depth <= (dmin + 0.10)
    ys, xs = np.where(mask)
    if xs.size == 0:
        return None, None
    h, w = depth.shape
    return float(xs.mean() / (w - 1)), float(ys.mean() / (h - 1))


def _enumerate_monitors() -> list:
    """Per-monitor rects [(x, y, w, h), ...] in virtual-desktop coords, sorted
    left-to-right. Empty list on failure (caller falls back to one rect)."""
    import ctypes
    from ctypes import wintypes
    mons: list = []
    try:
        MONITORENUMPROC = ctypes.WINFUNCTYPE(
            ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.POINTER(wintypes.RECT), ctypes.c_void_p)

        def _cb(hmon, hdc, lprc, lparam):
            r = lprc.contents
            mons.append((int(r.left), int(r.top),
                         int(r.right - r.left), int(r.bottom - r.top)))
            return 1

        ctypes.windll.user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(_cb), 0)
        mons.sort(key=lambda m: m[0])
    except Exception as exc:  # noqa: BLE001
        log.warning("monitor enumeration failed: %s", exc)
    return mons


def _cycle_monitor() -> None:
    """Switch the hand-controlled monitor to the next one ('m' key), loading
    THAT monitor's own calibration (so a portrait screen uses portrait corners)."""
    global _active_mon
    if not _monitors or _pointer is None:
        return
    _active_mon = (_active_mon + 1) % len(_monitors)
    m = _monitors[_active_mon]
    new_cfg = load_pointer_config(_mon_key(m))
    _pointer.cfg.corners = new_cfg.corners
    _pointer.cfg.in_x0, _pointer.cfg.in_y0 = new_cfg.in_x0, new_cfg.in_y0
    _pointer.cfg.in_x1, _pointer.cfg.in_y1 = new_cfg.in_x1, new_cfg.in_y1
    _pointer.set_screen_rect((m[0], m[1]), (m[2], m[3]))
    if new_cfg.corners is None:
        log.warning("ACTIVE MONITOR -> %d/%d (%dx%d) — NOT CALIBRATED; "
                    "run --calibrate-corners and press 'm' to calibrate it",
                    _active_mon + 1, len(_monitors), m[2], m[3])
    else:
        log.warning("ACTIVE MONITOR -> %d/%d  rect=(x=%d,y=%d,%dx%d)  [calibrated]",
                    _active_mon + 1, len(_monitors), m[0], m[1], m[2], m[3])


def _cycle_cal_monitor() -> None:
    """In calibration mode: choose the next monitor to calibrate ('m' key)."""
    global _cal_mon_idx, _cal_target
    if not _monitors:
        return
    _cal_mon_idx = (_cal_mon_idx + 1) % len(_monitors)
    _cal_target = _monitors[_cal_mon_idx]
    _reset_corners()
    log.warning("CALIBRATING MONITOR -> %d/%d  (%dx%d)",
                _cal_mon_idx + 1, len(_monitors), _cal_target[2], _cal_target[3])


def _is_convex_quad(pts) -> bool:
    """True if the 4 points (TL,TR,BR,BL order) form a convex, non-self-
    intersecting quad. A crossed/concave capture makes the homography fold, so
    we reject it and ask for a redo."""
    sign = 0
    for i in range(4):
        ax, ay = pts[i]
        bx, by = pts[(i + 1) % 4]
        cx, cy = pts[(i + 2) % 4]
        cross = (bx - ax) * (cy - by) - (by - ay) * (cx - bx)
        if abs(cross) < 1e-9:
            continue
        s = 1 if cross > 0 else -1
        if sign == 0:
            sign = s
        elif s != sign:
            return False
    return True


def _finalize_corners() -> None:
    """Build the input box from the 4 captured corner positions and save it.

    Rejects a non-convex (crossed/concave) capture — those fold the homography —
    by flagging _corner_bad and NOT saving, so the user redoes the monitor.
    """
    global _corner_done, _corner_bad
    _corner_done = True  # always stop capture; _corner_bad drives the rejection overlay
    if not _is_convex_quad(_corner_results):
        _corner_bad = True
        log.warning("CALIBRATION REJECTED [monitor %s]: corners form a crossed/"
                    "concave quad %s — press 'r' and recapture with clearer "
                    "corner separation",
                    _mon_key(_cal_target) if _cal_target else "?",
                    [(round(a, 2), round(b, 2)) for a, b in _corner_results])
        return
    _corner_bad = False
    nxs = [c[0] for c in _corner_results]
    nys = [c[1] for c in _corner_results]
    x0, x1 = min(nxs), max(nxs)
    y0, y1 = min(nys), max(nys)
    entry = {
        "in_x0": x0, "in_y0": y0, "in_x1": x1, "in_y1": y1,
        "method": "corners",
        "corners": {CORNER_LABELS[i]: list(_corner_results[i]) for i in range(4)},
    }
    # Save under THIS monitor's key, preserving other monitors' calibrations.
    try:
        with open(CAL_PATH) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    mons = data.get("monitors") if isinstance(data.get("monitors"), dict) else {}
    key = _mon_key(_cal_target) if _cal_target else "default"
    mons[key] = entry
    with open(CAL_PATH, "w") as f:
        json.dump({"monitors": mons}, f, indent=2)
    _corner_done = True
    x_span = x1 - x0
    y_span = y1 - y0
    log.warning("CORNER CALIBRATION DONE [monitor %s]  box=(%.3f,%.3f)-(%.3f,%.3f)  "
                "span=%.2fx%.2f  -> %s", key, x0, y0, x1, y1, x_span, y_span, CAL_PATH)
    if x_span < 0.30:
        log.warning("  NARROW X-SPAN (%.2f < 0.30) — horizontal reach is cramped. "
                    "Try a wider arm sweep when capturing LEFT/RIGHT corners, "
                    "or press 'r' to redo.", x_span)
    if y_span < 0.25:
        log.warning("  NARROW Y-SPAN (%.2f < 0.25) — vertical reach is cramped; "
                    "small hand movements will cause large cursor jumps. "
                    "Try raising/lowering hand more clearly for TOP/BOTTOM corners, "
                    "or press 'r' to redo.", y_span)


def _reset_corners() -> None:
    """Restart the corner-calibration sequence from TOP-LEFT ('r' key)."""
    global _corner_idx, _corner_results, _corner_anchor, _corner_anchor_t
    global _corner_hold, _corner_captured_t, _corner_progress, _corner_done, _corner_bad
    global _corner_jitter
    _corner_idx = 0
    _corner_results = []
    _corner_anchor = None
    _corner_anchor_t = 0.0
    _corner_hold = []
    _corner_captured_t = 0.0
    _corner_progress = 0.0
    _corner_done = False
    _corner_bad = False
    _corner_jitter = None
    log.warning("corner calibration RESET — point to TOP-LEFT and hold")


def _corner_step(cen) -> None:
    """Advance the point-to-corner capture state machine with one centroid.

    The user points at the current corner and HOLDS still; once the centroid
    stays within CORNER_RADIUS for CORNER_HOLD_S, the median of the held window
    is captured and we advance. A short CORNER_GAP_S flash follows each capture.
    """
    global _corner_idx, _corner_anchor, _corner_anchor_t, _corner_hold
    global _corner_captured_t, _corner_progress, _corner_jitter
    if _corner_done or cen is None:
        return
    now = time.monotonic()
    # Gap/flash window after a capture — let the user move to the next corner.
    if now - _corner_captured_t < CORNER_GAP_S:
        _corner_progress = 0.0
        _corner_anchor = None
        _corner_jitter = None
        return
    if _corner_anchor is None or math.dist(cen, _corner_anchor) > CORNER_RADIUS:
        _corner_anchor = cen
        _corner_anchor_t = now
        _corner_hold = [cen]
        _corner_progress = 0.0
        _corner_jitter = None
        return
    _corner_hold.append(cen)
    _corner_progress = min(1.0, (now - _corner_anchor_t) / CORNER_HOLD_S)
    # Live jitter feedback: std dev of accumulated hold samples (updates every frame)
    if len(_corner_hold) >= 3:
        jx = float(np.std([c[0] for c in _corner_hold]))
        jy = float(np.std([c[1] for c in _corner_hold]))
        _corner_jitter = (jx, jy)
    if _corner_progress >= 1.0:
        xs = [c[0] for c in _corner_hold]
        ys = [c[1] for c in _corner_hold]
        med = (float(np.median(xs)), float(np.median(ys)))
        jitter_max = max(float(np.std(xs)), float(np.std(ys)))
        log.warning("captured %s corner -> (%.3f, %.3f)  jitter_max=%.4f  [%d/4]",
                    CORNER_LABELS[_corner_idx], med[0], med[1], jitter_max, _corner_idx + 1)
        if jitter_max > 0.02:
            log.warning("  HIGH JITTER (%.4f > 0.02) — consider pressing 'r' and "
                        "recapturing this corner with a steadier hold", jitter_max)
        _corner_results.append(med)
        _corner_idx += 1
        _corner_captured_t = now
        _corner_anchor = None
        _corner_hold = []
        _corner_progress = 0.0
        _corner_jitter = None
        if _corner_idx >= 4:
            _finalize_corners()


def _render(bgr, landmarks, depth):
    """Draw landmarks + last gesture on color, build depth heatmap, show window."""
    h, w = bgr.shape[:2]
    if landmarks:
        pts = [(int(x * w), int(y * h)) for (x, y) in landmarks]
        for a, b in _BONES:
            cv2.line(bgr, pts[a], pts[b], (0, 255, 0), 2)
        for i, p in enumerate(pts):
            cv2.circle(bgr, p, 4, (0, 200, 255), -1)
        for tip, col in ((_INDEX_TIP, (0, 0, 255)), (_MIDDLE_TIP, (255, 0, 255)),
                         (_THUMB_TIP, (255, 255, 0))):
            cv2.circle(bgr, pts[tip], 8, col, 2)
    # depth heatmap
    if depth is not None:
        d = np.nan_to_num(depth, nan=0.0)
        norm = np.clip(d / 1.5, 0, 1)
        heat = cv2.applyColorMap((norm * 255).astype(np.uint8), cv2.COLORMAP_JET)
        heat[d <= 0] = (0, 0, 0)
        heat = cv2.resize(heat, (w, h), interpolation=cv2.INTER_NEAREST)
    else:
        heat = np.zeros((h, w, 3), dtype=np.uint8)
    # Pointer mode: draw the dwell ring at the fingertip centroid + click flash.
    if POINTER and _last_centroid is not None:
        cxp, cyp = int(_last_centroid[0] * w), int(_last_centroid[1] * h)
        pinched = THUMB_CLICK and _last_ratio is not None and _last_ratio < ThumbClickConfig().close_ratio
        cv2.circle(bgr, (cxp, cyp), 26, (0, 255, 0) if pinched else (200, 200, 200), 2)
        if _last_dwell > 0:
            cv2.ellipse(bgr, (cxp, cyp), (26, 26), -90, 0, int(360 * _last_dwell),
                        (0, 255, 0), 4)
        if THUMB_CLICK and _last_ratio is not None:
            held = _last_ratio < FREEZE_RATIO
            txt = f"pinch:{_last_ratio:.2f}" + ("  HOLD" if held else "")
            cv2.putText(bgr, txt, (cxp - 40, cyp + 48), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                        (0, 255, 0) if pinched else ((0, 165, 255) if held else (200, 200, 0)), 2)
        if (time.time() - _last_click_t) < 0.6:
            cv2.putText(bgr, "CLICK", (cxp - 36, cyp - 36),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 255, 0), 3)
    # ROM calibration overlay: show the accumulating range box + countdown.
    if CAL_ROM:
        if _rom_samples:
            xs = [s[0] for s in _rom_samples]; ys = [s[1] for s in _rom_samples]
            x0, y0 = int(min(xs) * w), int(min(ys) * h)
            x1, y1 = int(max(xs) * w), int(max(ys) * h)
            cv2.rectangle(bgr, (x0, y0), (x1, y1), (0, 255, 255), 2)
        if _rom_done:
            msg = "CALIBRATION DONE - saved"
        elif _rom_start is None:
            msg = "ROM: move hand into view to begin"
        else:
            left = max(0, ROM_SECONDS - (time.monotonic() - _rom_start))
            msg = f"ROM: move through your comfy range  {left:0.0f}s  n={len(_rom_samples)}"
        cv2.putText(bgr, msg, (10, h - 18), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
    # Corner calibration overlay: prompt for the current corner + a hold ring.
    if CAL_CORNERS:
        # Which monitor we're calibrating (and is it portrait?).
        mtag = ""
        if _cal_target is not None:
            orient = "portrait" if _cal_target[2] < _cal_target[3] else "landscape"
            mtag = (f"  [monitor {_cal_mon_idx + 1}/{len(_monitors)} "
                    f"{_cal_target[2]}x{_cal_target[3]} {orient}; 'm'=next]")
        if _corner_bad:
            msg = ("CORNERS CROSSED - press 'r' and recapture with CLEARER corner "
                   "separation" + mtag)
            col = (0, 0, 255)
        elif _corner_done:
            msg = "DONE - saved." + mtag + "  'm' to calibrate the next monitor."
            col = (0, 255, 0)
        elif (time.monotonic() - _corner_captured_t) < CORNER_GAP_S and _corner_results:
            msg = f"CAPTURED {CORNER_LABELS[_corner_idx - 1]}!   ({_corner_idx}/4)" + mtag
            col = (0, 255, 0)
        else:
            msg = (f"Point to the {CORNER_LABELS[_corner_idx]} corner and HOLD "
                   f"({_corner_idx}/4)" + mtag)
            col = (0, 255, 255)
        cv2.putText(bgr, msg, (10, h - 18), cv2.FONT_HERSHEY_SIMPLEX, 0.62, col, 2)
        if not _corner_done and _last_centroid is not None:
            cxp, cyp = int(_last_centroid[0] * w), int(_last_centroid[1] * h)
            jit_col = (0, 255, 255)  # yellow-cyan default
            if _corner_jitter is not None:
                jmax = max(_corner_jitter)
                jit_col = (0, 165, 255) if jmax > 0.02 else (0, 255, 0)  # orange=bad, green=good
                cv2.putText(bgr,
                            f"jitter x:{_corner_jitter[0]:.4f} y:{_corner_jitter[1]:.4f}"
                            + ("  HOLD STILLER" if jmax > 0.02 else "  stable"),
                            (cxp - 80, cyp + 60), cv2.FONT_HERSHEY_SIMPLEX, 0.5, jit_col, 1)
            cv2.circle(bgr, (cxp, cyp), 30, jit_col, 2)
            if _corner_progress > 0:
                cv2.ellipse(bgr, (cxp, cyp), (30, 30), -90, 0,
                            int(360 * _corner_progress), (0, 255, 0), 5)
    # banners
    det = "HAND DETECTED" if landmarks else "no hand"
    cv2.putText(bgr, det, (10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.8,
                (0, 255, 0) if landmarks else (0, 0, 255), 2)
    # Pointer mode: live smoothing readout + key hints for tuning.
    if POINTER and _pointer is not None:
        c = _pointer.cfg
        cv2.putText(bgr, f"min_cutoff {c.min_cutoff:.2f} [/]   beta {c.beta:.3f} ;/'"
                    f"   median {c.median_window} ,/.",
                    (10, 56), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 0), 2)
    if not POINTER and _last_gesture and (time.time() - _last_gesture_t) < 1.5:
        cv2.putText(bgr, _last_gesture, (10, h - 18), cv2.FONT_HERSHEY_SIMPLEX, 0.9,
                    (0, 255, 255), 2)
    cv2.putText(heat, "DEPTH", (10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
    cv2.imshow(WIN, np.hstack([bgr, heat]))
    key = cv2.waitKey(1) & 0xFF
    if key == ord("r") and CAL_CORNERS:
        _reset_corners()
    if key == ord("m"):
        if POINTER:
            _cycle_monitor()
        elif CAL_CORNERS:
            _cycle_cal_monitor()
    if POINTER and _pointer is not None and key != 255:
        _tune_smoothing(key)
    return key == ord("q")


def _tune_smoothing(key: int) -> None:
    """Live smoothing tuning in pointer mode (keys shown in the overlay):
      [ / ]  min_cutoff -/+ (lower = smoother at rest, more lag)
      ; / '  beta -/+       (higher = less lag on fast motion)
      , / .  median -/+     (larger = fewer 1-frame jumps, more lag)
    """
    c = _pointer.cfg
    if key == ord("["):
        _pointer.set_smoothing(min_cutoff=c.min_cutoff - 0.1)
    elif key == ord("]"):
        _pointer.set_smoothing(min_cutoff=c.min_cutoff + 0.1)
    elif key == ord(";"):
        _pointer.set_smoothing(beta=max(0.0, c.beta - 0.02))
    elif key == ord("'"):
        _pointer.set_smoothing(beta=c.beta + 0.02)
    elif key == ord(","):
        _pointer.set_smoothing(median_window=c.median_window - 1)
    elif key == ord("."):
        _pointer.set_smoothing(median_window=c.median_window + 1)
    else:
        return
    log.warning("SMOOTHING  min_cutoff=%.2f  beta=%.3f  median=%d",
                c.min_cutoff, c.beta, c.median_window)


def _make_app() -> web.Application:
    global _pointer, _thumb, _monitors, _active_mon, _cal_target, _cal_mon_idx
    lidar = LiDARReceiver()
    gesture = GestureProcessor()
    gesture.set_lidar(lidar)
    if POINTER:
        import ctypes
        _u = ctypes.windll.user32

        def _move_cursor(x, y):
            _u.SetCursorPos(int(x), int(y))

        # Per-monitor mapping: the hand controls ONE monitor at a time (press
        # 'm' to switch). One calibration drives whichever monitor is active —
        # comfortable single-screen reach instead of the cramped whole-desktop
        # stretch. SetCursorPos uses virtual coords matching UIA element bounds.
        _monitors = _enumerate_monitors()
        if not _monitors:
            _monitors = [(_u.GetSystemMetrics(76), _u.GetSystemMetrics(77),
                          _u.GetSystemMetrics(78), _u.GetSystemMetrics(79))]
        # Start on the monitor that contains the origin (primary), else first.
        _active_mon = next((i for i, m in enumerate(_monitors)
                            if m[0] == 0 and m[1] == 0), 0)
        vx, vy, sw, sh = _monitors[_active_mon]

        pcfg = load_pointer_config(_mon_key(_monitors[_active_mon]))
        if NO_DWELL or THUMB_CLICK:
            pcfg.dwell_enabled = False   # thumb-pinch replaces dwell
        # Live smoothing overrides: lower --min-cutoff = smoother at rest;
        # --median N = median pre-filter window (reject 1-frame jumps); --beta
        # raises responsiveness during fast motion.
        pcfg.min_cutoff = _argval("--min-cutoff", pcfg.min_cutoff)
        pcfg.beta = _argval("--beta", pcfg.beta)
        pcfg.median_window = int(_argval("--median", pcfg.median_window))
        # Overshoot margin makes corners forgiving (default 6%); --overshoot N.
        pcfg.overshoot = _argval("--overshoot", 0.06)
        log.info("pointer smoothing: min_cutoff=%.3f beta=%.3f median_window=%d overshoot=%.2f",
                 pcfg.min_cutoff, pcfg.beta, pcfg.median_window, pcfg.overshoot)

        # Cursor gravity: stickiness near clickable targets (min/max/close, etc).
        # ON by default in pointer mode; --no-gravity disables. Reads the
        # foreground window's clickable elements from ClickableTargetCache.
        gravity_provider = None
        global _tcache
        if "--no-gravity" not in sys.argv:
            pcfg.gravity_radius_px = _argval("--gravity-radius", pcfg.gravity_radius_px)
            pcfg.gravity_max_pull_px = _argval("--gravity-pull", pcfg.gravity_max_pull_px)
            try:
                import ctypes
                from ctypes import wintypes
                from desktop.target_cache import ClickableTargetCache

                _u32 = ctypes.windll.user32
                # Explicit restypes: HWND is pointer-sized; without these ctypes
                # truncates the return to 32-bit and corrupts the handle on x64.
                _u32.WindowFromPoint.argtypes = [wintypes.POINT]
                _u32.WindowFromPoint.restype = wintypes.HWND
                _u32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
                _u32.GetAncestor.restype = wintypes.HWND
                _u32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
                _u32.GetCursorPos.restype = wintypes.BOOL

                def _window_under_cursor() -> int:
                    # Top-level window under the cursor (GA_ROOT) so the whole
                    # window — including its title-bar min/max/close — is
                    # enumerated, not just the deepest child control.
                    try:
                        pt = wintypes.POINT()
                        if not _u32.GetCursorPos(ctypes.byref(pt)):
                            return 0
                        h = _u32.WindowFromPoint(pt)
                        if not h:
                            return 0
                        root = _u32.GetAncestor(h, 2)  # GA_ROOT
                        return int(root or h)
                    except Exception:
                        return 0

                _tcache = ClickableTargetCache(hwnd_source=_window_under_cursor)
                _tcache.start()

                # Scope gravity to COMPACT controls only (buttons, caption
                # min/max/close, toolbar icons) — skip large regions and long
                # hyperlinks so it doesn't grab every link on a web page.
                _grav_max_dim = _argval("--gravity-max-size", 120.0)
                _grav_dbg = {"t": 0.0}

                def _grav(x, y, r):
                    tg = _tcache.nearest(int(x), int(y), r, max_dim=_grav_max_dim)
                    now = time.monotonic()
                    if tg is not None and now - _grav_dbg["t"] > 0.4:
                        _grav_dbg["t"] = now
                        cx, cy = tg.center()
                        log.info("GRAVITY cursor=(%d,%d) -> target=%r center=(%d,%d) "
                                 "bounds=%s dist=%.0f", int(x), int(y), tg.name, cx, cy,
                                 tg.bounds, tg.distance_to(int(x), int(y)))
                    return tg.center() if tg else None

                gravity_provider = _grav
                log.info("cursor gravity ON: radius=%.0fpx pull=%.0fpx max-size=%.0fpx "
                         "(window under cursor; --no-gravity to disable)",
                         pcfg.gravity_radius_px, pcfg.gravity_max_pull_px, _grav_max_dim)
            except Exception as exc:  # noqa: BLE001
                log.warning("cursor gravity unavailable (%s) — continuing without", exc)
        _pointer = HandPointer(sw, sh, pcfg, move_cb=_move_cursor,
                               gravity_provider=gravity_provider, origin=(vx, vy))
        log.info("pointer on monitor %d/%d origin=(%d,%d) size=%dx%d "
                 "(press 'm' to switch monitor)", _active_mon + 1, len(_monitors),
                 vx, vy, sw, sh)
        if THUMB_CLICK:
            _thumb = ThumbClick(ThumbClickConfig())
            log.info("THUMB-CLICK enabled (pinch thumb to index/middle -> click, logged not executed)")
        log.info("POINTER test mode: moving real cursor (clicks logged, NOT executed). "
                 "screen=%dx%d", sw, sh)

    if CAL_CORNERS:
        # Calibrate one monitor at a time; default to the primary, 'm' cycles.
        _monitors = _enumerate_monitors()
        if not _monitors:
            import ctypes as _ct
            _u2 = _ct.windll.user32
            _monitors = [(_u2.GetSystemMetrics(76), _u2.GetSystemMetrics(77),
                          _u2.GetSystemMetrics(78), _u2.GetSystemMetrics(79))]
        _cal_mon_idx = next((i for i, m in enumerate(_monitors)
                             if m[0] == 0 and m[1] == 0), 0)
        _cal_target = _monitors[_cal_mon_idx]
        log.info("calibrating monitor %d/%d (%dx%d); press 'm' to calibrate another",
                 _cal_mon_idx + 1, len(_monitors), _cal_target[2], _cal_target[3])

    log.info("gesture available=%s  lidar available=%s  show=%s  pointer=%s",
             gesture._available, lidar.get_status().get("available"), SHOW, POINTER)

    async def ws_handler(request: web.Request) -> web.WebSocketResponse:
        global _last_status, _last_gesture, _last_gesture_t
        global _last_click_t, _last_centroid, _last_dwell, _rom_start, _last_ratio
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=0)
        await ws.prepare(request)
        log.info("sidecar connected: %s", request.remote)
        while True:
            msg = await ws.receive()
            if msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.CLOSING,
                            web.WSMsgType.CLOSED, web.WSMsgType.ERROR):
                break
            if msg.type != web.WSMsgType.TEXT:
                continue
            # Drain the backlog: MediaPipe (~14fps) is slower than the 30fps
            # stream, so process only the FRESHEST frame and drop stale ones —
            # otherwise the queue grows and the cursor lags further every second.
            dropped = 0
            while True:
                try:
                    nxt = await ws.receive(timeout=0.0005)
                except (asyncio.TimeoutError, Exception):
                    break
                if nxt.type != web.WSMsgType.TEXT:
                    break
                msg = nxt
                dropped += 1
            if dropped:
                stats["dropped"] = stats.get("dropped", 0) + dropped
            try:
                m = json.loads(msg.data)
            except Exception:
                continue
            t = m.get("type")
            if t == "depth_frame":
                lidar.on_depth_frame(m)
                stats["depth"] += 1
            elif t == "camera_frame":
                stats["camera"] += 1
                cmd = gesture.on_camera_frame(m)
                lms = gesture.latest_landmarks
                if lms:
                    stats["hands"] += 1
                    ix, iy = lms[_INDEX_TIP]
                    depth = lidar.get_depth_at(ix, iy)
                    dtxt = f"{depth:.3f} m" if depth is not None else "None"
                    now = time.time()
                    if now - _last_status > 0.33:
                        ncx, ncy = _nearest_centroid(lidar.latest_depth)
                        nc = f"({ncx:.2f},{ncy:.2f})" if ncx is not None else "None"
                        log.info("HAND idx_tip=(%.2f,%.2f) depth=%s | nearest-centroid=%s | pinch=%s",
                                 ix, iy, dtxt, nc,
                                 f"{_last_ratio:.2f}" if _last_ratio is not None else "?")
                        _last_status = now
                if cmd is not None and not POINTER:
                    stats["gestures"] += 1
                    g = (cmd.params or {}).get("gesture") if hasattr(cmd, "params") else None
                    label = g or getattr(cmd, "text", "?")
                    _last_gesture = f"{label} -> {getattr(cmd, 'action', '?')}"
                    _last_gesture_t = time.time()
                    log.warning(">>> GESTURE FIRED: %s  action=%s  (NOT executed)",
                                label, getattr(cmd, "action", "?"))

                # Range-of-motion calibration: record centroids, don't move anything.
                if CAL_ROM and not _rom_done:
                    if lms:
                        cen = fingertip_centroid(lms)
                        if cen is not None:
                            if _rom_start is None:
                                _rom_start = time.monotonic()
                                log.info("ROM capture started — move your hand through your "
                                         "comfortable range for %.0fs", ROM_SECONDS)
                            _rom_samples.append(cen)
                            _last_centroid = cen

                # Corner calibration: point at each monitor corner and HOLD.
                if CAL_CORNERS and not _corner_done:
                    if lms:
                        cen = fingertip_centroid(lms)
                        if cen is not None:
                            _last_centroid = cen
                            _corner_step(cen)
                    else:
                        _last_centroid = None

                # Relaxed Pointer: fingertip centroid -> absolute cursor + thumb/dwell click.
                if POINTER and _pointer is not None:
                    if lms:
                        # Thumb-pinch detection FIRST so we can FREEZE the cursor while
                        # pinching (the pinch curls the fingers, which would otherwise
                        # drag the fingertip-driven cursor off the target).
                        tev = {"ratio": None, "click": False}
                        hold = False
                        if _thumb is not None:
                            tev = _thumb.update(lms)
                            _last_ratio = tev["ratio"]
                            hold = tev["ratio"] is not None and tev["ratio"] < FREEZE_RATIO
                        cen = fingertip_centroid(lms)
                        if cen is not None:
                            _last_centroid = cen
                            ev = _pointer.update(cen[0], cen[1], hold=hold)
                            _last_dwell = ev["dwell_progress"]
                            if ev["click"]:   # dwell (only if dwell_enabled)
                                _last_click_t = time.time()
                                stats["gestures"] += 1
                                if EXECUTE:
                                    import pyautogui; pyautogui.click()
                        if tev["click"]:
                            _last_click_t = time.time()
                            stats["gestures"] += 1
                            if EXECUTE:
                                import pyautogui; pyautogui.click()
                            log.warning(">>> THUMB CLICK  ratio=%.2f  %s",
                                        tev["ratio"] if tev["ratio"] else -1,
                                        "(EXECUTED)" if EXECUTE else "(not executed)")
                    else:
                        _pointer.reset()
                        if _thumb is not None:
                            _thumb.reset()
                        _last_centroid = None
                        _last_dwell = 0.0
                        _last_ratio = None
                if SHOW:
                    try:
                        raw = base64.b64decode(m.get("image_b64", ""))
                        bgr = cv2.imdecode(np.frombuffer(raw, dtype="u1"), cv2.IMREAD_COLOR)
                        if bgr is not None and _render(bgr, lms, lidar.latest_depth):
                            await ws.close()
                    except Exception as exc:  # noqa: BLE001
                        log.debug("render error: %s", exc)
        log.info("sidecar disconnected")
        return ws

    async def _status_loop(_app):
        while True:
            await asyncio.sleep(5.0)
            tcnt = _tcache.count() if _tcache is not None else 0
            log.info("STATUS  camera=%d depth=%d  hand-frames=%d  gestures=%d  "
                     "dropped=%d  snap-targets=%d",
                     stats["camera"], stats["depth"], stats["hands"], stats["gestures"],
                     stats.get("dropped", 0), tcnt)

    async def _rom_finalize_loop(_app):
        global _rom_done, _rom_box
        while True:
            await asyncio.sleep(0.5)
            if _rom_done or _rom_start is None:
                continue
            if (time.monotonic() - _rom_start) >= ROM_SECONDS and len(_rom_samples) >= 30:
                xs = np.array([s[0] for s in _rom_samples])
                ys = np.array([s[1] for s in _rom_samples])
                # 2nd/98th percentile -> ignore stray outliers but keep real reach.
                x0, x1 = float(np.percentile(xs, 2)), float(np.percentile(xs, 98))
                y0, y1 = float(np.percentile(ys, 2)), float(np.percentile(ys, 98))
                _rom_box = (x0, y0, x1, y1)
                with open(CAL_PATH, "w") as f:
                    json.dump({"in_x0": x0, "in_y0": y0, "in_x1": x1, "in_y1": y1,
                               "samples": len(_rom_samples)}, f, indent=2)
                _rom_done = True
                log.warning("ROM CALIBRATION DONE  box=(%.3f,%.3f)-(%.3f,%.3f)  "
                            "span=%.2fx%.2f  samples=%d  -> %s",
                            x0, y0, x1, y1, x1 - x0, y1 - y0, len(_rom_samples), CAL_PATH)

    async def _on_start(a):
        a["status_task"] = asyncio.create_task(_status_loop(a))
        if CAL_ROM:
            a["rom_task"] = asyncio.create_task(_rom_finalize_loop(a))

    async def _on_cleanup(a):
        a["status_task"].cancel()
        if "rom_task" in a:
            a["rom_task"].cancel()
        if _tcache is not None:
            try:
                _tcache.stop()
            except Exception:
                pass

    app = web.Application()
    app.router.add_get("/ws", ws_handler)
    app.on_startup.append(_on_start)
    app.on_cleanup.append(_on_cleanup)
    return app


_VALUE_FLAGS = {"--hold", "--gap", "--min-cutoff", "--beta", "--median"}


def _parse_port(argv: list, default: int = 8765) -> int:
    """First positional digit-only arg is the port. Skips values that belong to
    value-flags (e.g. the 5 in `--median 5`) so they're never mistaken for it."""
    skip_next = False
    for a in argv:
        if skip_next:
            skip_next = False
            continue
        if a in _VALUE_FLAGS:
            skip_next = True
            continue
        if a.isdigit():
            return int(a)
    return default


if __name__ == "__main__":
    port = _parse_port(sys.argv[1:])
    log.info("validator listening on ws://0.0.0.0:%d/ws  (start the sidecar now)  show=%s", port, SHOW)
    web.run_app(_make_app(), host="0.0.0.0", port=port, print=None)
