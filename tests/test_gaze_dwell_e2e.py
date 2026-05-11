"""Integration test: gaze dwell fires click on desktop target (task 2.11)

Validates the gaze_dwell → click path end-to-end:
  iPad sends {"type":"gaze_dwell","x":0.5,"y":0.25}
  → IPadBridge._handle_message() → FusionEngine.on_gaze_dwell(0.5, 0.25)
  → FusionEngine tick Rule 3 → Command(action="CLICK", source="gaze_dwell",
                                        gaze_coords=(960, 270))
  → HybridCoordinator.route() (bypass path) → CommandExecutor.execute()
  → mouse.mouse_click(960, 270, button="left")

We mock pyautogui and the local inference backend so no real desktop
interaction happens and no GPU/Ollama is required.

Run:
    python tests/test_gaze_dwell_e2e.py

Exit codes:
    0 — all tests passed
    1 — one or more tests failed
"""

from __future__ import annotations

import asyncio
import json
import sys
from unittest.mock import patch, MagicMock, AsyncMock

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

try:
    import aiohttp
except ImportError as exc:
    print(f"ERROR: {exc}. Run: pip install aiohttp")
    sys.exit(1)

from ipad_bridge import IPadBridge
from fusion_engine import FusionEngine, FusionConfig
from hybrid_coordinator import HybridCoordinator, CoordinatorConfig

BRIDGE_PORT = 8768  # Unique port to avoid conflicts with other tests
SCREEN_W = 1920
SCREEN_H = 1080


# ---------------------------------------------------------------------------
# Test harness
# ---------------------------------------------------------------------------

async def start_test_bridge() -> tuple[IPadBridge, FusionEngine, asyncio.Task, asyncio.Task]:
    """Start bridge + FusionEngine + HybridCoordinator wired together."""
    bridge = IPadBridge(port=BRIDGE_PORT)

    # FusionEngine with fast tick for testing
    config = FusionConfig(tick_hz=60.0)
    fusion = FusionEngine(screen_width=SCREEN_W, screen_height=SCREEN_H, config=config)

    # HybridCoordinator with a mock local inference (returns "CLICK" for gaze_dwell)
    mock_local = MagicMock()
    mock_local.infer = AsyncMock(return_value="CLICK")
    mock_local.get_status = MagicMock(return_value={"backend": "mock"})

    coord_config = CoordinatorConfig(agentcore_enabled=False)
    coordinator = HybridCoordinator(local=mock_local, config=coord_config)

    # Wire components
    fusion.set_coordinator(coordinator)
    bridge.set_fusion_engine(fusion)

    # Start bridge and fusion engine
    bridge_task = asyncio.create_task(bridge.run(no_mdns=True))
    fusion_task = asyncio.create_task(fusion.run())

    # Wait for bridge to be ready
    await asyncio.sleep(1.5)
    return bridge, fusion, bridge_task, fusion_task


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

async def test_gaze_dwell_click(mock_click: MagicMock) -> tuple[bool, str]:
    """Send gaze_dwell at (0.5, 0.25) → verify mouse_click at (960, 270)."""
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(f"ws://localhost:{BRIDGE_PORT}/ws") as ws:
            # Send gaze_dwell message (normalized coords)
            await ws.send_json({
                "type": "gaze_dwell",
                "x": 0.5,
                "y": 0.25,
            })

            # Give FusionEngine time to tick and process the command
            await asyncio.sleep(0.5)

            if not mock_click.called:
                return False, "mouse_click was never called"

            args, kwargs = mock_click.call_args
            x, y = args[0], args[1]
            button = kwargs.get("button", args[2] if len(args) > 2 else "left")

            expected_x = int(0.5 * SCREEN_W)   # 960
            expected_y = int(0.25 * SCREEN_H)  # 270

            if x != expected_x or y != expected_y:
                return False, f"Expected ({expected_x}, {expected_y}), got ({x}, {y})"
            if button != "left":
                return False, f"Expected button='left', got '{button}'"

            return True, f"mouse_click called at ({x}, {y}), button='{button}'"


async def test_gaze_dwell_different_coords(mock_click: MagicMock) -> tuple[bool, str]:
    """Send gaze_dwell at (0.75, 0.8) → verify mouse_click at (1440, 864)."""
    mock_click.reset_mock()

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(f"ws://localhost:{BRIDGE_PORT}/ws") as ws:
            await ws.send_json({
                "type": "gaze_dwell",
                "x": 0.75,
                "y": 0.8,
            })

            await asyncio.sleep(0.5)

            if not mock_click.called:
                return False, "mouse_click was never called"

            args, kwargs = mock_click.call_args
            x, y = args[0], args[1]

            expected_x = int(0.75 * SCREEN_W)  # 1440
            expected_y = int(0.8 * SCREEN_H)   # 864

            if x != expected_x or y != expected_y:
                return False, f"Expected ({expected_x}, {expected_y}), got ({x}, {y})"

            return True, f"mouse_click called at ({x}, {y})"


async def test_gaze_dwell_top_left_corner(mock_click: MagicMock) -> tuple[bool, str]:
    """Send gaze_dwell at (0.0, 0.0) → verify mouse_click at (0, 0)."""
    mock_click.reset_mock()

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(f"ws://localhost:{BRIDGE_PORT}/ws") as ws:
            await ws.send_json({
                "type": "gaze_dwell",
                "x": 0.0,
                "y": 0.0,
            })

            await asyncio.sleep(0.5)

            if not mock_click.called:
                return False, "mouse_click was never called"

            args, kwargs = mock_click.call_args
            x, y = args[0], args[1]

            if x != 0 or y != 0:
                return False, f"Expected (0, 0), got ({x}, {y})"

            return True, f"mouse_click called at ({x}, {y}) — top-left corner"


async def test_gaze_dwell_no_fusion_engine(mock_click: MagicMock) -> tuple[bool, str]:
    """Send gaze_dwell when no FusionEngine is wired → no crash, no click."""
    mock_click.reset_mock()

    # Create a separate bridge with no fusion engine on a different approach:
    # We'll just verify the bridge handles it gracefully by checking the main
    # bridge's fusion engine is wired (positive test already covers the path).
    # Instead, test that the message is silently ignored when fusion is None.
    bridge_no_fusion = IPadBridge(port=8769)
    bridge_task = asyncio.create_task(bridge_no_fusion.run(no_mdns=True))
    await asyncio.sleep(1.0)

    try:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(f"ws://localhost:8769/ws") as ws:
                await ws.send_json({
                    "type": "gaze_dwell",
                    "x": 0.5,
                    "y": 0.5,
                })

                await asyncio.sleep(0.3)

                if mock_click.called:
                    return False, "mouse_click should NOT be called when no FusionEngine"

                return True, "gaze_dwell silently ignored when FusionEngine not wired"
    finally:
        bridge_task.cancel()
        try:
            await bridge_task
        except (asyncio.CancelledError, Exception):
            pass


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def run_tests() -> int:
    print("Starting gaze dwell integration test on port %d ..." % BRIDGE_PORT)

    # Patch mouse_click in command_executor (where it's actually called)
    import command_executor
    with patch.object(command_executor.mouse, "mouse_click") as mock_click, \
         patch("pyautogui.position", return_value=(960, 540)), \
         patch("pyautogui.size", return_value=(SCREEN_W, SCREEN_H)):

        # Make mock return a valid dict
        mock_click.return_value = {"clicked": {"x": 0, "y": 0, "button": "left", "clicks": 1}}

        bridge, fusion, bridge_task, fusion_task = await start_test_bridge()

        tests = [
            ("Gaze dwell click at (0.5, 0.25)", lambda: test_gaze_dwell_click(mock_click)),
            ("Gaze dwell click at (0.75, 0.8)", lambda: test_gaze_dwell_different_coords(mock_click)),
            ("Gaze dwell click at (0.0, 0.0)", lambda: test_gaze_dwell_top_left_corner(mock_click)),
            ("Gaze dwell ignored without FusionEngine", lambda: test_gaze_dwell_no_fusion_engine(mock_click)),
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
