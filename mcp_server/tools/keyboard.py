import time

import pyautogui
import win32clipboard
import win32con

pyautogui.FAILSAFE = True


def keyboard_type(text: str, interval: float = 0.0) -> dict:
    """Type a string at the current cursor position. ASCII characters only.
    For unicode/math symbols use keyboard_paste instead."""
    pyautogui.typewrite(text, interval=interval)
    return {"typed": text}


def keyboard_paste(text: str) -> dict:
    """Write text to the clipboard then send Ctrl+V. Supports full unicode,
    including mathematical symbols (π, √, ∑, Greek letters, etc.)."""
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
    finally:
        win32clipboard.CloseClipboard()
    time.sleep(0.05)  # let clipboard settle before paste
    pyautogui.hotkey("ctrl", "v")
    return {"pasted": text}


def keyboard_hotkey(*keys: str) -> dict:
    """Press a key combination, e.g. ('ctrl', 'c') or ('win', 'd').
    Pass keys as individual arguments: keyboard_hotkey('ctrl', 'c')
    """
    pyautogui.hotkey(*keys)
    return {"hotkey": list(keys)}


def keyboard_press(key: str) -> dict:
    """Press a single key by name, e.g. 'enter', 'escape', 'tab', 'space', 'backspace'."""
    pyautogui.press(key)
    return {"pressed": key}


def keyboard_key_down(key: str) -> dict:
    """Hold a key down (e.g. 'shift' for text selection). Call keyboard_key_up to release."""
    pyautogui.keyDown(key)
    return {"key_down": key}


def keyboard_key_up(key: str) -> dict:
    """Release a held key."""
    pyautogui.keyUp(key)
    return {"key_up": key}
