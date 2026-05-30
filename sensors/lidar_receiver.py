"""LiDARReceiver — receives and filters iPad LiDAR depth_frame WebSocket messages.

The iPad Pro (2020+) exposes ARKit ARDepthData:
  depthMap:      float32 CVPixelBuffer  (~256×192 px)  — depth in metres
  confidenceMap: uint8   CVPixelBuffer  (~256×192 px)  — 0=not-confident
                                                          1=low / medium
                                                          2=high

The Swift app serialises each buffer as little-endian binary, base64-encodes it,
and sends the JSON message:

    {
      "type":     "depth_frame",
      "ts":       <unix ms>,
      "width":    256,
      "height":   192,
      "depth_b64": "<base64 float32 LE>",
      "conf_b64":  "<base64 uint8>"
    }

Degrades gracefully when numpy is unavailable.
"""

from __future__ import annotations

import base64
import logging
import time
from typing import Optional

log = logging.getLogger(__name__)

try:
    import numpy as np
    _NP_AVAILABLE = True
except ImportError:
    _NP_AVAILABLE = False
    log.warning("numpy not installed — LiDARReceiver disabled.")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Minimum ARKit confidence level to keep a depth pixel.
# 0 = keep all, 1 = keep medium+high, 2 = keep high only.
_DEFAULT_CONF_MIN: int = 1


class LiDARReceiver:
    """Receives depth_frame messages and exposes filtered depth data.

    Thread-safety note: on_depth_frame() is called from the asyncio event loop;
    consumers (GestureProcessor, FusionEngine) read latest_depth from the same
    loop, so no locking is needed.
    """

    def __init__(self, conf_min: int = _DEFAULT_CONF_MIN) -> None:
        self._conf_min = conf_min
        self.latest_depth: Optional["np.ndarray"] = None   # float32 (H, W), NaN where masked
        self.latest_conf: Optional["np.ndarray"] = None    # uint8  (H, W)
        self.latest_ts: float = 0.0      # message timestamp (Unix ms from iPad)
        self._recv_mono: float = 0.0     # local monotonic time of last received frame
        self._frame_count: int = 0
        self._available: bool = _NP_AVAILABLE

    # ---------------------------------------------------------------------- #
    # Incoming message handler (called from IPadBridge)
    # ---------------------------------------------------------------------- #

    def on_depth_frame(self, msg: dict) -> None:
        """Decode and confidence-filter a depth_frame WebSocket message."""
        if not _NP_AVAILABLE:
            return

        try:
            w = int(msg["width"])
            h = int(msg["height"])
            depth_raw = base64.b64decode(msg["depth_b64"])
            conf_raw = base64.b64decode(msg["conf_b64"])
        except (KeyError, Exception) as exc:
            log.warning("LiDARReceiver: malformed depth_frame: %s", exc)
            return

        try:
            depth = np.frombuffer(depth_raw, dtype="<f4").reshape(h, w).copy()
            conf = np.frombuffer(conf_raw, dtype="u1").reshape(h, w).copy()
        except ValueError as exc:
            log.warning("LiDARReceiver: buffer reshape error: %s", exc)
            return

        # Mask low-confidence pixels with NaN so consumers can detect them.
        mask = conf < self._conf_min
        depth[mask] = np.nan

        self.latest_depth = depth
        self.latest_conf = conf
        self.latest_ts = msg.get("ts", time.time())
        self._recv_mono = time.monotonic()
        self._frame_count += 1
        log.debug(
            "LiDARReceiver: frame %d  valid=%.0f%%  range=%.2f–%.2f m",
            self._frame_count,
            100.0 * float(np.sum(~mask)) / mask.size,
            float(np.nanmin(depth)) if not np.all(mask) else 0.0,
            float(np.nanmax(depth)) if not np.all(mask) else 0.0,
        )

    # ---------------------------------------------------------------------- #
    # Depth query API (used by GestureProcessor)
    # ---------------------------------------------------------------------- #

    def get_depth_at(self, nx: float, ny: float) -> Optional[float]:
        """Return depth in metres at normalised screen coordinates (0–1, 0–1).

        Samples the 4 nearest pixels and returns an unweighted average of
        whichever neighbours have valid (non-NaN) depth values.
        Returns None when depth is unavailable or all neighbours are masked.
        """
        if self.latest_depth is None or not _NP_AVAILABLE:
            return None

        depth = self.latest_depth
        h, w = depth.shape
        px = nx * (w - 1)
        py = ny * (h - 1)

        x0, y0 = int(px), int(py)
        x1, y1 = min(x0 + 1, w - 1), min(y0 + 1, h - 1)

        samples = [
            depth[y0, x0], depth[y0, x1],
            depth[y1, x0], depth[y1, x1],
        ]
        valid = [s for s in samples if not np.isnan(s)]
        if not valid:
            return None
        return float(sum(valid) / len(valid))

    def is_fresh(self, max_age_s: float = 0.5) -> bool:
        """Return True if a depth frame arrived within max_age_s seconds."""
        return (time.monotonic() - self._recv_mono) < max_age_s

    def get_status(self) -> dict:
        return {
            "available": self._available,
            "frame_count": self._frame_count,
            "latest_ts": self.latest_ts,
            "conf_min": self._conf_min,
            "shape": list(self.latest_depth.shape) if self.latest_depth is not None else None,
        }
