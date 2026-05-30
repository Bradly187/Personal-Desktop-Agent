"""desktop.snap_zones — Windows snap grid definitions and Win32 placement (D7 spec).

Provides:
    get_snap_zones(hwnd) → list[SnapZone]
    apply_snap(hwnd, rect)
    move_window_drag(hwnd, rect, drag_pos)  # live drag without resizing

Snap grid (9 zones, matching Windows 11 Snap Layouts):
    left_half | right_half | top_half | bottom_half
    top_left  | top_right  | bot_left | bot_right | maximize
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import logging
import time
from dataclasses import dataclass
from typing import Optional, Tuple

log = logging.getLogger(__name__)

# SetWindowPos flags
_SWP_NOACTIVATE   = 0x0010
_SWP_SHOWWINDOW   = 0x0040
_SWP_NOZORDER     = 0x0004

# ShowWindow constants
_SW_RESTORE = 9


@dataclass(frozen=True)
class SnapZone:
    """A named snap target on one monitor.

    Attributes:
        name:     Human-readable label (e.g. "left_half").
        rect:     Screen-pixel rect (left, top, right, bottom) — absolute coords.
        centroid: Normalised (x, y) in [0,1]×[0,1] on the primary monitor,
                  used for cosine scoring against the flick direction.
    """
    name: str
    rect: Tuple[int, int, int, int]         # (left, top, right, bottom) pixels
    centroid: Tuple[float, float]           # normalised [0,1]×[0,1]


def get_snap_zones(hwnd: int = 0) -> list[SnapZone]:
    """Return the 9-zone snap grid for the monitor containing hwnd.

    Falls back to the primary monitor if hwnd is 0 or lookup fails.
    """
    try:
        work_left, work_top, work_right, work_bottom = _get_work_area(hwnd)
    except Exception as exc:
        log.warning("snap_zones: monitor lookup failed (%s), using fallback 1920×1080", exc)
        work_left, work_top, work_right, work_bottom = 0, 0, 1920, 1080

    W  = work_right  - work_left
    H  = work_bottom - work_top
    x0 = work_left
    y0 = work_top
    hw = W // 2   # half width
    hh = H // 2   # half height

    def norm(rect: Tuple[int, int, int, int]) -> Tuple[float, float]:
        l, t, r, b = rect
        cx = (l + r) / 2.0 / (work_right or 1)
        cy = (t + b) / 2.0 / (work_bottom or 1)
        return (cx, cy)

    zones: list[SnapZone] = []

    def add(name: str, l: int, t: int, r: int, b: int) -> None:
        rect = (l, t, r, b)
        zones.append(SnapZone(name=name, rect=rect, centroid=norm(rect)))

    # Full-edge halves
    add("left_half",   x0,       y0,       x0 + hw,  y0 + H)
    add("right_half",  x0 + hw,  y0,       work_right, y0 + H)
    add("top_half",    x0,       y0,       x0 + W,   y0 + hh)
    add("bottom_half", x0,       y0 + hh,  x0 + W,   work_bottom)

    # Quarters
    add("top_left",     x0,       y0,       x0 + hw,  y0 + hh)
    add("top_right",    x0 + hw,  y0,       work_right, y0 + hh)
    add("bottom_left",  x0,       y0 + hh,  x0 + hw,  work_bottom)
    add("bottom_right", x0 + hw,  y0 + hh,  work_right, work_bottom)

    # Maximize
    add("maximize", x0, y0, work_right, work_bottom)

    return zones


def apply_snap(hwnd: int, rect: Tuple[int, int, int, int]) -> bool:
    """Move and resize hwnd to rect (left, top, right, bottom) in screen pixels.

    Restores minimised windows first.  Returns True on success.
    """
    if not hwnd:
        log.warning("snap_zones.apply_snap: no hwnd")
        return False

    l, t, r, b = rect
    w = r - l
    h = b - t

    try:
        user32 = ctypes.windll.user32
        # Restore if minimised
        user32.ShowWindow(hwnd, _SW_RESTORE)
        time.sleep(0.05)

        ok = user32.SetWindowPos(
            hwnd,
            0,           # hWndInsertAfter (ignored with NOZORDER)
            l, t, w, h,
            _SWP_NOZORDER | _SWP_NOACTIVATE | _SWP_SHOWWINDOW,
        )
        if not ok:
            err = ctypes.get_last_error()
            log.warning("snap_zones.apply_snap: SetWindowPos failed  err=%d", err)
            return False

        log.info("snap_zones.apply_snap: hwnd=%d  (%d,%d,%d,%d)", hwnd, l, t, r, b)
        return True

    except Exception as exc:
        log.warning("snap_zones.apply_snap: %s", exc)
        return False


def move_window_drag(
    hwnd: int,
    rect: Optional[Tuple[int, int, int, int]],
    drag_pos: Optional[Tuple[int, int]] = None,
) -> bool:
    """Move hwnd during live drag without changing its size.

    If drag_pos is given, top-left is set to drag_pos while width/height
    are preserved from the current window rect.
    If rect is given, it is used directly (left, top, right, bottom).
    """
    if not hwnd:
        return False
    try:
        user32 = ctypes.windll.user32

        if drag_pos is not None:
            # Read current size
            cur = ctypes.wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(cur))
            w = cur.right  - cur.left
            h = cur.bottom - cur.top
            l, t = drag_pos
        elif rect is not None:
            l, t, r, b = rect
            w = r - l
            h = b - t
        else:
            return False

        user32.SetWindowPos(
            hwnd, 0, l, t, w, h,
            _SWP_NOZORDER | _SWP_NOACTIVATE,
        )
        return True
    except Exception as exc:
        log.debug("snap_zones.move_window_drag: %s", exc)
        return False


def get_window_rect(hwnd: int) -> Optional[Tuple[int, int, int, int]]:
    """Return (left, top, right, bottom) of hwnd, or None on failure."""
    try:
        import win32gui
        r = win32gui.GetWindowRect(hwnd)
        return r
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_work_area(hwnd: int) -> Tuple[int, int, int, int]:
    """Return the work area (excludes taskbar) of the monitor containing hwnd."""
    user32  = ctypes.windll.user32
    shcore  = ctypes.windll.shcore

    # MONITOR_DEFAULTTONEAREST = 2
    hmon = user32.MonitorFromWindow(hwnd, 2) if hwnd else user32.MonitorFromPoint(0, 0, 2)

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint),
                    ("rcMonitor", RECT), ("rcWork", RECT), ("dwFlags", ctypes.c_uint)]

    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    if not user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
        raise OSError("GetMonitorInfoW failed")

    r = mi.rcWork
    return (r.left, r.top, r.right, r.bottom)
