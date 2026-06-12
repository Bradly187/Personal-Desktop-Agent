"""E2E bridge tests for gesture + LiDAR pipeline (7 tests).

Tests GBE-01 through GBE-07 as specified in the test plan.
Spins up a real IPadBridge + FusionEngine + HybridCoordinator on port 8770.
GestureProcessor is mocked in most tests to control gesture output without
requiring real MediaPipe or camera images.

Run standalone:
    python tests/test_gesture_bridge_e2e.py

Exit codes:
    0 — all tests passed
    1 — one or more tests failed
"""

from __future__ import annotations

import asyncio
import json
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest.mock import MagicMock, AsyncMock, patch

try:
    import aiohttp
except ImportError as exc:
    print(f"ERROR: {exc}. Run: pip install aiohttp")
    sys.exit(1)

import core.command_executor
from core.ipad_bridge import IPadBridge
from core.fusion_engine import FusionEngine, FusionConfig
from core.hybrid_coordinator import HybridCoordinator, CoordinatorConfig
from core.command_executor import Command
from tests.conftest import make_depth_msg, make_camera_msg, make_lm_mock, make_hands_result

BRIDGE_PORT = 8770
_TEST_TOKEN = "sprint-n-test-token"  # C1 pairing token for the in-process bridge


# ---------------------------------------------------------------------------
# Test harness helpers
# ---------------------------------------------------------------------------

async def start_gesture_bridge(
    mock_gesture_result=None,
    infer_return: str = "CLICK",
):
    """Start bridge + FusionEngine + mocked GestureProcessor.

    mock_gesture_result: Command or None returned by mock GestureProcessor.
    infer_return: string the mock LLM returns (verb that CommandExecutor will run).
    """
    bridge = IPadBridge(port=BRIDGE_PORT, token=_TEST_TOKEN)

    mock_local = MagicMock()
    mock_local.infer = AsyncMock(return_value=infer_return)
    mock_local.get_status = MagicMock(return_value={"backend": "mock"})

    coord_config = CoordinatorConfig(
        vram_free_min_gb=0.0,
        latency_budget_ms=99999.0,
    )
    coordinator = HybridCoordinator(local=mock_local, config=coord_config)

    config = FusionConfig(tick_hz=60.0)
    fusion = FusionEngine(screen_width=1920, screen_height=1080, config=config)
    fusion.set_coordinator(coordinator)

    mock_gesture_proc = MagicMock()
    mock_gesture_proc.on_camera_frame = MagicMock(return_value=mock_gesture_result)

    bridge.set_fusion_engine(fusion)
    bridge.set_gesture_processor(mock_gesture_proc)

    bridge_task = asyncio.create_task(bridge.run(no_mdns=True))
    fusion_task = asyncio.create_task(fusion.run())
    await asyncio.sleep(1.5)
    return bridge, fusion, mock_gesture_proc, bridge_task, fusion_task


async def stop_bridge(bridge_task, fusion_task):
    bridge_task.cancel()
    fusion_task.cancel()
    await asyncio.gather(bridge_task, fusion_task, return_exceptions=True)


# ---------------------------------------------------------------------------
# GBE-01 — camera_frame POINT → mouse_click called
# ---------------------------------------------------------------------------

async def test_gbe01_point_gesture_fires_click():
    gesture_cmd = Command(
        text="gesture:POINT",
        action="CLICK",
        source="gesture",
        gesture_confidence=0.90,
        gaze_coords=None,
        params={"gesture": "POINT", "tip_x": 0.5, "tip_y": 0.5, "hand": "Right", "pinch_mm": None},
    )
    mock_click = MagicMock(return_value={"ok": True})

    with patch.object(command_executor.mouse, "mouse_click", mock_click), \
         patch("pyautogui.position", return_value=(960, 540)), \
         patch("pyautogui.size", return_value=(1920, 1080)):

        bridge, fusion, mock_gp, bt, ft = await start_gesture_bridge(mock_gesture_result=gesture_cmd)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(f"ws://localhost:{BRIDGE_PORT}/ws?token={_TEST_TOKEN}") as ws:
                    await ws.send_json(make_camera_msg())
                    await asyncio.sleep(0.5)

            if not mock_click.called:
                return False, "mouse_click was never called for POINT gesture"
            return True, "GBE-01 passed"
        finally:
            await stop_bridge(bt, ft)


# ---------------------------------------------------------------------------
# GBE-02 — camera_frame, GestureProcessor returns None → no click
# ---------------------------------------------------------------------------

async def test_gbe02_no_gesture_no_click():
    mock_click = MagicMock(return_value={"ok": True})

    with patch.object(command_executor.mouse, "mouse_click", mock_click), \
         patch("pyautogui.position", return_value=(960, 540)):

        bridge, fusion, mock_gp, bt, ft = await start_gesture_bridge(mock_gesture_result=None)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(f"ws://localhost:{BRIDGE_PORT}/ws?token={_TEST_TOKEN}") as ws:
                    await ws.send_json(make_camera_msg())
                    await asyncio.sleep(0.5)

            if mock_click.called:
                return False, "mouse_click should NOT have been called when gesture=None"
            return True, "GBE-02 passed"
        finally:
            await stop_bridge(bt, ft)


# ---------------------------------------------------------------------------
# GBE-03 — depth_frame then camera_frame with real LiDAR+Gesture → click (20mm)
# ---------------------------------------------------------------------------

def _make_pinch_lm_e2e():
    """PINCH landmark: thumb extended (y=0.488 < MCP 0.5), index at MCP level.
    Close in x so 2D dist < 0.06. n_ext=1 → skips FIST; index_ext=False → skips POINT."""
    return make_lm_mock(
        {4: 0.488, 8: 0.5, 12: 0.7, 16: 0.7, 20: 0.7},
        {4: 0.49, 8: 0.51},
    )


def _build_pinch_depth(index_depth, thumb_depth, h=192, w=256, default=1.5):
    """Build depth map with 2x2 blocks at fingertip pixel positions.
    index at nx=0.51, ny=0.5; thumb at nx=0.49, ny=0.488.
    2x2 blocks ensure bilinear interpolation returns the exact target depth.
    """
    import numpy as np
    depth = np.full((h, w), default, dtype=np.float32)

    def set_block(nx, ny, val):
        px, py = int(nx * (w - 1)), int(ny * (h - 1))
        for dy in range(2):
            for dx in range(2):
                depth[min(py + dy, h - 1), min(px + dx, w - 1)] = val

    set_block(0.51, 0.5, index_depth)
    set_block(0.49, 0.488, thumb_depth)
    return depth


async def _start_lidar_gesture_bridge(infer_return="CLICK"):
    """Shared bridge setup for GBE-03/04: real LiDAR + real GestureProcessor."""
    from sensors.lidar_receiver import LiDARReceiver
    from sensors.gesture_processor import GestureProcessor

    bridge = IPadBridge(port=BRIDGE_PORT, token=_TEST_TOKEN)
    mock_local = MagicMock()
    mock_local.infer = AsyncMock(return_value=infer_return)
    mock_local.get_status = MagicMock(return_value={"backend": "mock"})
    coord_config = CoordinatorConfig(
        vram_free_min_gb=0.0, latency_budget_ms=99999.0
    )
    coordinator = HybridCoordinator(local=mock_local, config=coord_config)
    config = FusionConfig(tick_hz=60.0)
    fusion = FusionEngine(screen_width=1920, screen_height=1080, config=config)
    fusion.set_coordinator(coordinator)

    lidar = LiDARReceiver(conf_min=0)
    proc = GestureProcessor()
    proc.set_lidar(lidar)
    proc._available = True

    result_mock = make_hands_result(_make_pinch_lm_e2e(), score=0.90)
    mock_hands = MagicMock()
    mock_hands.process.return_value = result_mock
    proc._hands = mock_hands

    bridge.set_fusion_engine(fusion)
    bridge.set_lidar(lidar)
    bridge.set_gesture_processor(proc)

    bridge_task = asyncio.create_task(bridge.run(no_mdns=True))
    fusion_task = asyncio.create_task(fusion.run())
    await asyncio.sleep(1.5)
    return bridge, fusion, lidar, bridge_task, fusion_task


async def test_gbe03_real_lidar_pinch_accept():
    mock_click = MagicMock(return_value={"ok": True})
    bridge, fusion, lidar, bt, ft = await _start_lidar_gesture_bridge()
    try:
        with patch.object(command_executor.mouse, "mouse_click", mock_click), \
             patch("pyautogui.position", return_value=(960, 540)), \
             patch("pyautogui.size", return_value=(1920, 1080)):

            depth = _build_pinch_depth(index_depth=1.000, thumb_depth=1.020)  # 20mm
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(f"ws://localhost:{BRIDGE_PORT}/ws?token={_TEST_TOKEN}") as ws:
                    await ws.send_json(make_depth_msg(depth_array=depth, conf_array=None))
                    await asyncio.sleep(0.05)
                    await ws.send_json(make_camera_msg())
                    await asyncio.sleep(0.5)

        if not mock_click.called:
            return False, "mouse_click not called for 20mm LiDAR PINCH"
        return True, "GBE-03 passed"
    finally:
        await stop_bridge(bt, ft)


# ---------------------------------------------------------------------------
# GBE-04 — same as GBE-03 but 60mm diff → no click
# ---------------------------------------------------------------------------

async def test_gbe04_real_lidar_pinch_reject():
    mock_click = MagicMock(return_value={"ok": True})
    bridge, fusion, lidar, bt, ft = await _start_lidar_gesture_bridge()
    try:
        with patch.object(command_executor.mouse, "mouse_click", mock_click), \
             patch("pyautogui.position", return_value=(960, 540)):

            depth = _build_pinch_depth(index_depth=1.000, thumb_depth=1.060)  # 60mm
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(f"ws://localhost:{BRIDGE_PORT}/ws?token={_TEST_TOKEN}") as ws:
                    await ws.send_json(make_depth_msg(depth_array=depth, conf_array=None))
                    await asyncio.sleep(0.05)
                    await ws.send_json(make_camera_msg())
                    await asyncio.sleep(0.5)

        if mock_click.called:
            return False, "mouse_click should NOT fire for 60mm LiDAR PINCH rejection"
        return True, "GBE-04 passed"
    finally:
        await stop_bridge(bt, ft)


# ---------------------------------------------------------------------------
# GBE-05 — camera_frame with no GestureProcessor wired → no crash
# ---------------------------------------------------------------------------

async def test_gbe05_no_gesture_proc_no_crash():
    bridge = IPadBridge(port=BRIDGE_PORT, token=_TEST_TOKEN)
    mock_local = MagicMock()
    mock_local.infer = AsyncMock(return_value="CLICK")
    mock_local.get_status = MagicMock(return_value={"backend": "mock"})
    coord_config = CoordinatorConfig(vram_free_min_gb=0.0)
    coordinator = HybridCoordinator(local=mock_local, config=coord_config)
    config = FusionConfig(tick_hz=60.0)
    fusion = FusionEngine(screen_width=1920, screen_height=1080, config=config)
    fusion.set_coordinator(coordinator)
    bridge.set_fusion_engine(fusion)
    # Intentionally NOT calling bridge.set_gesture_processor(...)

    bridge_task = asyncio.create_task(bridge.run(no_mdns=True))
    fusion_task = asyncio.create_task(fusion.run())
    await asyncio.sleep(1.5)

    try:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(f"ws://localhost:{BRIDGE_PORT}/ws?token={_TEST_TOKEN}") as ws:
                await ws.send_json(make_camera_msg())
                await asyncio.sleep(0.3)
        # If we reach here without exception, the test passes
        return True, "GBE-05 passed — no crash when GestureProcessor not wired"
    except Exception as exc:
        return False, f"Unexpected exception: {exc}"
    finally:
        bridge_task.cancel()
        fusion_task.cancel()
        await asyncio.gather(bridge_task, fusion_task, return_exceptions=True)


# ---------------------------------------------------------------------------
# GBE-06 — depth_frame with no LiDAR wired → no crash
# ---------------------------------------------------------------------------

async def test_gbe06_no_lidar_wired_no_crash():
    bridge = IPadBridge(port=BRIDGE_PORT, token=_TEST_TOKEN)
    mock_local = MagicMock()
    mock_local.infer = AsyncMock(return_value="CLICK")
    mock_local.get_status = MagicMock(return_value={"backend": "mock"})
    coord_config = CoordinatorConfig(vram_free_min_gb=0.0)
    coordinator = HybridCoordinator(local=mock_local, config=coord_config)
    config = FusionConfig(tick_hz=60.0)
    fusion = FusionEngine(screen_width=1920, screen_height=1080, config=config)
    fusion.set_coordinator(coordinator)
    bridge.set_fusion_engine(fusion)
    # Intentionally NOT calling bridge.set_lidar(...)

    bridge_task = asyncio.create_task(bridge.run(no_mdns=True))
    fusion_task = asyncio.create_task(fusion.run())
    await asyncio.sleep(1.5)

    try:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(f"ws://localhost:{BRIDGE_PORT}/ws?token={_TEST_TOKEN}") as ws:
                await ws.send_json(make_depth_msg())
                await asyncio.sleep(0.3)
        assert bridge._lidar is None
        return True, "GBE-06 passed — no crash when LiDAR not wired"
    except Exception as exc:
        return False, f"Unexpected exception: {exc}"
    finally:
        bridge_task.cancel()
        fusion_task.cancel()
        await asyncio.gather(bridge_task, fusion_task, return_exceptions=True)


# ---------------------------------------------------------------------------
# GBE-07 — FIST Command → keyboard_hotkey("alt","f4") called
# ---------------------------------------------------------------------------

async def test_gbe07_fist_produces_close():
    gesture_cmd = Command(
        text="gesture:FIST",
        action="CLOSE",
        source="gesture",
        gesture_confidence=0.88,
        gaze_coords=None,
        params={"gesture": "FIST", "tip_x": 0.5, "tip_y": 0.5, "hand": "Right", "pinch_mm": None},
    )
    mock_hotkey = MagicMock(return_value={"ok": True})

    # The coordinator routes gesture Commands through the LLM, uses the returned
    # string as the verb. Pass "CLOSE" so CommandExecutor runs keyboard_hotkey("alt","f4").
    with patch.object(command_executor.keyboard, "keyboard_hotkey", mock_hotkey), \
         patch("pyautogui.position", return_value=(960, 540)):

        bridge, fusion, mock_gp, bt, ft = await start_gesture_bridge(
            mock_gesture_result=gesture_cmd,
            infer_return="CLOSE",
        )
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(f"ws://localhost:{BRIDGE_PORT}/ws?token={_TEST_TOKEN}") as ws:
                    await ws.send_json(make_camera_msg())
                    await asyncio.sleep(0.5)

            if not mock_hotkey.called:
                return False, "keyboard_hotkey was not called for FIST/CLOSE gesture"
            args, _ = mock_hotkey.call_args
            if list(args) != ["alt", "f4"]:
                return False, f"Expected hotkey('alt','f4'), got {args}"
            return True, "GBE-07 passed"
        finally:
            await stop_bridge(bt, ft)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def _run_all():
    tests = [
        ("GBE-01 POINT->click", test_gbe01_point_gesture_fires_click),
        ("GBE-02 no gesture->no click", test_gbe02_no_gesture_no_click),
        ("GBE-03 real LiDAR 20mm->click", test_gbe03_real_lidar_pinch_accept),
        ("GBE-04 real LiDAR 60mm->no click", test_gbe04_real_lidar_pinch_reject),
        ("GBE-05 no GestureProcessor->no crash", test_gbe05_no_gesture_proc_no_crash),
        ("GBE-06 no LiDAR->no crash", test_gbe06_no_lidar_wired_no_crash),
        ("GBE-07 FIST->CLOSE", test_gbe07_fist_produces_close),
    ]

    passed = 0
    failed = 0
    for name, fn in tests:
        try:
            ok, msg = await fn()
            status = "PASS" if ok else "FAIL"
            if ok:
                passed += 1
            else:
                failed += 1
            print(f"  [{status}] {name}: {msg}")
        except Exception as exc:
            failed += 1
            print(f"  [FAIL] {name}: exception — {exc}")
        # Brief pause between tests so ports can release
        await asyncio.sleep(0.5)

    print(f"\n{passed} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    ok = asyncio.run(_run_all())
    sys.exit(0 if ok else 1)
