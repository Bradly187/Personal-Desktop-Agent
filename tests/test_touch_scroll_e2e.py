"""Integration test: touch command "scroll down" executes end-to-end (task 1.6)

Validates the Phase 1 touch_command path:
  iPad sends {"type":"touch_command","action":"SCROLL","params":{"direction":"down"}}
  → IPadBridge._handle_touch_command()
  → CommandExecutor.execute()
  → mouse.mouse_scroll(x, y, direction="down", clicks=3)

We mock the actual pyautogui/mouse call to avoid moving the real desktop,
and verify the correct function is called with the right arguments.

Run:
    python tests/test_touch_scroll_e2e.py

Exit codes:
    0 — all tests passed
    1 — one or more tests failed
"""

from __future__ import annotations

import asyncio
import json
import sys
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

try:
    import aiohttp
except ImportError as exc:
    print(f"ERROR: {exc}. Run: pip install aiohttp")
    sys.exit(1)

from ipad_bridge import IPadBridge

BRIDGE_PORT = 8767  # Unique port to avoid conflicts


# ---------------------------------------------------------------------------
# Test harness
# ---------------------------------------------------------------------------

async def start_test_bridge() -> tuple[IPadBridge, asyncio.Task]:
    """Start a minimal bridge (no FusionEngine needed for touch_command)."""
    bridge = IPadBridge(port=BRIDGE_PORT)
    bridge_task = asyncio.create_task(bridge.run(no_mdns=True))
    await asyncio.sleep(1.0)
    return bridge, bridge_task


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

async def test_scroll_down_e2e(mock_scroll: MagicMock) -> tuple[bool, str]:
    """Send touch_command SCROLL down → verify mouse_scroll called correctly."""
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(f"ws://localhost:{BRIDGE_PORT}/ws") as ws:
            await ws.send_json({
                "type": "touch_command",
                "id": "test-scroll-1",
                "action": "SCROLL",
                "text": "scroll down",
                "params": {"direction": "down", "amount": 3},
            })

            # Wait for ack
            resp = await asyncio.wait_for(ws.receive_json(), timeout=5.0)

            if resp.get("status") != "ok":
                return False, f"Expected status 'ok', got: {resp}"

            if not mock_scroll.called:
                return False, "mouse_scroll was never called"

            args, kwargs = mock_scroll.call_args
            # mouse_scroll(x, y, direction=..., clicks=...)
            direction = kwargs.get("direction", args[2] if len(args) > 2 else None)
            clicks = kwargs.get("clicks", args[3] if len(args) > 3 else None)

            if direction != "down":
                return False, f"Expected direction='down', got '{direction}'"
            if clicks != 3:
                return False, f"Expected clicks=3, got {clicks}"

            return True, f"mouse_scroll called with direction='down', clicks=3 at ({args[0]}, {args[1]})"


async def test_scroll_up_e2e(mock_scroll: MagicMock) -> tuple[bool, str]:
    """Send touch_command SCROLL up → verify direction='up'."""
    mock_scroll.reset_mock()

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(f"ws://localhost:{BRIDGE_PORT}/ws") as ws:
            await ws.send_json({
                "type": "touch_command",
                "id": "test-scroll-2",
                "action": "SCROLL",
                "text": "scroll up",
                "params": {"direction": "up", "amount": 5},
            })

            resp = await asyncio.wait_for(ws.receive_json(), timeout=5.0)

            if resp.get("status") != "ok":
                return False, f"Expected status 'ok', got: {resp}"

            if not mock_scroll.called:
                return False, "mouse_scroll was never called"

            args, kwargs = mock_scroll.call_args
            direction = kwargs.get("direction", args[2] if len(args) > 2 else None)
            clicks = kwargs.get("clicks", args[3] if len(args) > 3 else None)

            if direction != "up":
                return False, f"Expected direction='up', got '{direction}'"
            if clicks != 5:
                return False, f"Expected clicks=5, got {clicks}"

            return True, f"mouse_scroll called with direction='up', clicks=5"


async def test_scroll_default_params(mock_scroll: MagicMock) -> tuple[bool, str]:
    """Send SCROLL with no params → defaults to direction='down', clicks=3."""
    mock_scroll.reset_mock()

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(f"ws://localhost:{BRIDGE_PORT}/ws") as ws:
            await ws.send_json({
                "type": "touch_command",
                "id": "test-scroll-3",
                "action": "SCROLL",
                "text": "scroll down",
            })

            resp = await asyncio.wait_for(ws.receive_json(), timeout=5.0)

            if resp.get("status") != "ok":
                return False, f"Expected status 'ok', got: {resp}"

            if not mock_scroll.called:
                return False, "mouse_scroll was never called"

            args, kwargs = mock_scroll.call_args
            direction = kwargs.get("direction", args[2] if len(args) > 2 else None)
            clicks = kwargs.get("clicks", args[3] if len(args) > 3 else None)

            if direction != "down":
                return False, f"Expected direction='down', got '{direction}'"
            if clicks != 3:
                return False, f"Expected clicks=3, got {clicks}"

            return True, f"mouse_scroll defaults: direction='down', clicks=3"


async def test_touch_click_e2e(mock_click: MagicMock) -> tuple[bool, str]:
    """Send touch_command CLICK → verify mouse_click called."""
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(f"ws://localhost:{BRIDGE_PORT}/ws") as ws:
            await ws.send_json({
                "type": "touch_command",
                "id": "test-click-1",
                "action": "CLICK",
                "text": "click",
                "params": {"x": 500, "y": 300, "button": "left"},
            })

            resp = await asyncio.wait_for(ws.receive_json(), timeout=5.0)

            if resp.get("status") != "ok":
                return False, f"Expected status 'ok', got: {resp}"

            if not mock_click.called:
                return False, "mouse_click was never called"

            args, kwargs = mock_click.call_args
            x, y = args[0], args[1]
            button = kwargs.get("button", args[2] if len(args) > 2 else "left")

            if x != 500 or y != 300:
                return False, f"Expected (500, 300), got ({x}, {y})"
            if button != "left":
                return False, f"Expected button='left', got '{button}'"

            return True, f"mouse_click called at (500, 300), button='left'"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def run_tests() -> int:
    print("Starting test bridge on port %d ..." % BRIDGE_PORT)

    # Patch where the functions are looked up: command_executor imports `mouse` module
    # so we patch the functions on that module object.
    import command_executor
    with patch.object(command_executor.mouse, "mouse_scroll") as mock_scroll, \
         patch.object(command_executor.mouse, "mouse_click") as mock_click, \
         patch("pyautogui.position", return_value=(960, 540)):

        # Make mocks return valid dicts (CommandExecutor expects dict returns)
        mock_scroll.return_value = {"scrolled": True, "direction": "down", "clicks": 3}
        mock_click.return_value = {"clicked": True, "x": 500, "y": 300}

        bridge, bridge_task = await start_test_bridge()

        tests = [
            ("Touch SCROLL down end-to-end", lambda: test_scroll_down_e2e(mock_scroll)),
            ("Touch SCROLL up end-to-end", lambda: test_scroll_up_e2e(mock_scroll)),
            ("Touch SCROLL default params", lambda: test_scroll_default_params(mock_scroll)),
            ("Touch CLICK end-to-end", lambda: test_touch_click_e2e(mock_click)),
        ]

        passed = 0
        failed = 0

        print(f"Running {len(tests)} tests...\n")

        for name, test_fn in tests:
            try:
                ok, detail = await test_fn()
                if ok:
                    print(f"  \u2713 {name}")
                    print(f"    {detail}")
                    passed += 1
                else:
                    print(f"  \u2717 {name}")
                    print(f"    {detail}")
                    failed += 1
            except Exception as exc:
                print(f"  \u2717 {name}")
                print(f"    EXCEPTION: {exc}")
                failed += 1

            await asyncio.sleep(0.1)

        print(f"\n{'─' * 50}")
        print(f"Results: {passed} passed, {failed} failed")

        # Cleanup
        bridge_task.cancel()
        try:
            await bridge_task
        except (asyncio.CancelledError, Exception):
            pass

    return 0 if failed == 0 else 1


def main() -> None:
    sys.exit(asyncio.run(run_tests()))


if __name__ == "__main__":
    main()
