"""Headless RealSense L515 gesture-pipeline validator (SAFE — executes nothing).

Runs the REAL GestureProcessor + LiDARReceiver behind a minimal aiohttp WS
server at /ws (mirroring core/ipad_bridge.py), so the realsense_publisher.py
sidecar can stream into it exactly as it would into the real app. It logs:

  * frames received (camera / depth) and depth freshness
  * whether a hand is detected, and the index-fingertip depth in metres
    -> this is the rs.align() correctness check (the #1 integration risk)
  * any gesture Command the pipeline would have fired (but does NOT execute it)

Usage (Python 3.14 main env):
    python scripts/validate_realsense.py
Then in another terminal (Python 3.10 venv):
    start_realsense.bat
Hold a hand 0.4-0.8 m in front of the camera and try a peace sign / swipe.
Ctrl-C to stop. Nothing touches the desktop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from aiohttp import web

from sensors.gesture_processor import GestureProcessor
from sensors.lidar_receiver import LiDARReceiver


def _nearest_centroid(depth):
    """Return normalized (nx, ny) of the centroid of the nearest depth region.

    Used to cross-check alignment: when the hand is the closest object, this
    should land on the hand — i.e. ~= the MediaPipe hand landmark position.
    Returns None if no valid depth.
    """
    if depth is None:
        return None, None
    valid = depth[~np.isnan(depth)]
    if valid.size == 0:
        return None, None
    dmin = float(np.nanmin(depth))
    mask = depth <= (dmin + 0.10)  # within 10 cm of nearest
    ys, xs = np.where(mask)
    if xs.size == 0:
        return None, None
    h, w = depth.shape
    return float(xs.mean() / (w - 1)), float(ys.mean() / (h - 1))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("validate_realsense")

_INDEX_TIP = 8  # MediaPipe landmark index for the index fingertip

stats = {"camera": 0, "depth": 0, "hands": 0, "gestures": 0}
_last_status = 0.0


def _make_app() -> web.Application:
    lidar = LiDARReceiver()
    gesture = GestureProcessor()
    gesture.set_lidar(lidar)
    log.info("gesture available=%s  lidar available=%s",
             gesture._available, lidar.get_status().get("available"))

    async def ws_handler(request: web.Request) -> web.WebSocketResponse:
        global _last_status
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=0)
        await ws.prepare(request)
        log.info("sidecar connected: %s", request.remote)
        async for msg in ws:
            if msg.type != web.WSMsgType.TEXT:
                continue
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
                    fresh = lidar.is_fresh(0.5)
                    dtxt = f"{depth:.3f} m" if depth is not None else "None"
                    # throttle per-hand logging to ~3/s
                    now = time.time()
                    if now - _last_status > 0.33:
                        ncx, ncy = _nearest_centroid(lidar.latest_depth)
                        nc = f"({ncx:.2f},{ncy:.2f})" if ncx is not None else "None"
                        log.info("HAND idx_tip=(%.2f,%.2f) depth=%s | nearest-depth-centroid=%s "
                                 "(should match idx_tip/palm if aligned)", ix, iy, dtxt, nc)
                        _last_status = now
                if cmd is not None:
                    stats["gestures"] += 1
                    g = (cmd.params or {}).get("gesture") if hasattr(cmd, "params") else None
                    log.warning(">>> GESTURE FIRED: %s  action=%s  (NOT executed)",
                                g or getattr(cmd, "text", "?"), getattr(cmd, "action", "?"))
        log.info("sidecar disconnected")
        return ws

    async def _status_loop(_app):
        while True:
            await asyncio.sleep(5.0)
            log.info("STATUS  camera=%d depth=%d  hand-frames=%d  gestures=%d",
                     stats["camera"], stats["depth"], stats["hands"], stats["gestures"])

    app = web.Application()
    app.router.add_get("/ws", ws_handler)

    async def _on_start(a):
        a["status_task"] = asyncio.create_task(_status_loop(a))

    async def _on_cleanup(a):
        a["status_task"].cancel()

    app.on_startup.append(_on_start)
    app.on_cleanup.append(_on_cleanup)
    return app


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    log.info("validator listening on ws://0.0.0.0:%d/ws  (start the sidecar now)", port)
    web.run_app(_make_app(), host="0.0.0.0", port=port, print=None)
