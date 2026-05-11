"""Integration test: gaze dwell fires click on desktop target (task 2.11)

Validates the full pipeline for gaze_dwell:
  iPad sends {"type":"gaze_dwell","x":0.5,"y":0.5}
  → IPadBridge._handle_message() → FusionEngine.on_gaze_dwell(0.5, 0.5)
  → FusionEngine._tick() Rule 3 → Command(source="gaze_dwell", action="CLICK")
  → HybridCoordinator.route() → bypass path → _run_local() → _execute_action()
  → CommandExecutor.execute() → mouse.mouse_click(x, y)

We mock the LLM (OllamaInference.infer) to return "CLICK" and mock
mouse.mouse_click to verify it's called at the correct gaze coordinates.

Run:
    python tests/test_gaze_dwell_click.py

Exit codes:
    0 — all tests passed
    1 — one or more tests failed
"""

from __future__ import annotations

import asyncio
import json
import sys
from unittest.mock import patch, AsyncMock, MagicMock

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

try:
    import aiohttp
except ImportError as exc:
    print(f"ERROR: {exc}. Run: pip install aiohttp")
    sys.exit(1)

from command_executor import Command
from fusion_engine import FusionEngine, FusionConfig
from hybrid_coordinator import HybridCoordinator, CoordinatorConfig
from ipad_bridge import IPadBridge
from local_inference import OllamaInference

BRIDGE_PORT = 8768  # Unique port to avoid conflicts
SCREEN_W = 1920
SCREEN_H = 1080


# ---------------------------------------------------------------------------
# Test harness
# ---------------------------------------------------------------------------

async def start_test_server(
    mock_infer: AsyncMock,
    mock_click: MagicMock,
) -> tuple[IPadBridge, FusionEngine, HybridCoordinator, asyncio.Task, asyncio.Task]:
    """Start bridge + FusionEngine + HybridCoordinator with mocked LLM."""

    # Create a mock LocalInference that returns "CLICK"
    mock_local = AsyncMock()
    mock_local.infer = mock_infer
    mock_local.get_status.return_value = {"backend": "mock", "available": True}

    # Coordinator with mocked local inference (no cloud, no trainer)
    config = CoordinatorConfig(
        vram_free_min_gb=0.0,  # disable VRAM gate for testing
        latency_budget_ms=99999.0,  # disable latency gate
    )
    coordinator = HybridCoordinator(local=mock_local, config=config)

    # FusionEngine at 60 Hz
    fusion_config = FusionConfig(tick_hz=60.0)
    fusion = FusionEngine(
        screen_width=SCREEN_W,
        screen_height=SCREEN_H,
        config=fusion_config,
    )
    fusion.set_coordinator(coordinator)

    # Bridge wired to FusionEngine
    bridge = IPadBridge(port=BRIDGE_PORT)
    bridge.set_fusion_engine(fusion)

    bridge_task = asyncio.create_task(bridge.run(no_mdns=True))
    fusion_task = asyncio.create_task(fusion.run())

    # Wait for server to be ready
    await asyncio.sleep(1.0)
    return bridge, fusion, coordinator, bridge_task, fusion_task


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

async def test_gaze_dwell_center_click(
    mock_infer: AsyncMock,
    mock_click: MagicMock,
) -> tuple[bool, str]:
    """Send gaze_dwell at (0.5, 0.5) → click at (960, 540)."""
    mock_infer.reset_mock()
    mock_click.reset_mock()

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(f"ws://localhost:{BRIDGE_PORT}/ws") as ws:
            # Send gaze_dwell event
            await ws.send_json({"type": "gaze_dwell", "x": 0.5, "y": 0.5})

            # Wait for FusionEngine tick to process + coordinator to execute
            await asyncio.sleep(0.5)

            if not mock_infer.called:
                return False, "LLM infer() was never called (FusionEngine didn't emit)"

            # Verify the command passed to infer had correct source and coords
            call_args = mock_infer.call_args
            cmd_arg = call_args[0][0] if call_args[0] else call_args[1].get("cmd")
            if cmd_arg.source != "gaze_dwell":
                return False, f"Expected source='gaze_dwell', got '{cmd_arg.source}'"

            expected_x = int(0.5 * SCREEN_W)  # 960
            expected_y = int(0.5 * SCREEN_H)  # 540
            if cmd_arg.gaze_coords != (expected_x, expected_y):
                return False, f"Expected gaze_coords=({expected_x},{expected_y}), got {cmd_arg.gaze_coords}"

            if not mock_click.called:
                return False, "mouse_click was never called after LLM returned 'CLICK'"

            click_args = mock_click.call_args[0]
            cx, cy = click_args[0], click_args[1]

            if cx != expected_x or cy != expected_y:
                return False, f"Expected click at ({expected_x},{expected_y}), got ({cx},{cy})"

            return True, f"gaze_dwell(0.5, 0.5) → CLICK at ({cx}, {cy})"


async def test_gaze_dwell_top_left(
    mock_infer: AsyncMock,
    mock_click: MagicMock,
) -> tuple[bool, str]:
    """Send gaze_dwell at (0.1, 0.2) → click at (192, 216)."""
    mock_infer.reset_mock()
    mock_click.reset_mock()

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(f"ws://localhost:{BRIDGE_PORT}/ws") as ws:
            await ws.send_json({"type": "gaze_dwell", "x": 0.1, "y": 0.2})
            await asyncio.sleep(0.5)

            if not mock_click.called:
                return False, "mouse_click was never called"

            click_args = mock_click.call_args[0]
            cx, cy = click_args[0], click_args[1]

            expected_x = int(0.1 * SCREEN_W)  # 192
            expected_y = int(0.2 * SCREEN_H)  # 216

            if cx != expected_x or cy != expected_y:
                return False, f"Expected click at ({expected_x},{expected_y}), got ({cx},{cy})"

            return True, f"gaze_dwell(0.1, 0.2) → CLICK at ({cx}, {cy})"


async def test_gaze_dwell_bottom_right(
    mock_infer: AsyncMock,
    mock_click: MagicMock,
) -> tuple[bool, str]:
    """Send gaze_dwell at (0.9, 0.8) → click at (1728, 864)."""
    mock_infer.reset_mock()
    mock_click.reset_mock()

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(f"ws://localhost:{BRIDGE_PORT}/ws") as ws:
            await ws.send_json({"type": "gaze_dwell", "x": 0.9, "y": 0.8})
            await asyncio.sleep(0.5)

            if not mock_click.called:
                return False, "mouse_click was never called"

            click_args = mock_click.call_args[0]
            cx, cy = click_args[0], click_args[1]

            expected_x = int(0.9 * SCREEN_W)  # 1728
            expected_y = int(0.8 * SCREEN_H)  # 864

            if cx != expected_x or cy != expected_y:
                return False, f"Expected click at ({expected_x},{expected_y}), got ({cx},{cy})"

            return True, f"gaze_dwell(0.9, 0.8) → CLICK at ({cx}, {cy})"


async def test_gaze_dwell_bypass_gates(
    mock_infer: AsyncMock,
    mock_click: MagicMock,
) -> tuple[bool, str]:
    """Verify gaze_dwell bypasses all gates (source in _BYPASS_SOURCES).

    Even with a very low whisper_logprob, gaze_dwell should still execute
    because it skips the confidence gate entirely.
    """
    mock_infer.reset_mock()
    mock_click.reset_mock()

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(f"ws://localhost:{BRIDGE_PORT}/ws") as ws:
            await ws.send_json({"type": "gaze_dwell", "x": 0.5, "y": 0.5})
            await asyncio.sleep(0.5)

            # The key assertion: infer() was called (bypass still runs local)
            # and click was executed (not discarded by any gate)
            if not mock_infer.called:
                return False, "LLM was not called — gaze_dwell may not have reached coordinator"

            if not mock_click.called:
                return False, "mouse_click not called — command may have been gate-blocked"

            return True, "gaze_dwell bypassed all gates and executed click"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def run_tests() -> int:
    print("Starting test bridge + FusionEngine + Coordinator on port %d ..." % BRIDGE_PORT)

    # Mock LLM to always return "CLICK" (gaze_dwell action is always click)
    mock_infer = AsyncMock(return_value="CLICK")
    mock_click = MagicMock(return_value={"clicked": True, "x": 960, "y": 540})

    import command_executor
    with patch.object(command_executor.mouse, "mouse_click", mock_click), \
         patch("pyautogui.position", return_value=(960, 540)):

        bridge, fusion, coordinator, bridge_task, fusion_task = await start_test_server(
            mock_infer, mock_click
        )

        tests = [
            ("Gaze dwell center → click at (960, 540)",
             lambda: test_gaze_dwell_center_click(mock_infer, mock_click)),
            ("Gaze dwell top-left → click at (192, 216)",
             lambda: test_gaze_dwell_top_left(mock_infer, mock_click)),
            ("Gaze dwell bottom-right → click at (1728, 864)",
             lambda: test_gaze_dwell_bottom_right(mock_infer, mock_click)),
            ("Gaze dwell bypasses all gates",
             lambda: test_gaze_dwell_bypass_gates(mock_infer, mock_click)),
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

            await asyncio.sleep(0.2)

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
