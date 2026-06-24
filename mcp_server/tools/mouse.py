import logging

import pyautogui

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.05

log = logging.getLogger(__name__)


def mouse_move(x: int, y: int) -> dict:
    """Move the mouse cursor to absolute screen coordinates."""
    log.debug("mouse_move x=%d y=%d", x, y)
    pyautogui.moveTo(x, y)
    return {"moved_to": {"x": x, "y": y}}


def mouse_click(
    x: int,
    y: int,
    button: str = "left",
    clicks: int = 1,
    interval: float = 0.0,
) -> dict:
    """Click at the given coordinates. button: 'left', 'right', or 'middle'."""
    log.debug("mouse_click x=%d y=%d button=%s clicks=%d", x, y, button, clicks)
    try:
        pyautogui.click(x, y, button=button, clicks=clicks, interval=interval)
    except Exception as exc:
        log.warning("mouse_click failed x=%d y=%d: %s", x, y, exc)
        raise
    return {"clicked": {"x": x, "y": y, "button": button, "clicks": clicks}}


def mouse_double_click(x: int, y: int) -> dict:
    """Double-click at the given coordinates."""
    log.debug("mouse_double_click x=%d y=%d", x, y)
    try:
        pyautogui.doubleClick(x, y)
    except Exception as exc:
        log.warning("mouse_double_click failed x=%d y=%d: %s", x, y, exc)
        raise
    return {"double_clicked": {"x": x, "y": y}}


def mouse_scroll(x: int, y: int, direction: str, clicks: int = 3) -> dict:
    """Scroll at the given coordinates. direction: 'up', 'down', 'left', 'right'."""
    log.debug("mouse_scroll x=%d y=%d direction=%s clicks=%d", x, y, direction, clicks)
    pyautogui.moveTo(x, y)
    if direction == "up":
        pyautogui.scroll(clicks)
    elif direction == "down":
        pyautogui.scroll(-clicks)
    elif direction == "right":
        pyautogui.hscroll(clicks)
    elif direction == "left":
        pyautogui.hscroll(-clicks)
    else:
        raise ValueError(f"direction must be up/down/left/right, got: {direction}")
    return {"scrolled": {"x": x, "y": y, "direction": direction, "clicks": clicks}}


def mouse_drag(
    start_x: int,
    start_y: int,
    end_x: int,
    end_y: int,
    duration: float = 0.3,
) -> dict:
    """Drag from start to end coordinates."""
    log.debug("mouse_drag (%d,%d)→(%d,%d) dur=%.2fs", start_x, start_y, end_x, end_y, duration)
    try:
        pyautogui.moveTo(start_x, start_y)
        pyautogui.dragTo(end_x, end_y, duration=duration)
    except Exception as exc:
        log.warning("mouse_drag failed (%d,%d)→(%d,%d): %s", start_x, start_y, end_x, end_y, exc)
        raise
    return {"dragged": {"from": {"x": start_x, "y": start_y}, "to": {"x": end_x, "y": end_y}}}
