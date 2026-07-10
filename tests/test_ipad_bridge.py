"""B6 — IPadBridge._coordinator must be initialized; standalone-mode messages
must not crash the receive loop.

Spec: specs/bugfix-b6-ipad-coordinator/

``IPadBridge.__init__`` initialized every other optional phase-2 component to
``None`` but omitted ``_coordinator`` — it existed only after ``main.py`` called
``set_coordinator()``. The advertised standalone entry point
(``python ipad_bridge.py``) never calls it, so an iPad sending
``pain_day_override`` / ``flare_profile`` / ``calibration_start`` /
``calibration_cancel`` hit ``self._coordinator`` unset → ``AttributeError``,
tearing down the WebSocket receive loop.

The fix initializes ``self._coordinator = None``; the handlers' existing
truthiness guards then no-op safely. These tests lock the initialization and the
no-crash behavior, and confirm forwarding still works once a coordinator is wired.
"""
import json

import pytest

from core.ipad_bridge import IPadBridge

# Message types that dereference self._coordinator in _handle_message.
_COORDINATOR_MSG_TYPES = [
    "pain_day_override",
    "flare_profile",
    "calibration_start",
    "calibration_cancel",
]


class _FakeWS:
    """Minimal WebSocketResponse stand-in capturing acks."""

    def __init__(self):
        self.sent: list[dict] = []

    async def send_json(self, payload):
        self.sent.append(payload)


@pytest.fixture
def bridge():
    # token injected so no ~/.claude/ipad_bridge token file is created in tests.
    return IPadBridge(token="test-token")


def test_coordinator_attribute_exists(bridge):
    assert hasattr(bridge, "_coordinator")


def test_coordinator_defaults_to_none(bridge):
    assert bridge._coordinator is None


@pytest.mark.parametrize("msg_type", _COORDINATOR_MSG_TYPES)
async def test_unwired_coordinator_message_does_not_crash(bridge, msg_type):
    ws = _FakeWS()
    # Before B6 this raised AttributeError (self._coordinator undefined) and
    # killed the receive loop. It must now complete and still ack the iPad.
    await bridge._handle_message(ws, json.dumps({"type": msg_type, "id": 7}))
    assert ws.sent, f"{msg_type} produced no ack"
    assert ws.sent[-1]["type"] == "ack"
    assert ws.sent[-1]["id"] == 7


async def test_wired_coordinator_forwards_pain_day(bridge):
    class _Twin:
        def __init__(self):
            self.calls: list[bool] = []

        def set_manual_pain_day(self, active):
            self.calls.append(active)

    class _Coord:
        def __init__(self):
            self._twin = _Twin()

    coord = _Coord()
    bridge.set_coordinator(coord)
    ws = _FakeWS()

    await bridge._handle_message(
        ws, json.dumps({"type": "pain_day_override", "active": True, "id": 8})
    )

    assert coord._twin.calls == [True]
    assert ws.sent[-1]["type"] == "ack"
