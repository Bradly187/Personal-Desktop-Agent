"""desktop.magnetic_overlay — on-screen highlight of the magnetic-click target.

Draws a click-through outline around the clickable element that a tilt-tap would
snap to right now, so the user can see *which* target they'll hit before they
commit the tap. Reads the shared ClickableTargetCache (no UIA calls of its own)
and the live cursor position, redrawing at ~30 Hz.

Implementation notes (Windows):
  * Runs its own Tk mainloop on a daemon thread (all Tk calls stay on it).
  * Spans the full virtual desktop (handles multi-monitor / negative origins).
  * Click-through via BOTH -transparentcolor (so only the outline is painted)
    and the WS_EX_LAYERED|WS_EX_TRANSPARENT extended style (so even the painted
    outline never intercepts input).

Degrades to a no-op when tkinter, win32, or the cache are unavailable, or when
DA_MAGNETIC_OVERLAY is set to 0.
"""

from __future__ import annotations

import ctypes
import logging
import os
import threading

log = logging.getLogger(__name__)

_TRANSPARENT = "#010101"   # painted regions of this color are fully see-through
_OUTLINE = "#00E5FF"       # cyan highlight
_OUTLINE_W = 3


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def _cursor_pos() -> tuple[int, int]:
    pt = _POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def _virtual_bounds() -> tuple[int, int, int, int]:
    """(left, top, width, height) of the whole virtual desktop."""
    gsm = ctypes.windll.user32.GetSystemMetrics
    return gsm(76), gsm(77), gsm(78), gsm(79)  # SM_*VIRTUALSCREEN


class MagneticOverlay:
    """Daemon-thread Tk overlay highlighting the current magnetic-snap target."""

    def __init__(self, cache, radius: int | None = None, fps: int = 30) -> None:
        self._cache = cache
        if radius is None:
            try:
                radius = int(os.environ.get("DA_SNAP_RADIUS_PX", "200"))
            except ValueError:
                radius = 200
        self._radius = radius
        self._interval_ms = max(16, int(1000 / max(1, fps)))
        self._thread: threading.Thread | None = None
        self._running = False
        self._root = None
        self._canvas = None
        self._vleft = self._vtop = 0

    def start(self) -> None:
        if self._running:
            return
        if os.environ.get("DA_MAGNETIC_OVERLAY", "1") == "0":
            log.info("MagneticOverlay: disabled via DA_MAGNETIC_OVERLAY=0")
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run, name="magnetic-overlay", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        root = self._root
        if root is not None:
            try:
                root.after(0, root.destroy)
            except Exception:
                pass

    # ── thread body ─────────────────────────────────────────────────────────

    def _run(self) -> None:
        try:
            import tkinter as tk
        except Exception as exc:
            log.warning("MagneticOverlay: tkinter unavailable — %s", exc)
            self._running = False
            return

        try:
            vleft, vtop, vw, vh = _virtual_bounds()
            self._vleft, self._vtop = vleft, vtop

            root = tk.Tk()
            root.overrideredirect(True)
            root.geometry(f"{vw}x{vh}+{vleft}+{vtop}")
            root.attributes("-topmost", True)
            try:
                root.attributes("-transparentcolor", _TRANSPARENT)
            except Exception:
                pass
            root.config(bg=_TRANSPARENT)

            canvas = tk.Canvas(
                root, bg=_TRANSPARENT, highlightthickness=0, bd=0
            )
            canvas.pack(fill="both", expand=True)
            self._root, self._canvas = root, canvas

            root.update_idletasks()
            self._make_click_through(root)

            self._tick()
            root.mainloop()
        except Exception as exc:
            log.warning("MagneticOverlay: stopped — %s", exc)
        finally:
            self._running = False

    @staticmethod
    def _make_click_through(root) -> None:
        """Apply WS_EX_LAYERED | WS_EX_TRANSPARENT so the window ignores input."""
        try:
            GWL_EXSTYLE = -20
            WS_EX_LAYERED = 0x00080000
            WS_EX_TRANSPARENT = 0x00000020
            hwnd = root.winfo_id()
            user32 = ctypes.windll.user32
            cur = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(
                hwnd, GWL_EXSTYLE, cur | WS_EX_LAYERED | WS_EX_TRANSPARENT
            )
        except Exception as exc:
            log.debug("MagneticOverlay: click-through style not applied — %s", exc)

    def _tick(self) -> None:
        if not self._running or self._root is None:
            return
        try:
            self._draw()
        except Exception as exc:
            log.debug("MagneticOverlay draw failed: %s", exc)
        try:
            self._root.after(self._interval_ms, self._tick)
        except Exception:
            self._running = False

    def _draw(self) -> None:
        canvas = self._canvas
        canvas.delete("hl")
        if self._cache is None or not self._cache.is_running():
            return
        x, y = _cursor_pos()
        tg = self._cache.nearest(x, y, self._radius)
        if tg is None:
            return
        l, t, r, b = tg.bounds
        # virtual-desktop coords → window-local coords
        l -= self._vleft; r -= self._vleft
        t -= self._vtop;  b -= self._vtop
        pad = 2
        canvas.create_rectangle(
            l - pad, t - pad, r + pad, b + pad,
            outline=_OUTLINE, width=_OUTLINE_W, tags="hl",
        )
