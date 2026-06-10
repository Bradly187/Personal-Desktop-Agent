"""Live RealSense L515 preview — color + depth side by side (run in .venv-realsense).

A standalone OpenCV window for AIMING the camera. Left = color, right = depth
heatmap (blue=near .. red=far over 0-1.5 m; black = no/invalid depth). Top bar
shows valid-depth % and the depth at the center crosshair. Use it to position
the camera so your hand (held ~0.5 m away) shows up GREEN/BLUE in the depth pane.

    .venv-realsense\\Scripts\\python.exe scripts\\realsense_preview.py

Press q or ESC (or close the window) to quit.
"""
from __future__ import annotations
import numpy as np
import pyrealsense2 as rs
import cv2

WIN = "L515 preview  (left=color  right=depth)   q/ESC to quit"


def main() -> int:
    pipe = rs.pipeline()
    cfg = rs.config()
    # The L515 RGB camera has NO 640x480 mode — its smallest color profile is
    # 960x540. Requesting 640x480 color fails with "Couldn't resolve requests".
    cfg.enable_stream(rs.stream.color, 960, 540, rs.format.bgr8, 15)
    cfg.enable_stream(rs.stream.depth)
    align = rs.align(rs.stream.color)
    prof = pipe.start(cfg)
    scale = prof.get_device().first_depth_sensor().get_depth_scale()
    cv2.namedWindow(WIN, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(WIN, 1300, 500)
    try:
        while True:
            f = align.process(pipe.wait_for_frames())
            cframe = f.get_color_frame()
            dframe = f.get_depth_frame()
            if not cframe or not dframe:
                continue
            bgr = np.asanyarray(cframe.get_data())
            dm = np.asanyarray(dframe.get_data()).astype(np.float32) * scale
            # Camera mounted upside down -> rotate COLOR 180 for upright. On this
            # L515, align returns depth already 180 from color, so depth needs NO
            # rotation to end up upright AND aligned to color. (color=180, depth=0)
            bgr = cv2.rotate(bgr, cv2.ROTATE_180)

            norm = np.clip(dm / 1.5, 0, 1)
            heat = cv2.applyColorMap((norm * 255).astype(np.uint8), cv2.COLORMAP_JET)
            heat[dm <= 0] = (0, 0, 0)

            h, w = bgr.shape[:2]
            cx, cy = w // 2, h // 2
            for img in (bgr, heat):
                cv2.drawMarker(img, (cx, cy), (0, 255, 255), cv2.MARKER_CROSS, 24, 2)

            valid = dm[dm > 0]
            vpct = 100.0 * valid.size / dm.size
            cd = dm[cy, cx]
            ctxt = f"{cd:.2f} m" if cd > 0 else "INVALID(too close/far)"
            bar = np.zeros((40, w * 2, 3), dtype=np.uint8)
            cv2.putText(bar, f"valid depth: {vpct:4.1f}%   center: {ctxt}   "
                        f"(aim so hand ~0.5m shows green; hand should NOT fill frame)",
                        (10, 27), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)

            combo = np.hstack([bgr, heat])
            out = np.vstack([bar, combo])
            cv2.imshow(WIN, out)

            k = cv2.waitKey(1) & 0xFF
            if k in (ord("q"), 27):
                break
            if cv2.getWindowProperty(WIN, cv2.WND_PROP_VISIBLE) < 1:
                break
    finally:
        pipe.stop()
        cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
