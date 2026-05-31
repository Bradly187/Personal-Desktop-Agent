"""Property-based tests for WebSocket protocol correctness (Property 17).

**Validates: Requirements 7.2, 7.4**

Property 17: WebSocket set_dwell_action protocol correctness
- For any `set_dwell_action` message: if action_type is in the valid set, the bridge
  SHALL respond with ack status "ok" echoing the action_type and update internal state;
  if action_type is not in the valid set, the bridge SHALL respond with ack status "error"
  and retain the previous action type unchanged.

Run:
    pytest tests/test_prop_websocket_protocol.py -v
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from hypothesis import given, settings, assume
from hypothesis import strategies as st

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.ipad_bridge import IPadBridge


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_DWELL_ACTIONS = ["left_click", "right_click", "double_click", "drag_start", "drag_end"]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def bridge():
    """Create a fresh IPadBridge instance with mocked CommandExecutor."""
    with patch("core.ipad_bridge.CommandExecutor"):
        b = IPadBridge(port=9999)
    return b


@pytest.fixture
def mock_ws():
    """Create a mock WebSocket that captures sent JSON."""
    ws = AsyncMock()
    ws.send_json = AsyncMock()
    return ws


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_bridge():
    """Create a fresh IPadBridge for use inside Hypothesis tests."""
    with patch("core.ipad_bridge.CommandExecutor"):
        return IPadBridge(port=9999)


def make_mock_ws():
    """Create a mock WebSocket for use inside Hypothesis tests."""
    ws = AsyncMock()
    ws.send_json = AsyncMock()
    return ws


def run_async(coro):
    """Run an async coroutine synchronously, compatible with Python 3.14+."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Property 17a: Valid action_type → ack "ok" + state update
# **Validates: Requirements 7.2**
# ---------------------------------------------------------------------------

class TestValidActionProtocol:
    """Valid set_dwell_action messages produce ack ok and update state."""

    @given(
        action_type=st.sampled_from(VALID_DWELL_ACTIONS),
        initial_action=st.sampled_from(VALID_DWELL_ACTIONS),
        msg_id=st.one_of(st.none(), st.text(min_size=1, max_size=20)),
    )
    @settings(max_examples=100)
    def test_valid_action_ack_ok_and_state_update(self, action_type, initial_action, msg_id):
        """For any valid action_type, bridge responds with ack ok echoing the
        action_type and updates internal state to that action_type.

        **Validates: Requirements 7.2**
        """
        bridge = make_bridge()
        ws = make_mock_ws()

        # Set initial state to something known
        bridge._active_dwell_action = initial_action

        # Build message
        msg = {"type": "set_dwell_action", "action_type": action_type}
        if msg_id is not None:
            msg["id"] = msg_id

        # Execute
        run_async(bridge._handle_set_dwell_action(ws, msg))

        # Verify ack was sent
        ws.send_json.assert_called_once()
        payload = ws.send_json.call_args[0][0]

        # Ack structure correctness
        assert payload["type"] == "ack"
        assert payload["status"] == "ok"
        assert payload["action_type"] == action_type

        # Message id echoed when present, absent when not
        if msg_id is not None:
            assert payload["id"] == msg_id
        else:
            assert "id" not in payload

        # State updated
        assert bridge._active_dwell_action == action_type


# ---------------------------------------------------------------------------
# Property 17b: Invalid action_type → ack "error" + state unchanged
# **Validates: Requirements 7.4**
# ---------------------------------------------------------------------------

class TestInvalidActionProtocol:
    """Invalid set_dwell_action messages produce ack error and leave state unchanged."""

    @given(
        invalid_action=st.text(min_size=0, max_size=50).filter(
            lambda s: s not in VALID_DWELL_ACTIONS
        ),
        initial_action=st.sampled_from(VALID_DWELL_ACTIONS),
        msg_id=st.one_of(st.none(), st.text(min_size=1, max_size=20)),
    )
    @settings(max_examples=100)
    def test_invalid_action_ack_error_and_state_unchanged(
        self, invalid_action, initial_action, msg_id
    ):
        """For any action_type NOT in the valid set, bridge responds with ack error
        and retains the previous active action type unchanged.

        **Validates: Requirements 7.4**
        """
        bridge = make_bridge()
        ws = make_mock_ws()

        # Set initial state
        bridge._active_dwell_action = initial_action

        # Build message with invalid action_type
        msg = {"type": "set_dwell_action", "action_type": invalid_action}
        if msg_id is not None:
            msg["id"] = msg_id

        # Execute
        run_async(bridge._handle_set_dwell_action(ws, msg))

        # Verify ack was sent
        ws.send_json.assert_called_once()
        payload = ws.send_json.call_args[0][0]

        # Ack structure correctness
        assert payload["type"] == "ack"
        assert payload["status"] == "error"
        assert "error" in payload
        assert len(payload["error"]) > 0  # error message is non-empty

        # Message id echoed when present
        if msg_id is not None:
            assert payload["id"] == msg_id

        # State unchanged
        assert bridge._active_dwell_action == initial_action

    @given(
        initial_action=st.sampled_from(VALID_DWELL_ACTIONS),
        msg_id=st.one_of(st.none(), st.text(min_size=1, max_size=20)),
    )
    @settings(max_examples=50)
    def test_none_action_type_ack_error(self, initial_action, msg_id):
        """When action_type is None (missing from message dict.get), bridge responds
        with ack error and state unchanged.

        **Validates: Requirements 7.4**
        """
        bridge = make_bridge()
        ws = make_mock_ws()

        bridge._active_dwell_action = initial_action

        # Message without action_type key (dict.get returns None)
        msg = {"type": "set_dwell_action"}
        if msg_id is not None:
            msg["id"] = msg_id

        run_async(bridge._handle_set_dwell_action(ws, msg))

        ws.send_json.assert_called_once()
        payload = ws.send_json.call_args[0][0]

        assert payload["type"] == "ack"
        assert payload["status"] == "error"
        assert "error" in payload
        assert bridge._active_dwell_action == initial_action
