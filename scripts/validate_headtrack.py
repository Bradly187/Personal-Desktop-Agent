"""Headless/GUI RealSense L515 head-pointer validator + corner calibrator.

Runs the REAL FaceTracker + LiDARReceiver behind a minimal aiohttp WS server at
/ws (mirroring core/ipad_bridge.py), so the realsense_publisher.py sidecar can
stream into it exactly as it would into the real app. Three modes:

  (default)            log head pose + the cursor it WOULD drive (moves nothing).
  --pointer            move the REAL cursor via SetCursorPos (no click — clicking
                       is the voice "click" command in the full app).
  --calibrate-corners  guided: look at screen CENTER then each CORNER and hold
                       your head still; captures the gnomonic pointing feature at
                       each and writes head_pointer_calibration.json.

Add --show to open a live camera window with on-screen instructions, a hold
progress bar, and a green CAPTURED flash per point (recommended for calibration).
--no-mirror shows the true (un-mirrored) camera view.

Usage (Python 3.14 main env):
    python scripts/validate_headtrack.py [PORT] --calibrate-corners --show
Then start the sidecar (Python 3.10 venv):  start_realsense.bat --port PORT
Ctrl-C (or ESC / q / close the window with --show) to stop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import sys
import time


def _set_dpi_aware() -> None:
    """Per-monitor DPI aware so SetCursorPos coords match physical pixels."""
    import ctypes
    try:
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:
            return
    except Exception:
        pass
    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


if sys.platform == "win32":
    _set_dpi_aware()

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
CAL_PATH = os.path.join(REPO_ROOT, "head_pointer_calibration.json")

import numpy as np
import cv2
from aiohttp import web

from sensors.face_tracker import FaceTracker
from sensors.lidar_receiver import LiDARReceiver
from sensors.head_pointer import HeadPointer, HeadPointerConfig, head_angles

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("validate_headtrack")

POINTER = "--pointer" in sys.argv
CALIBRATE = "--calibrate-corners" in sys.argv
CALIBRATE_BOX = "--calibrate-box" in sys.argv
CALIBRATING = CALIBRATE or CALIBRATE_BOX
SHOW = "--show" in sys.argv
MIRROR = "--no-mirror" not in sys.argv
CORNER_LABELS = ("TOP-LEFT", "TOP-RIGHT", "BOTTOM-RIGHT", "BOTTOM-LEFT")
HOLD_S = 2.0          # hold head still this long to capture a point
STILL_RADIUS = 0.05   # gnomonic-feature stillness radius for the hold
GAP_S = 1.2           # settle gap after each capture
WIN = "Head-Pointer Calibration"

face = FaceTracker()
lidar = LiDARReceiver()
stats = {"camera": 0, "depth": 0, "faces": 0}

# Screen geometry (primary monitor).
try:
    import pyautogui
    SW, SH = pyautogui.size()
except Exception:
    SW, SH = 1920, 1080
MON_KEY = f"{SW}x{SH}@0,0"

import ctypes
_U32 = ctypes.windll.user32 if sys.platform == "win32" else None


def _move_cursor(x, y):
    if _U32 is not None:
        _U32.SetCursorPos(int(x), int(y))


# Latest annotated frame for the GUI thread to display (set by the WS handler).
_display = {"frame": None}


# --------------------------------------------------------------------------- #
# Calibration state machine
# --------------------------------------------------------------------------- #

class _Calibrator:
    """Capture the gnomonic pointing feature at each target pose (hold-still →
    capture), then write head_pointer_calibration.json. Subclasses define the
    target SEQ and how the captured poses become a calibration entry."""

    SEQ: tuple = ()

    def __init__(self):
        self.i = 0
        self.captured: dict[str, tuple] = {}
        self.nose_at: dict[str, tuple] = {}
        self._anchor = None
        self._anchor_t = 0.0
        self._next_at = 0.0
        self.just_captured = None       # (label, time) — drives the GUI flash
        self.written = False
        self._announce()

    @property
    def done(self) -> bool:
        return self.i >= len(self.SEQ)

    @property
    def target(self):
        return None if self.done else self.SEQ[self.i]

    def _announce(self):
        if not self.done:
            log.info("► Aim your head at: %s — HOLD still (%.0fs)…",
                     self.SEQ[self.i], HOLD_S)

    def instruction(self) -> str:
        if self.done:
            return "CALIBRATION COMPLETE — run with --headtrack.  (ESC to close)"
        return f"Point your NOSE at the {self.target} of the screen — HOLD still"

    def hold_progress(self, now: float) -> float:
        if self.done or self._anchor is None or now < self._next_at:
            return 0.0
        return max(0.0, min(1.0, (now - self._anchor_t) / HOLD_S))

    def feed(self, feat: tuple, nose: tuple, now: float):
        if self.done or now < self._next_at:
            return
        if self._anchor is None or math.dist(feat, self._anchor) > STILL_RADIUS:
            self._anchor = feat
            self._anchor_t = now
            return
        if now - self._anchor_t >= HOLD_S:
            tgt = self.SEQ[self.i]
            self.captured[tgt] = feat
            self.nose_at[tgt] = nose
            self.just_captured = (tgt, now)
            log.info("  ✓ captured %s  feature=(%.3f, %.3f)", tgt, feat[0], feat[1])
            self.i += 1
            self._anchor = None
            self._next_at = now + GAP_S
            self._announce()
            if self.done:
                self._write()

    def _build_entry(self) -> dict:
        raise NotImplementedError

    def _write(self):
        entry = self._build_entry()
        data = {"monitors": {}}
        if os.path.exists(CAL_PATH):
            try:
                with open(CAL_PATH) as f:
                    data = json.load(f)
            except Exception:
                pass
        data.setdefault("monitors", {})[MON_KEY] = entry
        with open(CAL_PATH, "w") as f:
            json.dump(data, f, indent=2)
        self.written = True
        log.info("✔ Calibration written to %s for %s", CAL_PATH, MON_KEY)
        log.info("  Run the app with --headtrack to use it.")


class CornerCalibrator(_Calibrator):
    """4-corner perspective (homography) calibration."""
    SEQ = ("CENTER",) + CORNER_LABELS

    def _build_entry(self) -> dict:
        us = [self.captured[c][0] for c in CORNER_LABELS]
        vs = [self.captured[c][1] for c in CORNER_LABELS]
        return {
            "in_x0": min(us), "in_x1": max(us),
            "in_y0": min(vs), "in_y1": max(vs),
            "depth_comp": 0.15,
            "corners": {c: list(self.captured[c]) for c in CORNER_LABELS},
            "nose_ref": list(self.nose_at.get("CENTER", (0.5, 0.5))),
        }


class BoxCalibrator(_Calibrator):
    """Axis-extent box calibration — robust to head-pose coupling. CENTER is a
    hold-still capture (the reference); LEFT/RIGHT/UP/DOWN are SWEEP captures: for
    a fixed window the user moves as far as they can and the FURTHEST point (max
    displacement from CENTER on the relevant axis) is kept — no awkward holding at
    an extreme. Maps in_x0=u@LEFT, in_x1=u@RIGHT (sign auto-resolves orientation),
    same for y. NO corners → HeadPointer uses box mode."""
    SEQ = ("CENTER", "LEFT", "RIGHT", "UP", "DOWN")
    WINDOW_S = 5.0
    _LOCK_THRESH = 0.08    # displacement that locks the sweep direction
    _AXIS = {"LEFT": 0, "RIGHT": 0, "UP": 1, "DOWN": 1}

    def __init__(self):
        self._win_start = None
        self._best = None      # (displacement, feat, nose)
        self._sign = 0         # locked sweep direction on the active axis
        super().__init__()

    def instruction(self) -> str:
        if self.done:
            return "CALIBRATION COMPLETE — run with --headtrack.  (ESC to close)"
        if self.target == "CENTER":
            return "Face the CENTER of the screen — hold still"
        return f"Look as far {self.target} as you can — keep moving while the bar fills"

    def hold_progress(self, now: float) -> float:
        if self.done or now < self._next_at:
            return 0.0
        if self.target == "CENTER":
            return super().hold_progress(now)
        if self._win_start is None:
            return 0.0
        return max(0.0, min(1.0, (now - self._win_start) / self.WINDOW_S))

    def feed(self, feat: tuple, nose: tuple, now: float):
        if self.done or now < self._next_at:
            return
        if self.target == "CENTER":
            return super().feed(feat, nose, now)   # hold-still reference capture
        # Directional sweep: keep the furthest point reached over the window,
        # locked to the direction of the user's first decisive move so a
        # return-through-center overshoot on the weak (vertical) axis can't
        # capture the opposite extreme.
        if self._win_start is None:
            self._win_start = now
            self._best = None
            self._sign = 0
        axis = self._AXIS[self.target]
        center = self.captured["CENTER"]
        delta = feat[axis] - center[axis]
        if self._sign == 0 and abs(delta) > self._LOCK_THRESH:
            self._sign = 1 if delta > 0 else -1
        if self._sign == 0 or delta * self._sign > 0:
            disp = abs(delta)
            if self._best is None or disp > self._best[0]:
                self._best = (disp, feat, nose)
        if now - self._win_start >= self.WINDOW_S and self._best is not None:
            tgt = self.target
            self.captured[tgt] = self._best[1]
            self.nose_at[tgt] = self._best[2]
            self.just_captured = (tgt, now)
            log.info("  ✓ captured %s  feature=(%.3f, %.3f)  reach=%.3f",
                     tgt, self._best[1][0], self._best[1][1], self._best[0])
            self.i += 1
            self._win_start = None
            self._best = None
            self._next_at = now + GAP_S
            self._announce()
            if self.done:
                self._write()

    def _build_entry(self) -> dict:
        center = self.captured.get("CENTER", (0.0, 0.0))
        return {
            "in_x0": self.captured["LEFT"][0],
            "in_x1": self.captured["RIGHT"][0],
            "in_y0": self.captured["UP"][1],
            "in_y1": self.captured["DOWN"][1],
            "center_u": center[0],
            "center_v": center[1],
            "invert_x": False, "invert_y": False,
            "depth_comp": 0.15,
            "nose_ref": list(self.nose_at.get("CENTER", (0.5, 0.5))),
        }


_cal = (BoxCalibrator() if CALIBRATE_BOX
        else CornerCalibrator() if CALIBRATE else None)


def _load_pointer() -> HeadPointer:
    cfg = HeadPointerConfig()
    if os.path.exists(CAL_PATH):
        try:
            with open(CAL_PATH) as f:
                e = json.load(f).get("monitors", {}).get(MON_KEY)
            if e:
                for k in ("in_x0", "in_y0", "in_x1", "in_y1", "depth_comp",
                          "center_u", "center_v"):
                    if k in e:
                        setattr(cfg, k, float(e[k]))
                if "invert_x" in e:
                    cfg.invert_x = bool(e["invert_x"])
                if "invert_y" in e:
                    cfg.invert_y = bool(e["invert_y"])
                cc = e.get("corners")
                if isinstance(cc, dict) and len(cc) == 4:
                    cfg.corners = [list(map(float, cc[k])) for k in CORNER_LABELS]
                nr = e.get("nose_ref")
                if isinstance(nr, (list, tuple)) and len(nr) == 2:
                    cfg.nose_ref = (float(nr[0]), float(nr[1]))
                log.info("Loaded calibration for %s (corners=%s)", MON_KEY, bool(cfg.corners))
        except Exception as exc:
            log.warning("calibration load failed: %s", exc)
    else:
        log.warning("No calibration yet — using the default box. "
                    "Run --calibrate-corners first for accurate aim.")
    return HeadPointer(SW, SH, cfg, move_cb=_move_cursor if POINTER else None)


_pointer = None if CALIBRATING else _load_pointer()
_last_log = 0.0


# --------------------------------------------------------------------------- #
# GUI overlay (--show)
# --------------------------------------------------------------------------- #

_GREEN = (80, 220, 80)
_CYAN = (230, 220, 60)
_WHITE = (245, 245, 245)
_RED = (60, 60, 230)
_DIM = (40, 40, 40)


def _annotate(bgr, hs, feat, depth, cursor, now):
    """Draw the calibration/pointer overlay onto a copy of the camera frame."""
    if MIRROR:
        bgr = cv2.flip(bgr, 1)
    img = bgr.copy()
    h, w = img.shape[:2]

    # Nose-tip marker.
    if hs is not None and hs.ok and hs.nose_nxy is not None:
        nx = (1.0 - hs.nose_nxy[0]) if MIRROR else hs.nose_nxy[0]
        cx, cy = int(nx * w), int(hs.nose_nxy[1] * h)
        cv2.circle(img, (cx, cy), 7, _CYAN, 2)
        cv2.drawMarker(img, (cx, cy), _CYAN, cv2.MARKER_CROSS, 16, 1)

    # Top instruction bar.
    cv2.rectangle(img, (0, 0), (w, 56), _DIM, -1)
    if _cal is not None:
        head = _cal.instruction()
        col = _GREEN if _cal.done else _WHITE
        cv2.putText(img, head, (14, 36), cv2.FONT_HERSHEY_SIMPLEX, 0.55, col, 2, cv2.LINE_AA)
    else:
        face_ok = "FACE OK" if (hs and hs.ok) else "no face"
        mode = "POINTER (cursor moving)" if POINTER else "OBSERVE"
        cv2.putText(img, f"{mode}   {face_ok}   depth={depth:.2f}m" if depth
                    else f"{mode}   {face_ok}   depth=n/a",
                    (14, 36), cv2.FONT_HERSHEY_SIMPLEX, 0.6, _WHITE, 2, cv2.LINE_AA)

    # Hold progress bar + capture flash (calibration only).
    if _cal is not None and not _cal.done:
        prog = _cal.hold_progress(now)
        bx0, by0, bx1, by1 = 14, h - 40, w - 14, h - 18
        cv2.rectangle(img, (bx0, by0), (bx1, by1), (90, 90, 90), 1)
        fillw = int((bx1 - bx0) * prog)
        cv2.rectangle(img, (bx0, by0), (bx0 + fillw, by1), _GREEN, -1)
        cv2.putText(img, f"hold {int(prog * 100)}%", (bx0 + 8, by1 - 6),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, _WHITE, 1, cv2.LINE_AA)

    # Corner checklist (top-right under the bar).
    if _cal is not None:
        y = 78
        for name in _cal.SEQ:
            ok = name in _cal.captured
            mark = "[x]" if ok else "[ ]"
            col = _GREEN if ok else (150, 150, 150)
            cv2.putText(img, f"{mark} {name}", (w - 210, y),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, col, 1, cv2.LINE_AA)
            y += 24

    # Green CAPTURED flash for ~1s after a capture.
    if _cal is not None and _cal.just_captured is not None:
        label, t0 = _cal.just_captured
        if now - t0 < 1.0:
            cv2.rectangle(img, (4, 4), (w - 4, h - 4), _GREEN, 6)
            txt = f"CAPTURED  {label}"
            (tw, th), _ = cv2.getTextSize(txt, cv2.FONT_HERSHEY_SIMPLEX, 1.1, 3)
            cv2.putText(img, txt, ((w - tw) // 2, h // 2),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.1, _GREEN, 3, cv2.LINE_AA)

    # Live feat readout (both modes — lets the user see their movement register).
    if feat is not None:
        txt = (f"feat=({feat[0]:.2f},{feat[1]:.2f})" if _cal is not None
               else f"feat=({feat[0]:.2f},{feat[1]:.2f})  cursor={cursor}")
        yy = 78 + 5 * 24 + 10 if _cal is not None else h - 18  # below the checklist
        cv2.putText(img, txt, (14, min(yy, h - 18)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, _CYAN, 1, cv2.LINE_AA)
    return img


async def _gui_loop(app):
    """Display the latest annotated frame. Runs on the event-loop (main) thread."""
    cv2.namedWindow(WIN, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(WIN, 1100, 620)
    try:
        while True:
            fr = _display["frame"]
            if fr is not None:
                cv2.imshow(WIN, fr)
            key = cv2.waitKey(1) & 0xFF
            closed = cv2.getWindowProperty(WIN, cv2.WND_PROP_VISIBLE) < 1
            if key in (27, ord("q")) or closed:
                log.info("window closed — exiting")
                cv2.destroyAllWindows()
                os._exit(0)
            await asyncio.sleep(0.012)
    except asyncio.CancelledError:
        cv2.destroyAllWindows()


# --------------------------------------------------------------------------- #
# WebSocket ingest
# --------------------------------------------------------------------------- #

async def _ws_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(max_msg_size=16 * 1024 * 1024)
    await ws.prepare(request)
    log.info("sidecar connected")
    global _last_log
    async for raw in ws:
        if raw.type != web.WSMsgType.TEXT:
            continue
        try:
            msg = json.loads(raw.data)
        except Exception:
            continue
        mt = msg.get("type")
        if mt == "depth_frame":
            lidar.on_depth_frame(msg)
            stats["depth"] += 1
            continue
        if mt != "camera_frame":
            continue
        stats["camera"] += 1

        # Decode once; reuse for both inference and display.
        bgr = None
        if SHOW:
            try:
                import base64
                buf = np.frombuffer(base64.b64decode(msg.get("image_b64", "")), dtype="u1")
                bgr = cv2.imdecode(buf, cv2.IMREAD_COLOR)
            except Exception:
                bgr = None

        hs = (face._process_frame(bgr) if (SHOW and bgr is not None)
              else face.on_camera_frame(msg))
        feat = head_angles(hs.forward) if (hs and hs.ok) else None
        depth = (lidar.get_depth_at(*hs.nose_nxy)
                 if (hs and hs.ok and hs.nose_nxy) else None)
        now = time.monotonic()
        cursor = (None, None)

        if hs and hs.ok and feat is not None:
            stats["faces"] += 1
            if _cal is not None:
                _cal.feed(feat, hs.nose_nxy or (0.5, 0.5), now)
            else:
                ev = _pointer.update(hs.forward, hs.nose_nxy, depth)
                cursor = (ev.get("x"), ev.get("y"))
        elif _cal is None and _pointer is not None:
            _pointer.reset()

        if SHOW and bgr is not None:
            _display["frame"] = _annotate(bgr, hs, feat, depth, cursor, now)

        if _cal is None and now - _last_log > 0.5:
            _last_log = now
            log.info("pose feat=%s depth=%s cursor=%s  cam=%d faces=%d",
                     f"({feat[0]:.3f},{feat[1]:.3f})" if feat else "n/a",
                     f"{depth:.2f}m" if depth else "n/a",
                     cursor, stats["camera"], stats["faces"])
    log.info("sidecar disconnected")
    return ws


def main() -> int:
    port = 8765
    for a in sys.argv[1:]:
        if a.isdigit():
            port = int(a)
    mode = ("CALIBRATE-BOX" if CALIBRATE_BOX else "CALIBRATE-CORNERS" if CALIBRATE
            else "POINTER (moves cursor)" if POINTER else "OBSERVE")
    log.info("validate_headtrack — mode=%s  show=%s  screen=%dx%d  port=%d",
             mode, SHOW, SW, SH, port)
    if not face._available:
        log.warning("FaceTracker unavailable (mediapipe/cv2/model missing). "
                    "See sensors/face_tracker.py docstring for the model download.")
    app = web.Application()
    app.router.add_get("/ws", _ws_handler)
    if SHOW:
        async def _start_gui(app):
            app["gui"] = asyncio.create_task(_gui_loop(app))
        async def _stop_gui(app):
            app["gui"].cancel()
        app.on_startup.append(_start_gui)
        app.on_cleanup.append(_stop_gui)
    try:
        web.run_app(app, host="127.0.0.1", port=port, print=None)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
