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
import os
import sys
import time

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
NO_DWELL = "--no-dwell" in sys.argv       # pointer test: movement only, no dwell click
THUMB_CLICK = "--thumb-click" in sys.argv # click when thumb tip rests near index/middle tips
EXECUTE = "--execute" in sys.argv         # actually perform real mouse clicks (else just log)
FREEZE_RATIO = 0.95                        # freeze cursor while thumb ratio is below this (pinching)
ROM_SECONDS = 15.0
WIN = "L515 validator  (color+landmarks | depth)   q to quit"

_pointer = None          # HandPointer instance (pointer mode)
_thumb = None            # ThumbClick instance (thumb-pinch click)
_last_click_t = 0.0      # for the on-screen CLICK flash
_last_centroid = None    # (nx, ny) for drawing the dwell ring
_last_dwell = 0.0
_last_ratio = None       # thumb-finger ratio for the overlay

# Range-of-motion calibration state
_rom_samples = []        # list of (nx, ny) fingertip centroids
_rom_start = None        # monotonic time of first sample
_rom_done = False
_rom_box = None          # (x0, y0, x1, y1) fitted box


def load_pointer_config() -> HandPointerConfig:
    """Load the fitted input box from CAL_PATH if present, else defaults."""
    cfg = HandPointerConfig()
    try:
        with open(CAL_PATH) as f:
            d = json.load(f)
        cfg.in_x0, cfg.in_y0 = float(d["in_x0"]), float(d["in_y0"])
        cfg.in_x1, cfg.in_y1 = float(d["in_x1"]), float(d["in_y1"])
        log.info("loaded ROM calibration box=(%.2f,%.2f)-(%.2f,%.2f) from %s",
                 cfg.in_x0, cfg.in_y0, cfg.in_x1, cfg.in_y1, CAL_PATH)
    except FileNotFoundError:
        log.info("no ROM calibration (%s) — using default box", CAL_PATH)
    except Exception as e:  # noqa: BLE001
        log.warning("failed to load ROM calibration: %s", e)
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
    # banners
    det = "HAND DETECTED" if landmarks else "no hand"
    cv2.putText(bgr, det, (10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.8,
                (0, 255, 0) if landmarks else (0, 0, 255), 2)
    if not POINTER and _last_gesture and (time.time() - _last_gesture_t) < 1.5:
        cv2.putText(bgr, _last_gesture, (10, h - 18), cv2.FONT_HERSHEY_SIMPLEX, 0.9,
                    (0, 255, 255), 2)
    cv2.putText(heat, "DEPTH", (10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
    cv2.imshow(WIN, np.hstack([bgr, heat]))
    return (cv2.waitKey(1) & 0xFF) == ord("q")


def _make_app() -> web.Application:
    global _pointer, _thumb
    lidar = LiDARReceiver()
    gesture = GestureProcessor()
    gesture.set_lidar(lidar)
    if POINTER:
        import pyautogui
        pyautogui.FAILSAFE = False  # HandPointer clamps; avoid corner-failsafe in the test
        sw, sh = pyautogui.size()
        pcfg = load_pointer_config()
        if NO_DWELL or THUMB_CLICK:
            pcfg.dwell_enabled = False   # thumb-pinch replaces dwell
        _pointer = HandPointer(sw, sh, pcfg, move_cb=pyautogui.moveTo)
        if THUMB_CLICK:
            _thumb = ThumbClick(ThumbClickConfig())
            log.info("THUMB-CLICK enabled (pinch thumb to index/middle -> click, logged not executed)")
        log.info("POINTER test mode: moving real cursor (clicks logged, NOT executed). "
                 "screen=%dx%d", sw, sh)
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
            log.info("STATUS  camera=%d depth=%d  hand-frames=%d  gestures=%d  dropped=%d",
                     stats["camera"], stats["depth"], stats["hands"], stats["gestures"],
                     stats.get("dropped", 0))

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

    app = web.Application()
    app.router.add_get("/ws", ws_handler)
    app.on_startup.append(_on_start)
    app.on_cleanup.append(_on_cleanup)
    return app


if __name__ == "__main__":
    port = 8765
    for a in sys.argv[1:]:
        if a.isdigit():
            port = int(a)
    log.info("validator listening on ws://0.0.0.0:%d/ws  (start the sidecar now)  show=%s", port, SHOW)
    web.run_app(_make_app(), host="0.0.0.0", port=port, print=None)
