"""realsense_publisher.py — Intel RealSense L515 capture sidecar.

Why a sidecar (and not an in-process ``realsense_receiver.py``):

  The L515 was EOL'd and **removed from the RealSense SDK at v2.55.1**. It exists
  only in ``pyrealsense2 <= 2.54.x``, whose wheels ship only for CPython 3.7-3.11.
  The main app runs on Python 3.14, so the L515 cannot be hosted in-process.

  This module runs in a dedicated Python 3.10 venv (``.venv-realsense``), captures
  aligned depth + color from the L515, and streams the **exact same**
  ``camera_frame`` + ``depth_frame`` WebSocket messages the iPad already sends to
  ``core/ipad_bridge.py``. The main app routes those message types unchanged:

      depth_frame  -> LiDARReceiver.on_depth_frame()   (sensors/lidar_receiver.py)
      camera_frame -> GestureProcessor.on_camera_frame() (sensors/gesture_processor.py)

  So the gesture/depth pipeline needs zero modification to consume the L515.

Critical: depth is aligned to the color stream (``rs.align(rs.stream.color)``).
MediaPipe emits normalized (nx, ny) landmarks in the *color* image; the depth
lookup ``LiDARReceiver.get_depth_at(nx, ny)`` indexes the depth array with the
same normalized coords. Without alignment every depth-gated gesture reads the
wrong depth.

The pure encode/convert helpers below import only numpy + cv2 (both present in the
main 3.14 env) so they can be unit-tested without the camera or pyrealsense2.
Run:
    python sensors/realsense_publisher.py --list-devices     # verify enumeration
    python sensors/realsense_publisher.py                    # stream to the bridge
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import time
from typing import Optional

import numpy as np

try:  # cv2 is present in both the main (3.14) and sidecar (3.10) envs.
    import cv2
    _CV2_AVAILABLE = True
except ImportError:  # pragma: no cover - cv2 is a hard dep in practice
    cv2 = None  # type: ignore
    _CV2_AVAILABLE = False

try:  # Only importable in the 3.10 sidecar venv with the L515 SDK installed.
    import pyrealsense2 as rs
    _RS_AVAILABLE = True
except Exception:  # noqa: BLE001 - any import/runtime error means no SDK here
    rs = None  # type: ignore
    _RS_AVAILABLE = False

log = logging.getLogger("realsense_publisher")

# L515 produces 0 for invalid / no-return depth pixels. We synthesise an
# ARKit-style confidence map: invalid -> 0 (LiDARReceiver masks these to NaN),
# valid -> 2 (high). This preserves the depth contract LiDARReceiver expects.
_CONF_INVALID = np.uint8(0)
_CONF_VALID = np.uint8(2)


# --------------------------------------------------------------------------- #
# Pure helpers (numpy + cv2 only — unit-testable without pyrealsense2)
# --------------------------------------------------------------------------- #

def depth_units_to_metres(depth_z16: np.ndarray, depth_scale: float) -> np.ndarray:
    """Convert a raw z16 depth frame to float32 metres.

    RealSense returns depth as uint16 device units; multiply by the device
    ``get_depth_scale()`` (~0.00025 m/unit for the L515) to get metres.
    """
    return depth_z16.astype(np.float32) * float(depth_scale)


def synth_confidence(depth_m: np.ndarray) -> np.ndarray:
    """Synthesise a uint8 confidence map from a metres depth frame.

    The L515 has no per-pixel confidence stream. Treat a non-positive (or NaN)
    reading as an invalid pixel (conf 0); everything else is high (conf 2).
    """
    valid = depth_m > 0.0  # NaN compares False, so NaN -> invalid too
    return np.where(valid, _CONF_VALID, _CONF_INVALID).astype(np.uint8)


def encode_depth_frame(depth_m: np.ndarray, conf: np.ndarray, ts_ms: float) -> dict:
    """Build a ``depth_frame`` message matching LiDARReceiver.on_depth_frame().

    depth is serialised as little-endian float32, conf as uint8 — the exact
    layout ``sensors/lidar_receiver.py`` deserialises with ``<f4`` / ``u1``.
    """
    h, w = depth_m.shape
    depth_le = np.ascontiguousarray(depth_m, dtype="<f4")
    conf_u8 = np.ascontiguousarray(conf, dtype="u1")
    return {
        "type": "depth_frame",
        "ts": float(ts_ms),
        "width": int(w),
        "height": int(h),
        "depth_b64": base64.b64encode(depth_le.tobytes()).decode("ascii"),
        "conf_b64": base64.b64encode(conf_u8.tobytes()).decode("ascii"),
    }


def encode_camera_frame(bgr: np.ndarray, jpeg_quality: int = 80) -> dict:
    """Build a ``camera_frame`` message matching GestureProcessor.on_camera_frame().

    Encodes BGR as JPEG (same as the iPad) -> base64 in ``image_b64``.
    """
    if not _CV2_AVAILABLE:
        raise RuntimeError("cv2 is required to encode camera frames")
    ok, buf = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), int(jpeg_quality)])
    if not ok:
        raise ValueError("cv2.imencode failed")
    return {
        "type": "camera_frame",
        "image_b64": base64.b64encode(buf.tobytes()).decode("ascii"),
    }


_ROTATE_CODES = {
    90: cv2.ROTATE_90_CLOCKWISE if _CV2_AVAILABLE else None,
    180: cv2.ROTATE_180 if _CV2_AVAILABLE else None,
    270: cv2.ROTATE_90_COUNTERCLOCKWISE if _CV2_AVAILABLE else None,
}


def rotate_frame(arr: np.ndarray, degrees: int) -> np.ndarray:
    """Rotate a frame by 0/90/180/270 degrees (cv2). 0 = no-op.

    Apply the SAME rotation to color and depth so they stay aligned. Use when the
    camera is physically mounted rotated (e.g. upside down -> 180).
    """
    if degrees % 360 == 0 or not _CV2_AVAILABLE:
        return arr
    code = _ROTATE_CODES.get(degrees % 360)
    if code is None:
        return arr
    return cv2.rotate(arr, code)


def downscale_depth(depth_m: np.ndarray, width: int, height: int) -> np.ndarray:
    """Nearest-neighbour downscale of a metres depth frame.

    Nearest-neighbour (not bilinear) so invalid-zero pixels never bleed into
    valid neighbours. Normalized-coordinate depth lookups are resolution
    independent, so transporting depth at a lower resolution is lossless for
    the gesture pipeline's purposes but much cheaper on the wire.
    """
    h, w = depth_m.shape
    if (w, h) == (width, height):
        return depth_m
    if not _CV2_AVAILABLE:
        return depth_m
    return cv2.resize(depth_m, (width, height), interpolation=cv2.INTER_NEAREST)


# --------------------------------------------------------------------------- #
# Device enumeration
# --------------------------------------------------------------------------- #

def list_devices() -> int:
    """Print connected RealSense devices + depth scale. Returns process exit code."""
    if not _RS_AVAILABLE:
        log.error(
            "pyrealsense2 not importable here. Install it in a Python<=3.11 venv: "
            "pip install -r requirements-realsense.txt"
        )
        return 1
    ctx = rs.context()
    devices = ctx.query_devices()
    if len(devices) == 0:
        log.error("No RealSense devices found. Check the USB connection / cable (USB3).")
        return 1
    for dev in devices:
        name = dev.get_info(rs.camera_info.name)
        serial = dev.get_info(rs.camera_info.serial_number)
        try:
            scale = dev.first_depth_sensor().get_depth_scale()
        except Exception:  # noqa: BLE001
            scale = float("nan")
        log.info("Found: %s  serial=%s  depth_scale=%.6f m/unit", name, serial, scale)
    return 0


# --------------------------------------------------------------------------- #
# Capture + stream loop (requires pyrealsense2)
# --------------------------------------------------------------------------- #

async def run(args: argparse.Namespace) -> int:
    """Capture from the L515 and stream frames to the bridge until interrupted."""
    if not _RS_AVAILABLE:
        log.error(
            "pyrealsense2 not available — run this from the .venv-realsense (Python 3.10) "
            "venv. See requirements-realsense.txt."
        )
        return 1
    try:
        import websockets
    except ImportError:
        log.error("websockets not installed in this venv — pip install -r requirements-realsense.txt")
        return 1

    uri = f"ws://{args.host}:{args.port}{args.path}"

    want_depth = not args.no_depth
    pipeline = rs.pipeline()
    align = rs.align(rs.stream.color) if want_depth else None

    # Detect the USB link up front. On USB2 the L515 cannot carry depth + color
    # at 30fps simultaneously — the device resets mid-stream ("No device
    # connected"). Clamp to 15fps on USB2 ONLY when depth is enabled; color-only
    # streams fine at 30fps on USB2 (and the Relaxed-Pointer model is depth-free,
    # so --no-depth gives the smoother 30fps cursor without a USB3 cable).
    eff_fps = args.fps
    if want_depth:
        try:
            _devs = rs.context().query_devices()
            if len(_devs) and _devs[0].get_info(rs.camera_info.usb_type_descriptor).startswith("2"):
                if eff_fps > 15:
                    log.warning("USB2 link + depth — clamping to 15fps (was %d) to avoid resets. "
                                "Use --no-depth for 30fps color, or a USB3 port.", eff_fps)
                    eff_fps = 15
        except Exception:  # noqa: BLE001
            pass

    # The L515 supports DIFFERENT resolutions for depth vs color, and the set of
    # available profiles depends on the USB link (USB2 caps depth to 320x240).
    # Enable color explicitly (640x480 bgr8 is universal); let librealsense pick
    # the native depth profile for the link. Retry fully-auto if the request fails.
    def _make_config(depth_explicit: bool) -> "rs.config":
        cfg = rs.config()
        cfg.enable_stream(rs.stream.color, args.color_width, args.color_height,
                          rs.format.bgr8, eff_fps)
        if want_depth:
            if depth_explicit and args.depth_capture_width > 0:
                cfg.enable_stream(rs.stream.depth, args.depth_capture_width,
                                  args.depth_capture_height, rs.format.z16, eff_fps)
            else:
                cfg.enable_stream(rs.stream.depth)
        return cfg

    try:
        profile = pipeline.start(_make_config(depth_explicit=True))
    except Exception as exc:  # noqa: BLE001
        log.warning("requested stream config rejected (%s) — retrying with auto profiles", exc)
        profile = pipeline.start(_make_config(depth_explicit=False))

    usb = "?"
    try:
        usb = profile.get_device().get_info(rs.camera_info.usb_type_descriptor)
    except Exception:  # noqa: BLE001
        pass
    cvp = profile.get_stream(rs.stream.color).as_video_stream_profile()
    depth_scale = 0.0
    if want_depth:
        depth_scale = profile.get_device().first_depth_sensor().get_depth_scale()
        dvp = profile.get_stream(rs.stream.depth).as_video_stream_profile()
        log.info("L515 streaming  depth=%dx%d color=%dx%d @%dfps  usb=%s  depth_scale=%.6f -> %s",
                 dvp.width(), dvp.height(), cvp.width(), cvp.height(), eff_fps, usb, depth_scale, uri)
    else:
        log.info("L515 streaming  COLOR-ONLY %dx%d @%dfps  usb=%s -> %s  (depth disabled; "
                 "depth-free pointer mode)", cvp.width(), cvp.height(), eff_fps, usb, uri)

    frame_idx = 0
    try:
        while True:  # reconnect loop — survive bridge restarts
            try:
                async with websockets.connect(uri, max_size=None) as ws:
                    log.info("connected to bridge at %s", uri)
                    while True:
                        frames = await asyncio.to_thread(pipeline.wait_for_frames)
                        if want_depth:
                            frames = align.process(frames)
                        color = frames.get_color_frame()
                        if not color:
                            continue
                        bgr = rotate_frame(np.asanyarray(color.get_data()), args.rotate)
                        if args.scale != 1.0 and _CV2_AVAILABLE:
                            bgr = cv2.resize(bgr, None, fx=args.scale, fy=args.scale,
                                             interpolation=cv2.INTER_AREA)
                        await ws.send(json.dumps(encode_camera_frame(bgr, args.jpeg_quality)))

                        if want_depth:
                            depth = frames.get_depth_frame()
                            if depth and frame_idx % args.depth_every == 0:
                                z16 = np.asanyarray(depth.get_data())
                                depth_m = depth_units_to_metres(z16, depth_scale)
                                depth_m = rotate_frame(depth_m, args.depth_rotate)
                                depth_m = downscale_depth(depth_m, args.depth_width, args.depth_height)
                                conf = synth_confidence(depth_m)
                                await ws.send(
                                    json.dumps(encode_depth_frame(depth_m, conf, time.time() * 1000.0))
                                )
                        frame_idx += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect on any WS/IO error
                log.warning("connection lost (%s) — reconnecting in 2s", exc)
                await asyncio.sleep(2.0)
    finally:
        pipeline.stop()  # release the USB handle cleanly (soak / B3 hygiene)
        log.info("pipeline stopped")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Intel RealSense L515 capture sidecar")
    p.add_argument("--host", default="127.0.0.1", help="bridge host (default 127.0.0.1)")
    p.add_argument("--port", type=int, default=8765, help="bridge WebSocket port (default 8765)")
    p.add_argument("--path", default="/ws", help="bridge WebSocket path (default /ws)")
    p.add_argument("--color-width", type=int, default=640, help="color capture width (default 640)")
    p.add_argument("--color-height", type=int, default=480, help="color capture height (default 480)")
    p.add_argument("--depth-capture-width", type=int, default=0,
                   help="depth capture width (0 = auto/native for the USB link)")
    p.add_argument("--depth-capture-height", type=int, default=0,
                   help="depth capture height (0 = auto/native for the USB link)")
    p.add_argument("--fps", type=int, default=30, help="capture fps (default 30)")
    p.add_argument("--no-depth", action="store_true",
                   help="color-only mode (no depth stream) — enables 30fps on USB2 for the "
                        "depth-free Relaxed-Pointer cursor model")
    p.add_argument("--depth-every", type=int, default=2,
                   help="send depth every Nth frame (default 2 -> ~15fps depth)")
    p.add_argument("--depth-width", type=int, default=320, help="transported depth width (default 320)")
    p.add_argument("--depth-height", type=int, default=240, help="transported depth height (default 240)")
    p.add_argument("--jpeg-quality", type=int, default=80, help="color JPEG quality (default 80)")
    p.add_argument("--scale", type=float, default=1.0,
                   help="downscale color before sending (e.g. 0.5 -> 320x240) so MediaPipe runs "
                        "faster and the cursor keeps up at 30fps. Landmarks are normalized so "
                        "accuracy is preserved.")
    p.add_argument("--rotate", type=int, default=180, choices=[0, 90, 180, 270],
                   help="rotate COLOR N degrees (default 180 — camera mounted upside down)")
    p.add_argument("--depth-rotate", type=int, default=0, choices=[0, 90, 180, 270],
                   help="rotate DEPTH N degrees (default 0). On this L515 align returns depth "
                        "180deg from color, so color=180 + depth=0 makes both upright AND aligned.")
    p.add_argument("--list-devices", action="store_true", help="list RealSense devices and exit")
    p.add_argument("--debug", action="store_true", help="verbose logging")
    return p


def main(argv: Optional[list] = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.list_devices:
        return list_devices()
    try:
        return asyncio.run(run(args)) or 0
    except KeyboardInterrupt:
        log.info("interrupted — shutting down")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
