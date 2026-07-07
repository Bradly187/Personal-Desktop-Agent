"""Sensor Viewer — Desktop window showing iPad camera + LiDAR depth feeds.

Runs a tkinter window in a dedicated daemon thread so it doesn't block the
asyncio event loop. Receives frames via thread-safe callbacks from ipad_bridge.

Features:
  - Camera + LiDAR depth side-by-side (aspect-ratio preserving resize)
  - Connection status indicator (green=live, amber=stale, grey=no data)
  - Always-on-top toggle (Ctrl+T or checkbox)
  - Hand landmark overlay from GestureProcessor
  - Freeze-frame (Space or click the Freeze button)
  - Snapshot to disk (Ctrl+S)
  - Depth-at-cursor readout on hover

Usage:
  Standalone:  python sensor_viewer.py          (opens with empty panels)
  Integrated:  main.py --viewer                 (wired into bridge pipeline)
  Viewer-only: main.py --viewer-only            (connects to running bridge)

No additional dependencies — uses tkinter (stdlib), Pillow, and numpy
which are already in requirements.txt.
"""

from __future__ import annotations

import base64
import io
import logging
import os
import queue
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageTk

log = logging.getLogger("sensor_viewer")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_STALE_THRESHOLD = 2.0  # seconds before feed is considered stale
_SNAPSHOT_DIR = Path("snapshots")

# Hand landmark connections (MediaPipe hand topology)
_HAND_CONNECTIONS = [
    (0, 1), (1, 2), (2, 3), (3, 4),       # thumb
    (0, 5), (5, 6), (6, 7), (7, 8),       # index
    (0, 9), (9, 10), (10, 11), (11, 12),  # middle
    (0, 13), (13, 14), (14, 15), (15, 16),  # ring
    (0, 17), (17, 18), (18, 19), (19, 20),  # pinky
    (5, 9), (9, 13), (13, 17),             # palm
]


# ---------------------------------------------------------------------------
# Depth colorization (matches iPad LiDARStreamer hue mapping)
# ---------------------------------------------------------------------------

def _depth_to_rgb(depth: np.ndarray, max_range: float = 4.0) -> np.ndarray:
    """Convert float32 depth map to an RGB image (H, W, 3) uint8.

    Near = blue (hue 240), far = red (hue 0). NaN/invalid = dark grey.
    """
    h, w = depth.shape
    rgb = np.full((h, w, 3), 40, dtype=np.uint8)

    valid = np.isfinite(depth) & (depth > 0)
    if not np.any(valid):
        return rgb

    t = np.clip(depth / max_range, 0.0, 1.0)
    hue = (1.0 - t) * 240.0
    # Zero out invalid pixels before int conversion to suppress NaN RuntimeWarnings
    hue = np.where(valid, hue, 0.0)

    h_sector = (hue / 60.0).astype(np.float32)
    i = h_sector.astype(np.int32) % 6
    f = h_sector - h_sector.astype(np.int32)

    r = np.zeros_like(hue, dtype=np.float32)
    g = np.zeros_like(hue, dtype=np.float32)
    b = np.zeros_like(hue, dtype=np.float32)

    mask0 = (i == 0) & valid
    mask1 = (i == 1) & valid
    mask2 = (i == 2) & valid
    mask3 = (i == 3) & valid
    mask4 = (i == 4) & valid
    mask5 = (i == 5) & valid

    r[mask0] = 1.0; g[mask0] = f[mask0]; b[mask0] = 0.0
    r[mask1] = 1.0 - f[mask1]; g[mask1] = 1.0; b[mask1] = 0.0
    r[mask2] = 0.0; g[mask2] = 1.0; b[mask2] = f[mask2]
    r[mask3] = 0.0; g[mask3] = 1.0 - f[mask3]; b[mask3] = 1.0
    r[mask4] = f[mask4]; g[mask4] = 0.0; b[mask4] = 1.0
    r[mask5] = 1.0; g[mask5] = 0.0; b[mask5] = 1.0 - f[mask5]

    rgb[..., 0] = (r * 255).astype(np.uint8)
    rgb[..., 1] = (g * 255).astype(np.uint8)
    rgb[..., 2] = (b * 255).astype(np.uint8)
    rgb[~valid] = [40, 40, 40]

    return rgb


# ---------------------------------------------------------------------------
# SensorViewer
# ---------------------------------------------------------------------------

class SensorViewer:
    """Desktop window showing live iPad camera and LiDAR depth feeds.

    Thread-safe: on_camera_frame(), on_depth_frame(), and on_hand_landmarks()
    can be called from the asyncio event loop. Data is queued
    and rendered in the tkinter thread.
    """

    # Default panel size (actual size adapts to frame aspect ratio)
    DEFAULT_PANEL_WIDTH = 480
    DEFAULT_PANEL_HEIGHT = 360

    def __init__(self, title: str = "iPad Sensor Viewer", always_on_top: bool = False):
        self._title = title
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._always_on_top = always_on_top
        self._frozen = False

        # Panel dimensions (updated when first frame arrives with aspect ratio)
        self._panel_w = self.DEFAULT_PANEL_WIDTH
        self._panel_h = self.DEFAULT_PANEL_HEIGHT
        self._cam_aspect: Optional[float] = None
        self._depth_aspect: Optional[float] = None

        # Thread-safe frame queues (maxsize=1 → always show latest)
        self._camera_q: queue.Queue[Image.Image] = queue.Queue(maxsize=1)
        self._depth_q: queue.Queue[Image.Image] = queue.Queue(maxsize=1)

        # Overlay data queues
        self._landmarks_q: queue.Queue[Optional[list]] = queue.Queue(maxsize=1)

        # Stats (written from asyncio thread, read from tk thread — atomic floats)
        self._camera_fps = 0.0
        self._depth_fps = 0.0
        self._camera_ts = 0.0
        self._depth_ts = 0.0

        # Raw depth for hover readout (latest frame, numpy array)
        self._raw_depth: Optional[np.ndarray] = None
        self._raw_depth_lock = threading.Lock()

        # Latest PIL images for snapshot saving
        self._last_camera_img: Optional[Image.Image] = None
        self._last_depth_img: Optional[Image.Image] = None

        # Hand landmarks (list of (x, y) normalised 0–1, 21 points)
        self._landmarks: Optional[list] = None

        # D7 FlickEngine — optional reference for debug overlay
        self._flick_engine = None
        # Thread-safe queue: each entry is (state_name, speed, preview_zone, zone_scores)
        self._flick_q: queue.Queue = queue.Queue(maxsize=1)

    # ---------------------------------------------------------------------- #
    # Lifecycle
    # ---------------------------------------------------------------------- #

    def start(self) -> None:
        """Start the viewer window in a background daemon thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_tk, daemon=True, name="SensorViewer")
        self._thread.start()
        log.info("SensorViewer started")

    def stop(self) -> None:
        """Signal the viewer to close. Safe to call from any thread."""
        self._running = False

    # ---------------------------------------------------------------------- #
    # Frame callbacks (called from asyncio thread)
    # ---------------------------------------------------------------------- #

    def on_camera_frame(self, msg: dict) -> None:
        """Handle a camera_frame message from ipad_bridge."""
        if self._frozen:
            return

        image_b64 = msg.get("image_b64", "")
        if not image_b64:
            return

        try:
            img_bytes = base64.b64decode(image_b64)
            img = Image.open(io.BytesIO(img_bytes))
        except Exception as exc:
            log.debug("SensorViewer: camera decode error: %s", exc)
            return

        # Compute aspect-preserving panel size on first frame
        if self._cam_aspect is None:
            self._cam_aspect = img.width / img.height
            self._panel_h = int(self._panel_w / self._cam_aspect)

        img = img.resize((self._panel_w, self._panel_h), Image.LANCZOS)  # type: ignore[attr-defined]

        # Draw hand landmarks overlay
        if self._landmarks:
            img = self._draw_landmarks(img, self._landmarks)

        # Update FPS
        now = time.monotonic()
        if self._camera_ts > 0:
            dt = now - self._camera_ts
            if dt > 0:
                self._camera_fps = 0.7 * self._camera_fps + 0.3 * (1.0 / dt)
        self._camera_ts = now

        self._last_camera_img = img

        # Drop old, push new
        try:
            self._camera_q.get_nowait()
        except queue.Empty:
            pass
        self._camera_q.put(img)

    def on_depth_frame(self, msg: dict) -> None:
        """Handle a depth_frame message from ipad_bridge."""
        if self._frozen:
            return

        try:
            w = int(msg["width"])
            h = int(msg["height"])
            depth_raw = base64.b64decode(msg["depth_b64"])
        except (KeyError, Exception) as exc:
            log.debug("SensorViewer: depth decode error: %s", exc)
            return

        try:
            depth = np.frombuffer(depth_raw, dtype="<f4").reshape(h, w).copy()
        except ValueError:
            return

        # Store raw depth for hover readout
        with self._raw_depth_lock:
            self._raw_depth = depth

        # Compute aspect-preserving size
        if self._depth_aspect is None:
            self._depth_aspect = w / h
            # Use same height as camera panel for visual alignment
            self._panel_h = int(self._panel_w / self._depth_aspect)

        # Colorize and resize
        rgb = _depth_to_rgb(depth)
        img = Image.fromarray(rgb).resize((self._panel_w, self._panel_h), Image.LANCZOS)  # type: ignore[attr-defined]

        # Update FPS
        now = time.monotonic()
        if self._depth_ts > 0:
            dt = now - self._depth_ts
            if dt > 0:
                self._depth_fps = 0.7 * self._depth_fps + 0.3 * (1.0 / dt)
        self._depth_ts = now

        self._last_depth_img = img

        # Drop old, push new
        try:
            self._depth_q.get_nowait()
        except queue.Empty:
            pass
        self._depth_q.put(img)

    def on_hand_landmarks(self, landmarks: Optional[list]) -> None:
        """Receive hand landmarks from GestureProcessor.

        landmarks: list of 21 (x, y) tuples normalised 0–1, or None.
        """
        self._landmarks = landmarks

    def set_flick_engine(self, engine) -> None:
        """Wire the D7 FlickEngine for the debug panel.  Thread-safe."""
        self._flick_engine = engine

    def push_flick_debug(self, state_name: str, speed: float,
                         preview_zone: Optional[str],
                         zone_scores: list[tuple[str, float]]) -> None:
        """Push a flick debug snapshot from the gesture thread.

        zone_scores: list of (zone_name, cosine_score) sorted by score desc.
        """
        data = (state_name, speed, preview_zone, zone_scores)
        try:
            self._flick_q.get_nowait()
        except queue.Empty:
            pass
        self._flick_q.put(data)

    # ---------------------------------------------------------------------- #
    # Overlay drawing
    # ---------------------------------------------------------------------- #

    def _draw_landmarks(self, img: Image.Image, landmarks: list) -> Image.Image:
        """Draw hand skeleton on camera image."""
        draw = ImageDraw.Draw(img)
        w, h = img.size
        points = [(int(lm[0] * w), int(lm[1] * h)) for lm in landmarks]

        # Draw connections
        for i, j in _HAND_CONNECTIONS:
            if i < len(points) and j < len(points):
                draw.line([points[i], points[j]], fill="#00ff88", width=2)

        # Draw joints
        for pt in points:
            r = 3
            draw.ellipse([pt[0] - r, pt[1] - r, pt[0] + r, pt[1] + r],
                         fill="#00ffcc", outline="#004433")

        return img

    # ---------------------------------------------------------------------- #
    # Snapshot
    # ---------------------------------------------------------------------- #

    def _save_snapshot(self) -> Optional[str]:
        """Save current camera + depth frames to disk. Returns path or None."""
        _SNAPSHOT_DIR.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")

        saved = []
        if self._last_camera_img:
            p = _SNAPSHOT_DIR / f"{ts}_camera.png"
            self._last_camera_img.save(p)
            saved.append(str(p))
        if self._last_depth_img:
            p = _SNAPSHOT_DIR / f"{ts}_depth.png"
            self._last_depth_img.save(p)
            saved.append(str(p))

        if saved:
            log.info("Snapshot saved: %s", ", ".join(saved))
            return saved[0]
        return None

    # ---------------------------------------------------------------------- #
    # Depth hover readout
    # ---------------------------------------------------------------------- #

    def _get_depth_at_pixel(self, px: int, py: int, display_w: int, display_h: int) -> Optional[float]:
        """Get depth in metres at a pixel position on the depth panel."""
        with self._raw_depth_lock:
            if self._raw_depth is None:
                return None
            dh, dw = self._raw_depth.shape
            # Map display pixel to depth array coords
            dx = int(px / display_w * dw)
            dy = int(py / display_h * dh)
            dx = max(0, min(dx, dw - 1))
            dy = max(0, min(dy, dh - 1))
            val = self._raw_depth[dy, dx]
            if np.isfinite(val) and val > 0:
                return float(val)
            return None

    # ---------------------------------------------------------------------- #
    # Tkinter main loop (runs in dedicated thread)
    # ---------------------------------------------------------------------- #

    def _run_tk(self) -> None:
        import tkinter as tk
        from tkinter import ttk

        root = tk.Tk()
        root.title(self._title)
        root.configure(bg="#1e1e1e")
        root.resizable(True, True)
        root.attributes("-topmost", self._always_on_top)

        # ---- Top toolbar ----
        toolbar = tk.Frame(root, bg="#2d2d2d", height=32)
        toolbar.pack(fill=tk.X, padx=0, pady=0)

        # Connection status indicator
        status_canvas = tk.Canvas(toolbar, width=14, height=14, bg="#2d2d2d",
                                  highlightthickness=0)
        status_canvas.pack(side=tk.LEFT, padx=(8, 4), pady=4)
        status_dot = status_canvas.create_oval(2, 2, 12, 12, fill="#555555", outline="")
        status_label = tk.Label(toolbar, text="No data", fg="#888888", bg="#2d2d2d",
                                font=("Segoe UI", 9))
        status_label.pack(side=tk.LEFT, padx=2)

        # Always-on-top toggle
        aot_var = tk.BooleanVar(value=self._always_on_top)

        def _toggle_aot(*_):
            self._always_on_top = aot_var.get()
            root.attributes("-topmost", self._always_on_top)

        aot_check = tk.Checkbutton(toolbar, text="Pin", variable=aot_var,
                                   command=_toggle_aot, fg="#aaaaaa", bg="#2d2d2d",
                                   selectcolor="#1e1e1e", activebackground="#2d2d2d",
                                   activeforeground="#ffffff", font=("Segoe UI", 9))
        aot_check.pack(side=tk.RIGHT, padx=8)

        # Freeze button
        freeze_var = tk.BooleanVar(value=False)

        def _toggle_freeze(*_):
            self._frozen = freeze_var.get()

        freeze_check = tk.Checkbutton(toolbar, text="Freeze", variable=freeze_var,
                                      command=_toggle_freeze, fg="#aaaaaa", bg="#2d2d2d",
                                      selectcolor="#1e1e1e", activebackground="#2d2d2d",
                                      activeforeground="#ffffff", font=("Segoe UI", 9))
        freeze_check.pack(side=tk.RIGHT, padx=4)

        # Snapshot button
        snap_label = tk.Label(toolbar, text="", fg="#66cc66", bg="#2d2d2d",
                              font=("Segoe UI", 8))
        snap_label.pack(side=tk.RIGHT, padx=4)

        def _do_snapshot(*_):
            path = self._save_snapshot()
            if path:
                snap_label.configure(text=f"Saved: {Path(path).name}")
                root.after(3000, lambda: snap_label.configure(text=""))

        snap_btn = tk.Button(toolbar, text="📷", command=_do_snapshot,
                             bg="#3d3d3d", fg="#ffffff", relief=tk.FLAT,
                             font=("Segoe UI", 10), padx=6)
        snap_btn.pack(side=tk.RIGHT, padx=4)

        # ---- Main content ----
        content = tk.Frame(root, bg="#1e1e1e")
        content.pack(fill=tk.BOTH, expand=True, padx=8, pady=(4, 8))

        # Camera panel
        cam_frame = tk.Frame(content, bg="#1e1e1e")
        cam_frame.pack(side=tk.LEFT, padx=4, fill=tk.BOTH, expand=True)
        cam_header = tk.Label(cam_frame, text="Camera", fg="#aaaaaa", bg="#1e1e1e",
                              font=("Segoe UI", 10))
        cam_header.pack()
        cam_canvas = tk.Label(cam_frame, bg="#2d2d2d")
        cam_canvas.pack(fill=tk.BOTH, expand=True)
        cam_info = tk.Label(cam_frame, text="-- fps", fg="#666666", bg="#1e1e1e",
                            font=("Segoe UI", 9))
        cam_info.pack()

        # Depth panel
        depth_frame_w = tk.Frame(content, bg="#1e1e1e")
        depth_frame_w.pack(side=tk.LEFT, padx=4, fill=tk.BOTH, expand=True)
        depth_header = tk.Label(depth_frame_w, text="LiDAR Depth", fg="#aaaaaa",
                                bg="#1e1e1e", font=("Segoe UI", 10))
        depth_header.pack()
        depth_canvas = tk.Label(depth_frame_w, bg="#2d2d2d")
        depth_canvas.pack(fill=tk.BOTH, expand=True)
        depth_info = tk.Label(depth_frame_w, text="-- fps", fg="#666666", bg="#1e1e1e",
                              font=("Segoe UI", 9))
        depth_info.pack()

        # Depth hover readout
        depth_readout = tk.Label(depth_frame_w, text="", fg="#cccccc", bg="#1e1e1e",
                                 font=("Segoe UI", 9))
        depth_readout.pack()

        def _on_depth_motion(event):
            d = self._get_depth_at_pixel(event.x, event.y, self._panel_w, self._panel_h)
            if d is not None:
                depth_readout.configure(text=f"{d:.2f} m")
            else:
                depth_readout.configure(text="--")

        depth_canvas.bind("<Motion>", _on_depth_motion)
        depth_canvas.bind("<Leave>", lambda _: depth_readout.configure(text=""))

        # Keep references to PhotoImages (prevent GC)
        self._cam_photo: Optional[ImageTk.PhotoImage] = None
        self._depth_photo: Optional[ImageTk.PhotoImage] = None

        # ---- FlickEngine debug panel (D7) ----
        _V_ON_DISPLAY = 0.35   # must match flick_engine._V_ON

        flick_panel = tk.Frame(root, bg="#1e1e1e")
        flick_panel.pack(fill=tk.X, padx=8, pady=(0, 4))

        tk.Label(flick_panel, text="Flick:", fg="#888888", bg="#1e1e1e",
                 font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=(0, 4))

        flick_state_lbl = tk.Label(flick_panel, text="IDLE", fg="#aaaaaa",
                                   bg="#1e1e1e", font=("Segoe UI", 9, "bold"), width=12)
        flick_state_lbl.pack(side=tk.LEFT)

        # Speed gauge — coloured label
        speed_canvas = tk.Canvas(flick_panel, width=80, height=14, bg="#333333",
                                 highlightthickness=0)
        speed_canvas.pack(side=tk.LEFT, padx=4)
        speed_bar = speed_canvas.create_rectangle(0, 0, 0, 14, fill="#44cc44", outline="")
        speed_text = tk.Label(flick_panel, text="0.000", fg="#aaaaaa", bg="#1e1e1e",
                              font=("Segoe UI", 9), width=6)
        speed_text.pack(side=tk.LEFT)

        tk.Label(flick_panel, text="→", fg="#555555", bg="#1e1e1e",
                 font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=2)

        flick_zone_lbl = tk.Label(flick_panel, text="—", fg="#00E5FF",
                                  bg="#1e1e1e", font=("Segoe UI", 9), width=14)
        flick_zone_lbl.pack(side=tk.LEFT)

        # Zone score mini-bars (9 zones, drawn as canvas rectangles)
        _ZONE_NAMES = [
            "left_half", "right_half", "top_half", "bottom_half",
            "top_left", "top_right", "bottom_left", "bottom_right", "maximize",
        ]
        _ZONE_ABBR = {
            "left_half": "L½", "right_half": "R½", "top_half": "T½",
            "bottom_half": "B½", "top_left": "TL", "top_right": "TR",
            "bottom_left": "BL", "bottom_right": "BR", "maximize": "MAX",
        }
        zone_bar_w = 28
        zone_bar_gap = 2
        zone_bars_canvas = tk.Canvas(
            flick_panel,
            width=(zone_bar_w + zone_bar_gap) * len(_ZONE_NAMES),
            height=20,
            bg="#1e1e1e",
            highlightthickness=0,
        )
        zone_bars_canvas.pack(side=tk.LEFT, padx=6)
        _zone_rects = []
        _zone_texts = []
        for i, name in enumerate(_ZONE_NAMES):
            x0 = i * (zone_bar_w + zone_bar_gap)
            r = zone_bars_canvas.create_rectangle(x0, 10, x0 + zone_bar_w, 20,
                                                  fill="#333333", outline="")
            t = zone_bars_canvas.create_text(x0 + zone_bar_w // 2, 5,
                                             text=_ZONE_ABBR[name],
                                             fill="#555555", font=("Segoe UI", 7))
            _zone_rects.append(r)
            _zone_texts.append(t)

        # ---- Update loop ----
        def _update():
            if not self._running:
                root.destroy()
                return

            now = time.monotonic()

            # Camera
            try:
                img = self._camera_q.get_nowait()
                self._cam_photo = ImageTk.PhotoImage(img)
                cam_canvas.configure(image=self._cam_photo)
                cam_info.configure(text=f"{self._camera_fps:.1f} fps")
            except queue.Empty:
                pass

            # Depth
            try:
                img = self._depth_q.get_nowait()
                self._depth_photo = ImageTk.PhotoImage(img)
                depth_canvas.configure(image=self._depth_photo)
                depth_info.configure(text=f"{self._depth_fps:.1f} fps")
            except queue.Empty:
                pass

            # Connection status
            cam_age = now - self._camera_ts if self._camera_ts > 0 else float("inf")
            depth_age = now - self._depth_ts if self._depth_ts > 0 else float("inf")
            best_age = min(cam_age, depth_age)

            if best_age < _STALE_THRESHOLD:
                status_canvas.itemconfig(status_dot, fill="#44cc44")
                status_label.configure(text="Live", fg="#44cc44")
            elif best_age < float("inf"):
                status_canvas.itemconfig(status_dot, fill="#ccaa00")
                status_label.configure(text="Stale", fg="#ccaa00")
            else:
                status_canvas.itemconfig(status_dot, fill="#555555")
                status_label.configure(text="No data", fg="#888888")

            # FlickEngine debug panel
            try:
                state_name, speed, preview_zone, zone_scores = self._flick_q.get_nowait()
                _state_colors = {
                    "IDLE": "#555555", "GRABBED": "#44cc44",
                    "DRAGGING": "#44aaff", "FLICKING": "#ffaa00",
                    "RESOLVE": "#ff6600", "SNAPPING": "#cc44ff",
                    "DROP_IN_PLACE": "#888888", "SETTLING": "#aaaaaa",
                }
                flick_state_lbl.configure(
                    text=state_name,
                    fg=_state_colors.get(state_name, "#aaaaaa"),
                )
                # Speed bar (0→_V_ON_DISPLAY = full width)
                bar_fill = min(1.0, speed / _V_ON_DISPLAY)
                bar_px = int(bar_fill * 80)
                bar_color = "#44cc44" if speed < _V_ON_DISPLAY * 0.6 else (
                    "#ffaa00" if speed < _V_ON_DISPLAY else "#ff4444"
                )
                speed_canvas.coords(speed_bar, 0, 0, bar_px, 14)
                speed_canvas.itemconfig(speed_bar, fill=bar_color)
                speed_text.configure(text=f"{speed:.3f}")

                # Preview zone
                flick_zone_lbl.configure(
                    text=preview_zone or "—",
                    fg="#00E5FF" if preview_zone else "#555555",
                )
                # Zone score bars
                score_map = {name: score for name, score in zone_scores}
                for i, name in enumerate(_ZONE_NAMES):
                    score = score_map.get(name, -1.0)
                    bar_h = max(0, int(10 * (score + 1.0) / 2.0))  # [-1,1] → [0,10]
                    x0 = i * (zone_bar_w + zone_bar_gap)
                    zone_bars_canvas.coords(_zone_rects[i], x0, 20 - bar_h, x0 + zone_bar_w, 20)
                    is_winner = (name == preview_zone)
                    zone_bars_canvas.itemconfig(
                        _zone_rects[i],
                        fill="#00E5FF" if is_winner else ("#558866" if score > 0 else "#334433"),
                    )
                    zone_bars_canvas.itemconfig(
                        _zone_texts[i],
                        fill="#ffffff" if is_winner else "#555555",
                    )
            except queue.Empty:
                pass

            root.after(33, _update)

        # ---- Keyboard shortcuts ----
        def _on_key(event):
            if event.keysym == "space":
                self._frozen = not self._frozen
                freeze_var.set(self._frozen)
            elif event.char == "\x14":  # Ctrl+T
                aot_var.set(not aot_var.get())
                _toggle_aot()
            elif event.char == "\x13":  # Ctrl+S
                _do_snapshot()

        root.bind("<Key>", _on_key)

        def _on_close():
            self._running = False
            root.destroy()

        root.protocol("WM_DELETE_WINDOW", _on_close)

        # Set initial window size
        total_w = self._panel_w * 2 + 40
        total_h = self._panel_h + 100
        root.geometry(f"{total_w}x{total_h}")

        root.after(33, _update)

        try:
            root.mainloop()
        except Exception:
            pass
        finally:
            self._running = False

    # ---------------------------------------------------------------------- #
    # Properties
    # ---------------------------------------------------------------------- #

    @property
    def running(self) -> bool:
        return self._running


# ---------------------------------------------------------------------------
# Standalone test mode
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(name)s %(message)s")

    viewer = SensorViewer()
    viewer.start()

    print("SensorViewer running. Press Ctrl+C to exit.")
    print("Shortcuts: Space=freeze, Ctrl+T=pin on top, Ctrl+S=snapshot")
    print("(No iPad connected — showing empty panels. Connect iPad to see feeds.)")

    try:
        while viewer.running:
            time.sleep(0.5)
    except KeyboardInterrupt:
        viewer.stop()
        print("\nStopped.")
