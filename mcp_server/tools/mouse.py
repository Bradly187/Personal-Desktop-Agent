import pyautogui

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.05


def mouse_move(x: int, y: int) -> dict:
    """Move the mouse cursor to absolute screen coordinates."""
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
    pyautogui.click(x, y, button=button, clicks=clicks, interval=interval)
    return {"clicked": {"x": x, "y": y, "button": button, "clicks": clicks}}


def mouse_double_click(x: int, y: int) -> dict:
    """Double-click at the given coordinates."""
    pyautogui.doubleClick(x, y)
    return {"double_clicked": {"x": x, "y": y}}


def mouse_scroll(x: int, y: int, direction: str, clicks: int = 3) -> dict:
    """Scroll at the given coordinates. direction: 'up', 'down', 'left', 'right'."""
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
    pyautogui.moveTo(start_x, start_y)
    pyautogui.dragTo(end_x, end_y, duration=duration)
    return {"dragged": {"from": {"x": start_x, "y": start_y}, "to": {"x": end_x, "y": end_y}}}
