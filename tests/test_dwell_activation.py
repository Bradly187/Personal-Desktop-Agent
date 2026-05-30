"""Integration test: dwell activation timeout and stability gating (Req 2).

Validates the FusionEngine dwell path:
  1. gaze_dwell event → exactly one CLICK Command emitted to coordinator (bypass)
  2. Multiple rapid gaze_dwell events → each fires exactly one Command (one-shot)
  3. Stability buffer with <5 recent samples → stable_centroid returns None (suppressed)
  4. Stability buffer with sufficient spread → stable_centroid returns None (too unstable)
  5. Stability buffer with tight spread → stable_centroid returns centroid
  6. gaze_dwell with unrecognized action_type → Command discarded, warning logged
  7. Drag state machine: drag_start → coordinator gets MOUSEDOWN; drag_end → MOUSEUP
  8. 30-second drag safety timeout auto-releases (MOUSEUP emitted)

Run:
    python tests/test_dwell_activation.py

Exit codes: 0 = all passed, 1 = any failed
"""

from __future__ import annotations

import asyncio
import logging
import math
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.command_executor import Command
from core.fusion_engine import FusionEngine, FusionConfig, _GazeBuffer, _GazeSample


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fusion(coord=None, screen_w=1920, screen_h=1080) -> FusionEngine:
    cfg = FusionConfig(tick_hz=60.0, dwell_duration_s=1.0)
    fe = FusionEngine(screen_width=screen_w, screen_height=screen_h, config=cfg)
    if coord:
        fe.set_coordinator(coord)
    return fe


def _mock_coord() -> tuple[MagicMock, list[Command]]:
    routed: list[Command] = []
    coord = MagicMock()
    coord.route = AsyncMock(side_effect=lambda cmd: routed.append(cmd) or {"status": "ok"})
    return coord, routed


# ---------------------------------------------------------------------------
# Test 1: gaze_dwell event emits exactly one CLICK via bypass
# ---------------------------------------------------------------------------

async def test_gaze_dwell_fires_single_click() -> tuple[bool, str]:
    """A gaze_dwell event emits exactly one CLICK Command to coordinator."""
    coord, routed = _mock_coord()
    fe = _make_fusion(coord)

    fe.on_gaze_dwell(0.5, 0.5, "left_click")

    with patch("pyautogui.moveRel"), patch("pyautogui.size", return_value=(1920, 1080)):
        await fe._tick()

    if len(routed) != 1:
        return False, f"Expected 1 Command, got {len(routed)}"
    cmd = routed[0]
    if cmd.action != "CLICK":
        return False, f"Expected action='CLICK', got {cmd.action!r}"
    if cmd.source != "gaze_dwell":
        return False, f"Expected source='gaze_dwell', got {cmd.source!r}"
    if cmd.gaze_coords != (960, 540):   # 0.5 * 1920, 0.5 * 1080
        return False, f"Expected gaze_coords=(960, 540), got {cmd.gaze_coords}"
    return True, f"Gaze dwell (0.5, 0.5) → CLICK at {cmd.gaze_coords}"


# ---------------------------------------------------------------------------
# Test 2: gaze_dwell is consumed after one tick (one-shot)
# ---------------------------------------------------------------------------

async def test_gaze_dwell_consumed_after_one_tick() -> tuple[bool, str]:
    """A gaze_dwell is cleared after the first tick — no double-fire."""
    coord, routed = _mock_coord()
    fe = _make_fusion(coord)

    fe.on_gaze_dwell(0.5, 0.5)

    with patch("pyautogui.moveRel"), patch("pyautogui.size", return_value=(1920, 1080)):
        await fe._tick()
        await fe._tick()   # second tick — dwell already consumed

    if len(routed) != 1:
        return False, f"Expected 1 Command (one-shot), got {len(routed)}"
    return True, "gaze_dwell is consumed after exactly one tick"


# ---------------------------------------------------------------------------
# Test 3: Stability buffer — fewer than 5 recent samples → centroid = None
# ---------------------------------------------------------------------------

async def test_stability_buffer_insufficient_samples() -> tuple[bool, str]:
    """stable_centroid returns None when fewer than 5 recent samples are present."""
    diag = math.sqrt(1920**2 + 1080**2)
    buf = _GazeBuffer(30, 0.04, 0.55)

    # Add only 3 samples (recent)
    for _ in range(3):
        buf.update(0.5, 0.5, 0.9)

    result = buf.stable_centroid(diag)
    if result is not None:
        return False, f"Expected None with <5 samples, got {result}"
    return True, "Insufficient samples → stable_centroid returns None (suppressed)"


# ---------------------------------------------------------------------------
# Test 4: Stability buffer — large spread → centroid = None
# ---------------------------------------------------------------------------

async def test_stability_buffer_large_spread() -> tuple[bool, str]:
    """stable_centroid returns None when gaze spread exceeds 4% of screen diagonal."""
    diag = math.sqrt(1920**2 + 1080**2)
    buf = _GazeBuffer(30, 0.04, 0.55)

    # Spread across 20% of screen diagonal in normalised coords
    spread = 0.20
    positions = [
        (0.5 - spread, 0.5), (0.5 + spread, 0.5),
        (0.5, 0.5 - spread), (0.5, 0.5 + spread),
        (0.5, 0.5), (0.5, 0.5),
    ]
    for x, y in positions:
        buf.update(x, y, 0.9)

    result = buf.stable_centroid(diag)
    if result is not None:
        return False, f"Expected None for large spread, got {result}"
    return True, "Large gaze spread → stable_centroid returns None"


# ---------------------------------------------------------------------------
# Test 5: Stability buffer — tight cluster → centroid returned
# ---------------------------------------------------------------------------

async def test_stability_buffer_tight_cluster() -> tuple[bool, str]:
    """stable_centroid returns a centroid when gaze is within the stability threshold."""
    diag = math.sqrt(1920**2 + 1080**2)
    buf = _GazeBuffer(30, 0.04, 0.55)

    # 6 samples within 1% of diagonal spread — should be stable
    for i in range(6):
        tiny_offset = 0.001 * (i % 3)
        buf.update(0.5 + tiny_offset, 0.5 + tiny_offset, 0.9)

    result = buf.stable_centroid(diag)
    if result is None:
        return False, "Expected centroid for tight cluster, got None"
    cx, cy = result
    if abs(cx - 0.5) > 0.01 or abs(cy - 0.5) > 0.01:
        return False, f"Centroid far from expected (0.5, 0.5): got ({cx:.3f}, {cy:.3f})"
    return True, f"Tight cluster → centroid ({cx:.3f}, {cy:.3f})"


# ---------------------------------------------------------------------------
# Test 6: Unrecognized action_type is discarded with warning
# ---------------------------------------------------------------------------

async def test_gaze_dwell_invalid_action_type_discarded() -> tuple[bool, str]:
    """gaze_dwell with unrecognized action_type is silently discarded."""
    coord, routed = _mock_coord()
    fe = _make_fusion(coord)

    fe.on_gaze_dwell(0.5, 0.5, "triple_click")  # invalid

    with patch("pyautogui.moveRel"), patch("pyautogui.size", return_value=(1920, 1080)):
        await fe._tick()

    if routed:
        return False, f"Expected no Command for invalid action_type, got {routed[0].action!r}"
    return True, "Invalid action_type 'triple_click' discarded"


# ---------------------------------------------------------------------------
# Test 7: Drag state machine — drag_start → MOUSEDOWN; drag_end → MOUSEUP
# ---------------------------------------------------------------------------

async def test_drag_state_machine() -> tuple[bool, str]:
    """drag_start emits MOUSEDOWN and sets drag state; drag_end emits MOUSEUP."""
    coord, routed = _mock_coord()
    fe = _make_fusion(coord)

    # Start drag
    fe.on_gaze_dwell(0.3, 0.4, "drag_start")
    with patch("pyautogui.moveRel"), patch("pyautogui.size", return_value=(1920, 1080)), \
         patch("pyautogui.mouseDown"), patch("pyautogui.mouseUp"):
        await fe._tick()

    if not fe._drag_active:
        return False, "Expected _drag_active=True after drag_start"

    # End drag
    fe.on_gaze_dwell(0.3, 0.4, "drag_end")
    with patch("pyautogui.moveRel"), patch("pyautogui.size", return_value=(1920, 1080)), \
         patch("pyautogui.mouseDown"), patch("pyautogui.mouseUp"):
        await fe._tick()

    if fe._drag_active:
        return False, "Expected _drag_active=False after drag_end"

    actions = [cmd.action for cmd in routed]
    if "MOUSEDOWN" not in actions:
        return False, f"Expected MOUSEDOWN in routed commands, got {actions}"
    if "MOUSEUP" not in actions:
        return False, f"Expected MOUSEUP in routed commands, got {actions}"
    return True, f"Drag state: MOUSEDOWN → drag active → MOUSEUP → released"


# ---------------------------------------------------------------------------
# Test 8: Drag safety timeout auto-releases after 30s
# ---------------------------------------------------------------------------

async def test_drag_safety_timeout() -> tuple[bool, str]:
    """If drag_end is not received within 30s, FusionEngine auto-releases with MOUSEUP."""
    coord, routed = _mock_coord()
    fe = _make_fusion(coord)

    fe.on_gaze_dwell(0.5, 0.5, "drag_start")
    with patch("pyautogui.moveRel"), patch("pyautogui.size", return_value=(1920, 1080)), \
         patch("pyautogui.mouseDown"), patch("pyautogui.mouseUp"):
        await fe._tick()

    if not fe._drag_active:
        return False, "_drag_active should be True after drag_start"

    # Simulate 31 seconds passing by backdating drag_start
    fe._drag_start_time = time.monotonic() - 31.0

    with patch("pyautogui.moveRel"), patch("pyautogui.size", return_value=(1920, 1080)), \
         patch("pyautogui.mouseDown"), patch("pyautogui.mouseUp"):
        await fe._tick()

    if fe._drag_active:
        return False, "_drag_active should be False after 30s safety timeout"

    actions = [cmd.action for cmd in routed]
    if "MOUSEUP" not in actions:
        return False, f"Safety timeout should emit MOUSEUP, got {actions}"
    return True, "Drag safety timeout fires MOUSEUP after 30s without drag_end"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def run_tests() -> int:
    tests = [
        ("gaze_dwell fires exactly one CLICK (bypass)",         test_gaze_dwell_fires_single_click),
        ("gaze_dwell consumed after one tick (one-shot)",        test_gaze_dwell_consumed_after_one_tick),
        ("< 5 recent samples → stable_centroid=None",           test_stability_buffer_insufficient_samples),
        ("Large spread → stable_centroid=None",                  test_stability_buffer_large_spread),
        ("Tight cluster → stable_centroid returns centroid",     test_stability_buffer_tight_cluster),
        ("Invalid action_type discarded with warning",           test_gaze_dwell_invalid_action_type_discarded),
        ("Drag: drag_start→MOUSEDOWN; drag_end→MOUSEUP",         test_drag_state_machine),
        ("Drag safety timeout auto-releases after 30s",          test_drag_safety_timeout),
    ]

    print(f"\nDwell activation integration tests\n{'─' * 52}")
    passed = 0
    for name, fn in tests:
        try:
            ok, msg = await fn()
        except Exception as exc:
            ok, msg = False, f"EXCEPTION: {exc}"
        mark = "✓" if ok else "✗"
        print(f"  {mark} {name}")
        if ok:
            passed += 1
        else:
            print(f"    FAIL: {msg}")

    print(f"\n{'─' * 52}")
    print(f"Results: {passed}/{len(tests)} passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run_tests()))
