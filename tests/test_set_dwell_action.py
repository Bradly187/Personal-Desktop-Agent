"""Unit tests for IPadBridge.set_dwell_action handler (task 1.1)

Tests:
- Valid action types produce ack with status "ok" and echo action_type
- Invalid action types produce ack with status "error" and error message
- Bridge state updates on valid input
- Bridge state unchanged on invalid input
- Default state is "left_click"

Run:
    pytest tests/test_set_dwell_action.py -v
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from ipad_bridge import IPadBridge


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def bridge():
    """Create a fresh IPadBridge instance."""
    with patch("ipad_bridge.CommandExecutor"):
        b = IPadBridge(port=9999)
    return b


@pytest.fixture
def mock_ws():
    """Create a mock WebSocket that captures sent JSON."""
    ws = AsyncMock()
    ws.send_json = AsyncMock()
    return ws


# ---------------------------------------------------------------------------
# Tests: default state
# ---------------------------------------------------------------------------

def test_default_dwell_action(bridge):
    """Bridge starts with left_click as the active dwell action."""
    assert bridge._active_dwell_action == "left_click"


# ---------------------------------------------------------------------------
# Tests: valid action types
# ---------------------------------------------------------------------------

VALID_ACTIONS = ["left_click", "right_click", "double_click", "drag_start", "drag_end"]


@pytest.mark.asyncio
@pytest.mark.parametrize("action_type", VALID_ACTIONS)
async def test_valid_action_type_ack(bridge, mock_ws, action_type):
    """Valid action_type produces ack with status ok and echoes the value."""
    msg = {"type": "set_dwell_action", "action_type": action_type, "id": "test-1"}
    await bridge._handle_set_dwell_action(mock_ws, msg)

    mock_ws.send_json.assert_called_once()
    payload = mock_ws.send_json.call_args[0][0]

    assert payload["type"] == "ack"
    assert payload["status"] == "ok"
    assert payload["action_type"] == action_type
    assert payload["id"] == "test-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("action_type", VALID_ACTIONS)
async def test_valid_action_type_updates_state(bridge, mock_ws, action_type):
    """Valid action_type updates the bridge's active dwell action state."""
    msg = {"type": "set_dwell_action", "action_type": action_type}
    await bridge._handle_set_dwell_action(mock_ws, msg)

    assert bridge._active_dwell_action == action_type


@pytest.mark.asyncio
async def test_valid_action_without_id(bridge, mock_ws):
    """Valid action_type without msg id still works, id not included in ack."""
    msg = {"type": "set_dwell_action", "action_type": "right_click"}
    await bridge._handle_set_dwell_action(mock_ws, msg)

    payload = mock_ws.send_json.call_args[0][0]
    assert payload["type"] == "ack"
    assert payload["status"] == "ok"
    assert payload["action_type"] == "right_click"
    assert "id" not in payload


# ---------------------------------------------------------------------------
# Tests: invalid action types
# ---------------------------------------------------------------------------

INVALID_ACTIONS = [
    "invalid",
    "LEFT_CLICK",
    "click",
    "",
    None,
    123,
    "left-click",
    "triple_click",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("action_type", INVALID_ACTIONS)
async def test_invalid_action_type_error_ack(bridge, mock_ws, action_type):
    """Invalid action_type produces ack with status error and error message."""
    msg = {"type": "set_dwell_action", "action_type": action_type, "id": "test-2"}
    await bridge._handle_set_dwell_action(mock_ws, msg)

    mock_ws.send_json.assert_called_once()
    payload = mock_ws.send_json.call_args[0][0]

    assert payload["type"] == "ack"
    assert payload["status"] == "error"
    assert "error" in payload
    assert payload["id"] == "test-2"


@pytest.mark.asyncio
@pytest.mark.parametrize("action_type", INVALID_ACTIONS)
async def test_invalid_action_type_state_unchanged(bridge, mock_ws, action_type):
    """Invalid action_type does not change the active dwell action state."""
    # Set a known state first
    bridge._active_dwell_action = "double_click"

    msg = {"type": "set_dwell_action", "action_type": action_type}
    await bridge._handle_set_dwell_action(mock_ws, msg)

    assert bridge._active_dwell_action == "double_click"


@pytest.mark.asyncio
async def test_missing_action_type_field(bridge, mock_ws):
    """Message without action_type field produces error ack."""
    msg = {"type": "set_dwell_action", "id": "test-3"}
    await bridge._handle_set_dwell_action(mock_ws, msg)

    payload = mock_ws.send_json.call_args[0][0]
    assert payload["status"] == "error"
    assert "error" in payload


# ---------------------------------------------------------------------------
# Tests: state transitions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sequential_action_changes(bridge, mock_ws):
    """Multiple valid set_dwell_action messages update state sequentially."""
    for action in ["right_click", "drag_start", "double_click", "left_click"]:
        msg = {"type": "set_dwell_action", "action_type": action}
        await bridge._handle_set_dwell_action(mock_ws, msg)
        assert bridge._active_dwell_action == action


# ---------------------------------------------------------------------------
# Tests: message routing integration
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_message_routes_set_dwell_action(bridge, mock_ws):
    """_handle_message correctly routes set_dwell_action to the handler."""
    raw = json.dumps({"type": "set_dwell_action", "action_type": "right_click", "id": "r1"})
    await bridge._handle_message(mock_ws, raw)

    # Should have sent an ack (the handler sends it)
    assert mock_ws.send_json.called
    payload = mock_ws.send_json.call_args[0][0]
    assert payload["type"] == "ack"
    assert payload["status"] == "ok"
    assert payload["action_type"] == "right_click"
    assert bridge._active_dwell_action == "right_click"


# ---------------------------------------------------------------------------
# Tests: welcome message includes active_dwell_action (task 1.4)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_welcome_message_includes_active_dwell_action_default(bridge, mock_ws):
    """Welcome status message includes active_dwell_action defaulting to left_click."""
    # Simulate what ws_handler does when sending the welcome message
    await mock_ws.send_json({
        "type": "status",
        "active_window": None,
        "cursor": {"x": 0, "y": 0},
        "active_dwell_action": bridge._active_dwell_action,
        "ts": 1234567890.0,
    })

    payload = mock_ws.send_json.call_args[0][0]
    assert payload["type"] == "status"
    assert payload["active_dwell_action"] == "left_click"


@pytest.mark.asyncio
async def test_welcome_message_reflects_prior_selection(bridge, mock_ws):
    """Welcome message reflects a previously set dwell action (not just default)."""
    # Set a non-default action first
    bridge._active_dwell_action = "right_click"

    await mock_ws.send_json({
        "type": "status",
        "active_window": None,
        "cursor": {"x": 0, "y": 0},
        "active_dwell_action": bridge._active_dwell_action,
        "ts": 1234567890.0,
    })

    payload = mock_ws.send_json.call_args[0][0]
    assert payload["active_dwell_action"] == "right_click"
