"""Property-based test: Invalid action type rejection (Property 3).

Validates that FusionEngine discards gaze_dwell events with unrecognized
action_type strings and logs a warning — no Command is emitted.

**Validates: Requirements 2.7**

Run:
    python -m pytest tests/test_prop_invalid_action.py -v
"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

from hypothesis import given, settings, assume
from hypothesis import strategies as st

from command_executor import Command
from fusion_engine import FusionEngine, FusionConfig

SCREEN_W = 1920
SCREEN_H = 1080

VALID_DWELL_ACTIONS = {"left_click", "right_click", "double_click", "drag_start", "drag_end"}


def _make_engine() -> FusionEngine:
    """Create a FusionEngine with a mocked coordinator for capturing emitted Commands."""
    engine = FusionEngine(screen_width=SCREEN_W, screen_height=SCREEN_H)
    mock_coordinator = AsyncMock()
    mock_coordinator.route = AsyncMock()
    engine.set_coordinator(mock_coordinator)
    return engine


async def _tick_and_capture(engine: FusionEngine) -> Command | None:
    """Run one tick and return the Command passed to coordinator.route(), or None."""
    with patch("pyautogui.moveRel"), patch("pyautogui.position", return_value=(960, 540)):
        await engine._tick()
    coordinator = engine._coordinator
    if coordinator.route.called:
        return coordinator.route.call_args[0][0]
    return None


class TestInvalidActionTypeRejection:
    """Property 3: Invalid action type rejection.

    For any string not in the set {"left_click", "right_click", "double_click",
    "drag_start", "drag_end"}, when received as an action_type in a gaze_dwell
    event, the FusionEngine SHALL emit no Command and SHALL log a warning.

    **Validates: Requirements 2.7**
    """

    @given(
        action_type=st.text().filter(lambda s: s not in VALID_DWELL_ACTIONS),
    )
    @settings(max_examples=200)
    def test_invalid_action_type_emits_no_command(self, action_type: str):
        """No Command is emitted for any invalid action_type string."""
        engine = _make_engine()
        engine.on_gaze_dwell(0.5, 0.5, action_type)
        cmd = asyncio.run(_tick_and_capture(engine))
        assert cmd is None, (
            f"Expected no Command for invalid action_type={action_type!r}, "
            f"but got Command(action={cmd.action!r})"
        )

    @given(
        action_type=st.text().filter(lambda s: s not in VALID_DWELL_ACTIONS),
    )
    @settings(max_examples=200)
    def test_invalid_action_type_logs_warning(self, action_type: str):
        """A warning is logged containing the invalid action_type value."""
        engine = _make_engine()

        # Use a fresh handler per iteration to avoid fixture-scope issues
        logger = logging.getLogger("fusion_engine")
        handler = logging.handlers.MemoryHandler(capacity=100)
        handler.setLevel(logging.WARNING)
        logger.addHandler(handler)
        logger.setLevel(logging.WARNING)

        try:
            engine.on_gaze_dwell(0.5, 0.5, action_type)
            asyncio.run(_tick_and_capture(engine))

            # Flush buffered records
            handler.flush()
            records = handler.buffer

            warning_records = [
                r for r in records
                if r.levelno >= logging.WARNING and "action_type" in r.message.lower()
            ]
            assert len(warning_records) > 0, (
                f"Expected a warning log for invalid action_type={action_type!r}, "
                f"but no warning was logged. Records: {[r.message for r in records]}"
            )
            # Verify the invalid value appears in the warning message
            assert any(repr(action_type) in r.message for r in warning_records), (
                f"Expected warning to contain the invalid value {action_type!r}, "
                f"but got: {[r.message for r in warning_records]}"
            )
        finally:
            logger.removeHandler(handler)
            handler.close()
