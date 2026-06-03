"""Unit tests for IPadBridge.dwell_click handler (tilt dwell-to-click).

The iPad fires a `dwell_click` message after the cursor is held still long
enough; the PC executes whatever DwellActionToolbar selected
(_active_dwell_action) at the current cursor position.

Run:
    pytest tests/test_dwell_click.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.ipad_bridge import IPadBridge


@pytest.fixture
def bridge():
    with patch("core.ipad_bridge.CommandExecutor"):
        return IPadBridge(port=9999)


@pytest.fixture
def mock_ws():
    ws = AsyncMock()
    ws.send_json = AsyncMock()
    return ws


# action_type → (pyautogui function name, expected kwargs)
ACTION_TO_CALL = {
    "left_click": ("click", {"button": "left", "_pause": False}),
    "right_click": ("click", {"button": "right", "_pause": False}),
    "double_click": ("doubleClick", {"_pause": False}),
    "drag_start": ("mouseDown", {"_pause": False}),
    "drag_end": ("mouseUp", {"_pause": False}),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("action,expected", ACTION_TO_CALL.items())
async def test_dwell_click_executes_active_action(bridge, action, expected):
    """Each active dwell action maps to the right pyautogui call."""
    bridge._active_dwell_action = action
    fn_name, fn_kwargs = expected
    with patch("pyautogui.click") as click, \
            patch("pyautogui.doubleClick") as dbl, \
            patch("pyautogui.mouseDown") as down, \
            patch("pyautogui.mouseUp") as up:
        result = await bridge._handle_dwell_click()

    calls = {"click": click, "doubleClick": dbl, "mouseDown": down, "mouseUp": up}
    assert result["status"] == "ok"
    assert result["action"] == action
    calls[fn_name].assert_called_once_with(**fn_kwargs)
    # No other pyautogui function should have fired.
    for name, mock in calls.items():
        if name != fn_name:
            mock.assert_not_called()


@pytest.mark.asyncio
async def test_dwell_click_default_is_left_click(bridge):
    """Default action (no toolbar selection) left-clicks."""
    assert bridge._active_dwell_action == "left_click"
    with patch("pyautogui.click") as click, patch("pyautogui.doubleClick"), \
            patch("pyautogui.mouseDown"), patch("pyautogui.mouseUp"):
        result = await bridge._handle_dwell_click()
    assert result["status"] == "ok"
    click.assert_called_once_with(button="left", _pause=False)


@pytest.mark.asyncio
async def test_dwell_click_unknown_action_errors(bridge):
    """An unexpected action value errors instead of silently clicking."""
    bridge._active_dwell_action = "triple_click"
    with patch("pyautogui.click") as click, patch("pyautogui.doubleClick"), \
            patch("pyautogui.mouseDown"), patch("pyautogui.mouseUp"):
        result = await bridge._handle_dwell_click()
    assert result["status"] == "error"
    click.assert_not_called()


@pytest.mark.asyncio
async def test_handle_message_routes_dwell_click(bridge, mock_ws):
    """_handle_message routes dwell_click to the handler and acks."""
    bridge._active_dwell_action = "right_click"
    raw = json.dumps({"type": "dwell_click", "id": "d1"})
    with patch("pyautogui.click") as click, patch("pyautogui.doubleClick"), \
            patch("pyautogui.mouseDown"), patch("pyautogui.mouseUp"):
        await bridge._handle_message(mock_ws, raw)
    click.assert_called_once_with(button="right", _pause=False)
    assert mock_ws.send_json.called
    payload = mock_ws.send_json.call_args[0][0]
    assert payload["type"] == "ack"
    assert payload["status"] == "ok"
