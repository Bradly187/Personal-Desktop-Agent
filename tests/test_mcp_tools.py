"""Unit tripwires for the mcp_server/tools desktop-action surface (Gap 4).

These wrappers are thin pyautogui/win32 passthroughs — so they have NO dedicated
tests, and calling them for real would move the live mouse / type on the desktop.
pyautogui is mocked here so the tests are safe and assert the mapping logic only:
direction → scroll sign, argument forwarding, and the validation that rejects a
bad scroll direction. The genuinely action-taking branches are never executed.
"""

import pytest
from unittest.mock import MagicMock

from mcp_server.tools import keyboard, mouse


@pytest.fixture
def pag(monkeypatch):
    """Replace pyautogui in both tool modules with a recording mock."""
    fake = MagicMock()
    monkeypatch.setattr(mouse, "pyautogui", fake)
    monkeypatch.setattr(keyboard, "pyautogui", fake)
    return fake


# --------------------------------------------------------------------------- #
# mouse
# --------------------------------------------------------------------------- #
def test_mouse_move_forwards_coords(pag):
    assert mouse.mouse_move(10, 20) == {"moved_to": {"x": 10, "y": 20}}
    pag.moveTo.assert_called_once_with(10, 20)


def test_mouse_click_forwards_button_and_clicks(pag):
    mouse.mouse_click(5, 6, button="right", clicks=2)
    pag.click.assert_called_once_with(5, 6, button="right", clicks=2, interval=0.0)


def test_mouse_scroll_vertical_sign(pag):
    mouse.mouse_scroll(1, 2, "up", clicks=3)
    pag.scroll.assert_called_once_with(3)
    pag.reset_mock()
    mouse.mouse_scroll(1, 2, "down", clicks=3)
    pag.scroll.assert_called_once_with(-3)


def test_mouse_scroll_horizontal_sign(pag):
    mouse.mouse_scroll(1, 2, "right", clicks=4)
    pag.hscroll.assert_called_once_with(4)
    pag.reset_mock()
    mouse.mouse_scroll(1, 2, "left", clicks=4)
    pag.hscroll.assert_called_once_with(-4)


def test_mouse_scroll_invalid_direction_raises_without_scrolling(pag):
    with pytest.raises(ValueError):
        mouse.mouse_scroll(1, 2, "sideways")
    pag.scroll.assert_not_called()
    pag.hscroll.assert_not_called()


# --------------------------------------------------------------------------- #
# keyboard
# --------------------------------------------------------------------------- #
def test_keyboard_type_forwards_text(pag):
    assert keyboard.keyboard_type("hello") == {"typed": "hello"}
    pag.typewrite.assert_called_once_with("hello", interval=0.0)


def test_keyboard_hotkey_forwards_keys(pag):
    keyboard.keyboard_hotkey("ctrl", "c")
    pag.hotkey.assert_called_once_with("ctrl", "c")


def test_keyboard_press_forwards_key(pag):
    keyboard.keyboard_press("enter")
    pag.press.assert_called_once_with("enter")
