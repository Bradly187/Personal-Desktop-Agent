"""Integration test: tilt navigation moves cursor (task 2.9)

Validates FusionEngine rule 6: iPad tilt data → pyautogui.moveRel.

Starts a minimal bridge + FusionEngine (no coordinator needed — tilt
bypasses the LLM entirely), sends simulated tilt messages over WebSocket,
and verifies the cursor position changes.

Run:
    python tests/test_tilt_navigation.py

Exit codes:
    0 — all tests passed
    1 — one or more tests failed
"""

from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

try:
    import aiohttp
    import pyautogui
except ImportError as exc:
    print(f"ERROR: {exc}. Run: pip install aiohttp pyautogui")
    sys.exit(1)

from core.fusion_engine import FusionEngine, FusionConfig
from core.ipad_bridge import IPadBridge

BRIDGE_PORT = 8766  # Use a different port to avoid conflict with running bridge
_TEST_TOKEN = "sprint-n-test-token"  # C1 pairing token for the in-process bridge


# ---------------------------------------------------------------------------
# Test harness — start bridge + fusion engine in background
# ---------------------------------------------------------------------------

async def start_test_server() -> tuple[IPadBridge, FusionEngine, asyncio.Task, asyncio.Task]:
    """Start a minimal bridge + FusionEngine for testing."""
    sw, sh = pyautogui.size()

    # Use a faster tick rate for testing responsiveness
    config = FusionConfig(tick_hz=60.0, tilt_dead_zone=0.02, tilt_sensitivity=300.0)
    fusion = FusionEngine(screen_width=sw, screen_height=sh, config=config)
    # No coordinator needed — tilt goes direct to pyautogui (rule 6)

    bridge = IPadBridge(port=BRIDGE_PORT, token=_TEST_TOKEN)
    bridge.set_fusion_engine(fusion)

    bridge_task = asyncio.create_task(bridge.run(no_mdns=True))
    fusion_task = asyncio.create_task(fusion.run())

    # Wait for server to be ready
    await asyncio.sleep(1.0)
    return bridge, fusion, bridge_task, fusion_task


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

async def test_tilt_moves_cursor_right() -> tuple[bool, str]:
    """Send tilt ry > dead_zone → cursor should move right (positive dx)."""
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(f"ws://localhost:{BRIDGE_PORT}/ws?token={_TEST_TOKEN}") as ws:
            # Record starting position
            start_x, start_y = pyautogui.position()

            # Send tilt: ry=0.1 rad/s (above dead zone of 0.02)
            # Expected dx = int(0.1 * 300) = 30 pixels right
            for _ in range(5):
                await ws.send_json({"type": "tilt", "rx": 0.0, "ry": 0.1})
                await asyncio.sleep(0.02)  # ~50Hz send rate

            # Wait for FusionEngine ticks to process
            await asyncio.sleep(0.2)

            end_x, end_y = pyautogui.position()
            dx = end_x - start_x

            if dx > 0:
                return True, f"cursor moved right by {dx}px (start={start_x}, end={end_x})"
            else:
                return False, f"cursor did NOT move right: dx={dx} (start={start_x}, end={end_x})"


async def test_tilt_moves_cursor_down() -> tuple[bool, str]:
    """Send tilt rx < -dead_zone → cursor should move down (positive dy)."""
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(f"ws://localhost:{BRIDGE_PORT}/ws?token={_TEST_TOKEN}") as ws:
            start_x, start_y = pyautogui.position()

            # Send tilt: rx=-0.1 rad/s → dy = int(-(-0.1) * 300) = 30 pixels down
            for _ in range(5):
                await ws.send_json({"type": "tilt", "rx": -0.1, "ry": 0.0})
                await asyncio.sleep(0.02)

            await asyncio.sleep(0.2)

            end_x, end_y = pyautogui.position()
            dy = end_y - start_y

            if dy > 0:
                return True, f"cursor moved down by {dy}px (start={start_y}, end={end_y})"
            else:
                return False, f"cursor did NOT move down: dy={dy} (start={start_y}, end={end_y})"


async def test_tilt_dead_zone_ignored() -> tuple[bool, str]:
    """Send tilt below dead zone (0.01 < 0.02) → cursor should NOT move."""
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(f"ws://localhost:{BRIDGE_PORT}/ws?token={_TEST_TOKEN}") as ws:
            # Move cursor to a known position first
            pyautogui.moveTo(960, 540)
            await asyncio.sleep(0.1)

            start_x, start_y = pyautogui.position()

            # Send tilt below dead zone
            for _ in range(5):
                await ws.send_json({"type": "tilt", "rx": 0.01, "ry": 0.01})
                await asyncio.sleep(0.02)

            await asyncio.sleep(0.2)

            end_x, end_y = pyautogui.position()
            dx = abs(end_x - start_x)
            dy = abs(end_y - start_y)

            if dx == 0 and dy == 0:
                return True, f"cursor stayed at ({end_x}, {end_y}) — dead zone working"
            else:
                return False, f"cursor moved despite dead zone: dx={dx}, dy={dy}"


async def test_tilt_diagonal() -> tuple[bool, str]:
    """Send tilt with both rx and ry → cursor moves diagonally."""
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(f"ws://localhost:{BRIDGE_PORT}/ws?token={_TEST_TOKEN}") as ws:
            # Move to center so we have room in all directions
            pyautogui.moveTo(960, 540)
            await asyncio.sleep(0.1)

            start_x, start_y = pyautogui.position()

            # ry=0.1 → dx=30 right, rx=-0.1 → dy=30 down
            for _ in range(5):
                await ws.send_json({"type": "tilt", "rx": -0.1, "ry": 0.1})
                await asyncio.sleep(0.02)

            await asyncio.sleep(0.2)

            end_x, end_y = pyautogui.position()
            dx = end_x - start_x
            dy = end_y - start_y

            if dx > 0 and dy > 0:
                return True, f"cursor moved diagonally: dx={dx}, dy={dy}"
            else:
                return False, f"expected positive dx and dy, got dx={dx}, dy={dy}"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def run_tests() -> int:
    print("Starting test bridge + FusionEngine on port %d ..." % BRIDGE_PORT)

    bridge, fusion, bridge_task, fusion_task = await start_test_server()

    tests = [
        ("Tilt moves cursor right (ry > dead_zone)", test_tilt_moves_cursor_right),
        ("Tilt moves cursor down (rx < -dead_zone)", test_tilt_moves_cursor_down),
        ("Tilt below dead zone is ignored", test_tilt_dead_zone_ignored),
        ("Tilt diagonal movement", test_tilt_diagonal),
    ]

    passed = 0
    failed = 0

    print(f"Running {len(tests)} tests...\n")

    for name, test_fn in tests:
        try:
            ok, detail = await test_fn()
            if ok:
                print(f"  ✓ {name}")
                print(f"    {detail}")
                passed += 1
            else:
                print(f"  ✗ {name}")
                print(f"    {detail}")
                failed += 1
        except Exception as exc:
            print(f"  ✗ {name}")
            print(f"    EXCEPTION: {exc}")
            failed += 1

        await asyncio.sleep(0.3)

    print(f"\n{'─' * 50}")
    print(f"Results: {passed} passed, {failed} failed")

    # Cleanup
    fusion.stop()
    bridge_task.cancel()
    fusion_task.cancel()
    try:
        await bridge_task
    except (asyncio.CancelledError, Exception):
        pass
    try:
        await fusion_task
    except (asyncio.CancelledError, Exception):
        pass

    return 0 if failed == 0 else 1


def main() -> None:
    sys.exit(asyncio.run(run_tests()))


if __name__ == "__main__":
    main()
