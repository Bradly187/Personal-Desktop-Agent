"""calibration.calibration_overlay.py — Tkinter calibration dot overlay for gaze monitor mapping.

Shows a translucent full-screen overlay on the primary monitor with a single
bright cyan dot at a known pixel position. Advances through 5 positions as the
user dwells on each one. Closes automatically after all dots are collected or
when the calibration is cancelled.

Follows the sensor_viewer.py pattern: runs in a daemon thread so it doesn't
block the asyncio event loop. Thread-safe communication via threading.Event
and queue.Queue.

Usage (wired by HybridCoordinator / voice trigger):
    overlay = CalibrationOverlay(screen_w=2560, screen_h=1440)
    overlay.start()                         # opens window in daemon thread

    target = overlay.current_target()       # (px_x, px_y) for current dot
    overlay.advance()                       # move to next dot
    overlay.finish()                        # close (all dots done or cancelled)
    overlay.cancel()                        # close (user cancelled)
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Optional

log = logging.getLogger(__name__)

# Dot appearance
_DOT_RADIUS   = 32      # pixels — large enough to see from monitor distance
_DOT_COLOR    = "#00E5FF"   # bright cyan
_CROSS_COLOR  = "#FFFFFF"   # white crosshair
_BG_COLOR     = "#000000"   # black overlay background
_BG_ALPHA     = 0.25        # overlay transparency (0=invisible, 1=opaque)
_LABEL_COLOR  = "#FFFFFF"

# Pad dot positions 5% from each edge so they're reachable by gaze
_PAD = 0.05


def _dot_positions(screen_w: int, screen_h: int) -> list[tuple[int, int]]:
    """5 calibration targets: top-left, top-right, center, bottom-left, bottom-right."""
    px = lambda fx: int(round(fx * screen_w))
    py = lambda fy: int(round(fy * screen_h))
    return [
        (px(_PAD),       py(_PAD)),        # top-left
        (px(1 - _PAD),   py(_PAD)),        # top-right
        (px(0.5),        py(0.5)),         # center
        (px(_PAD),       py(1 - _PAD)),    # bottom-left
        (px(1 - _PAD),   py(1 - _PAD)),    # bottom-right
    ]


class CalibrationOverlay:
    """Full-screen gaze calibration overlay.

    Call start() to open the window. Poll current_target() to get the active
    dot position. Call advance() after each successful dwell capture. Call
    finish() or cancel() to close.
    """

    def __init__(self, screen_w: int = 1920, screen_h: int = 1080) -> None:
        self._screen_w = screen_w
        self._screen_h = screen_h
        self._targets = _dot_positions(screen_w, screen_h)
        self._current_idx = 0

        self._cmd_queue: queue.Queue = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._done_event = threading.Event()

    # ── Public API (call from event loop thread) ───────────────────────────

    def start(self) -> None:
        """Open the overlay window in a daemon thread."""
        self._current_idx = 0
        self._done_event.clear()
        self._thread = threading.Thread(
            target=self._run_tk,
            daemon=True,
            name="calibration-overlay",
        )
        self._thread.start()
        log.info("CalibrationOverlay: started  targets=%d", len(self._targets))

    def current_target(self) -> Optional[tuple[int, int]]:
        """Return (px_x, px_y) of the currently displayed dot, or None if done."""
        if self._current_idx >= len(self._targets):
            return None
        return self._targets[self._current_idx]

    def advance(self) -> bool:
        """Advance to the next dot. Returns True if more dots remain, False when done."""
        self._current_idx += 1
        remaining = self._current_idx < len(self._targets)
        if remaining:
            self._cmd_queue.put(("next", self._current_idx))
            log.debug("CalibrationOverlay: advanced to dot %d", self._current_idx + 1)
        else:
            self.finish()
        return remaining

    def finish(self) -> None:
        """Close the overlay — calibration complete."""
        self._cmd_queue.put(("close", None))
        self._done_event.set()
        log.info("CalibrationOverlay: finished")

    def cancel(self) -> None:
        """Close the overlay — calibration cancelled."""
        self._cmd_queue.put(("close", None))
        self._done_event.set()
        log.info("CalibrationOverlay: cancelled")

    def wait_until_closed(self, timeout: float = 5.0) -> None:
        self._done_event.wait(timeout=timeout)

    # ── Tkinter thread ─────────────────────────────────────────────────────

    def _run_tk(self) -> None:
        try:
            import tkinter as tk
        except ImportError:
            log.error("CalibrationOverlay: tkinter not available")
            self._done_event.set()
            return

        root = tk.Tk()
        root.title("Gaze Calibration")
        root.attributes("-fullscreen", True)
        root.attributes("-topmost", True)
        root.attributes("-alpha", _BG_ALPHA)
        root.configure(bg=_BG_COLOR)
        root.overrideredirect(True)   # no title bar

        canvas = tk.Canvas(
            root,
            width=self._screen_w,
            height=self._screen_h,
            bg=_BG_COLOR,
            highlightthickness=0,
        )
        canvas.pack(fill="both", expand=True)

        # Allow Escape to cancel
        root.bind("<Escape>", lambda _: self.cancel())

        state = {"idx": 0}

        def draw_dot(idx: int) -> None:
            canvas.delete("all")
            if idx >= len(self._targets):
                return
            cx, cy = self._targets[idx]
            r = _DOT_RADIUS
            # Outer glow ring
            canvas.create_oval(
                cx - r - 8, cy - r - 8, cx + r + 8, cy + r + 8,
                outline=_DOT_COLOR, width=2, fill="",
            )
            # Filled dot
            canvas.create_oval(
                cx - r, cy - r, cx + r, cy + r,
                fill=_DOT_COLOR, outline="",
            )
            # Crosshair
            canvas.create_line(cx - r - 12, cy, cx + r + 12, cy,
                               fill=_CROSS_COLOR, width=2)
            canvas.create_line(cx, cy - r - 12, cx, cy + r + 12,
                               fill=_CROSS_COLOR, width=2)
            # Label
            canvas.create_text(
                cx, cy + r + 24,
                text=f"{idx + 1} / {len(self._targets)}",
                fill=_LABEL_COLOR, font=("Helvetica", 14, "bold"),
            )
            canvas.create_text(
                self._screen_w // 2, 30,
                text="Look at the dot and dwell to click",
                fill=_LABEL_COLOR, font=("Helvetica", 16),
            )

        draw_dot(0)

        def poll_commands() -> None:
            try:
                while True:
                    cmd, data = self._cmd_queue.get_nowait()
                    if cmd == "next":
                        state["idx"] = data
                        draw_dot(data)
                    elif cmd == "close":
                        root.quit()
                        return
            except queue.Empty:
                pass
            root.after(50, poll_commands)

        root.after(50, poll_commands)

        try:
            root.mainloop()
        finally:
            try:
                root.destroy()
            except Exception:
                pass
            self._done_event.set()
            log.debug("CalibrationOverlay: tk mainloop exited")
