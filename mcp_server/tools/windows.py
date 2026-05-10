import ctypes
import ctypes.wintypes

import win32gui
import win32process
import psutil


def _get_pid_for_hwnd(hwnd: int) -> int:
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    return pid


def get_active_window() -> dict:
    """Return info about the currently focused window."""
    hwnd = win32gui.GetForegroundWindow()
    if not hwnd:
        return {"title": None, "x": None, "y": None, "width": None, "height": None, "pid": None}

    title = win32gui.GetWindowText(hwnd)
    rect = win32gui.GetWindowRect(hwnd)
    x, y, x2, y2 = rect
    pid = _get_pid_for_hwnd(hwnd)

    return {
        "title": title,
        "x": x,
        "y": y,
        "width": x2 - x,
        "height": y2 - y,
        "pid": pid,
    }


def list_windows() -> list[dict]:
    """Return a list of all visible top-level windows with their titles and PIDs."""
    results = []

    def _enum_cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            title = win32gui.GetWindowText(hwnd)
            if title:
                pid = _get_pid_for_hwnd(hwnd)
                results.append({"title": title, "pid": pid})

    win32gui.EnumWindows(_enum_cb, None)
    return results


def focus_window(title_substring: str) -> dict:
    """Bring the first window whose title contains title_substring to the foreground."""
    target = title_substring.lower()
    found_hwnd = None

    def _enum_cb(hwnd, _):
        nonlocal found_hwnd
        if found_hwnd:
            return
        if win32gui.IsWindowVisible(hwnd):
            title = win32gui.GetWindowText(hwnd)
            if target in title.lower():
                found_hwnd = hwnd

    win32gui.EnumWindows(_enum_cb, None)

    if not found_hwnd:
        return {"focused": False, "title": None, "error": f"No window matching '{title_substring}'"}

    # SW_RESTORE = 9, handles minimized windows
    ctypes.windll.user32.ShowWindow(found_hwnd, 9)
    win32gui.SetForegroundWindow(found_hwnd)
    title = win32gui.GetWindowText(found_hwnd)
    return {"focused": True, "title": title}
