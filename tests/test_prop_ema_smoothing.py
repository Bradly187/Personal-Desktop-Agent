"""Property-based tests for FusionEngine EMA smoothing recurrence (Property 9).

Uses Hypothesis to validate correctness properties for EMA smoothing applied to
tilt position messages:
- The EMA recurrence relation holds exactly: smoothed[n] == alpha * input[n] + (1 - alpha) * smoothed[n-1]
- The variance of the smoothed sequence is less than or equal to the variance of the input sequence

**Validates: Requirements 5.2, 6.2**

Run:
    pytest tests/test_prop_ema_smoothing.py -v
"""

from __future__ import annotations

import asyncio
import math
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from hypothesis import given, settings, assume
from hypothesis import strategies as st

sys.path.insert(0, str(Path(__file__).parent.parent))

from fusion_engine import FusionEngine, FusionConfig


SCREEN_W = 1920
SCREEN_H = 1080


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Normalized position coordinates [0.0, 1.0] — what tilt_position messages contain
pos_coord_st = st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)

# EMA alpha values in valid range [0.1, 0.9]
alpha_st = st.floats(min_value=0.1, max_value=0.9, allow_nan=False, allow_infinity=False)

# Position sequences (lists of floats in [0, 1]) with at least 2 elements
pos_sequence_st = st.lists(
    st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
    min_size=2,
    max_size=50,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_engine(alpha: float = 0.4) -> FusionEngine:
    """Create a FusionEngine with configurable EMA alpha for tilt position."""
    cfg = FusionConfig()
    engine = FusionEngine(screen_width=SCREEN_W, screen_height=SCREEN_H, config=cfg)
    engine._tilt_pos_alpha = alpha
    # Disable gaze cursor mode so tilt position is not suppressed
    engine._feature_toggles["gaze_cursor_mode"] = False
    mock_coordinator = AsyncMock()
    mock_coordinator.route = AsyncMock()
    engine.set_coordinator(mock_coordinator)
    return engine


def _apply_ema_sequence(inputs: list[float], alpha: float) -> list[float]:
    """Compute EMA sequence from inputs using the recurrence relation.

    smoothed[0] = input[0]
    smoothed[n] = alpha * input[n] + (1 - alpha) * smoothed[n-1]
    """
    if not inputs:
        return []
    smoothed = [inputs[0]]
    for i in range(1, len(inputs)):
        s = alpha * inputs[i] + (1 - alpha) * smoothed[-1]
        smoothed.append(s)
    return smoothed


def _variance(values: list[float]) -> float:
    """Compute population variance of a list of floats."""
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return sum((v - mean) ** 2 for v in values) / len(values)


# ---------------------------------------------------------------------------
# Property 9: EMA Smoothing Recurrence
# ---------------------------------------------------------------------------

class TestProperty9EMARecurrence:
    """Property 9: For any sequence of position messages received by FusionEngine,
    the smoothed output at step n SHALL equal `alpha * input[n] + (1 - alpha) * smoothed[n-1]`,
    and the variance of the smoothed sequence SHALL be less than or equal to the variance
    of the input sequence.

    **Validates: Requirements 5.2, 6.2**
    """

    @given(
        x_inputs=pos_sequence_st,
        y_inputs=pos_sequence_st,
        alpha=alpha_st,
    )
    @settings(max_examples=200)
    def test_ema_recurrence_relation_holds(
        self, x_inputs: list[float], y_inputs: list[float], alpha: float
    ) -> None:
        """The EMA recurrence relation holds exactly (within floating-point epsilon)
        for both X and Y axes when feeding tilt_position messages to FusionEngine."""
        engine = _make_engine(alpha=alpha)

        # Track the EMA values produced by the engine
        engine_ema_x_values: list[float] = []
        engine_ema_y_values: list[float] = []

        # Use the shorter of the two sequences
        n = min(len(x_inputs), len(y_inputs))

        for i in range(n):
            engine.on_tilt_position(x_inputs[i], y_inputs[i])

            # Run tick to process the tilt_position
            with patch("pyautogui.moveTo"):
                asyncio.run(engine._tick())

            engine_ema_x_values.append(engine._tilt_pos_ema_x)
            engine_ema_y_values.append(engine._tilt_pos_ema_y)

        # Verify recurrence relation for X axis
        # First value: smoothed[0] == input[0]
        assert math.isclose(engine_ema_x_values[0], x_inputs[0], rel_tol=1e-9, abs_tol=1e-12), (
            f"EMA X[0] should equal input[0]: got {engine_ema_x_values[0]}, expected {x_inputs[0]}"
        )
        assert math.isclose(engine_ema_y_values[0], y_inputs[0], rel_tol=1e-9, abs_tol=1e-12), (
            f"EMA Y[0] should equal input[0]: got {engine_ema_y_values[0]}, expected {y_inputs[0]}"
        )

        # Subsequent values: smoothed[n] == alpha * input[n] + (1 - alpha) * smoothed[n-1]
        for i in range(1, n):
            expected_x = alpha * x_inputs[i] + (1 - alpha) * engine_ema_x_values[i - 1]
            expected_y = alpha * y_inputs[i] + (1 - alpha) * engine_ema_y_values[i - 1]

            assert math.isclose(engine_ema_x_values[i], expected_x, rel_tol=1e-9, abs_tol=1e-12), (
                f"EMA X[{i}] recurrence failed: got {engine_ema_x_values[i]}, "
                f"expected {expected_x} (alpha={alpha}, input={x_inputs[i]}, prev={engine_ema_x_values[i-1]})"
            )
            assert math.isclose(engine_ema_y_values[i], expected_y, rel_tol=1e-9, abs_tol=1e-12), (
                f"EMA Y[{i}] recurrence failed: got {engine_ema_y_values[i]}, "
                f"expected {expected_y} (alpha={alpha}, input={y_inputs[i]}, prev={engine_ema_y_values[i-1]})"
            )

    @given(
        x_inputs=pos_sequence_st,
        alpha=alpha_st,
    )
    @settings(max_examples=200)
    def test_ema_variance_reduction_x(self, x_inputs: list[float], alpha: float) -> None:
        """The variance of the EMA-smoothed X sequence is less than or equal to
        the variance of the input X sequence."""
        assume(len(x_inputs) >= 2)

        smoothed = _apply_ema_sequence(x_inputs, alpha)

        input_var = _variance(x_inputs)
        smoothed_var = _variance(smoothed)

        assert smoothed_var <= input_var + 1e-10, (
            f"Smoothed variance ({smoothed_var:.8f}) exceeds input variance ({input_var:.8f}) "
            f"for alpha={alpha}, inputs={x_inputs[:5]}..."
        )

    @given(
        y_inputs=pos_sequence_st,
        alpha=alpha_st,
    )
    @settings(max_examples=200)
    def test_ema_variance_reduction_y(self, y_inputs: list[float], alpha: float) -> None:
        """The variance of the EMA-smoothed Y sequence is less than or equal to
        the variance of the input Y sequence."""
        assume(len(y_inputs) >= 2)

        smoothed = _apply_ema_sequence(y_inputs, alpha)

        input_var = _variance(y_inputs)
        smoothed_var = _variance(smoothed)

        assert smoothed_var <= input_var + 1e-10, (
            f"Smoothed variance ({smoothed_var:.8f}) exceeds input variance ({input_var:.8f}) "
            f"for alpha={alpha}, inputs={y_inputs[:5]}..."
        )

    @given(
        x_inputs=pos_sequence_st,
        y_inputs=pos_sequence_st,
        alpha=alpha_st,
    )
    @settings(max_examples=200)
    def test_ema_variance_reduction_via_engine(
        self, x_inputs: list[float], y_inputs: list[float], alpha: float
    ) -> None:
        """Variance reduction holds when tested through the actual FusionEngine
        (end-to-end through on_tilt_position and _tick)."""
        engine = _make_engine(alpha=alpha)

        n = min(len(x_inputs), len(y_inputs))
        assume(n >= 2)

        engine_ema_x_values: list[float] = []
        engine_ema_y_values: list[float] = []

        for i in range(n):
            engine.on_tilt_position(x_inputs[i], y_inputs[i])
            with patch("pyautogui.moveTo"):
                asyncio.run(engine._tick())
            engine_ema_x_values.append(engine._tilt_pos_ema_x)
            engine_ema_y_values.append(engine._tilt_pos_ema_y)

        # Variance of smoothed output <= variance of input
        input_var_x = _variance(x_inputs[:n])
        input_var_y = _variance(y_inputs[:n])
        smoothed_var_x = _variance(engine_ema_x_values)
        smoothed_var_y = _variance(engine_ema_y_values)

        assert smoothed_var_x <= input_var_x + 1e-10, (
            f"Engine smoothed X variance ({smoothed_var_x:.8f}) exceeds input variance ({input_var_x:.8f})"
        )
        assert smoothed_var_y <= input_var_y + 1e-10, (
            f"Engine smoothed Y variance ({smoothed_var_y:.8f}) exceeds input variance ({input_var_y:.8f})"
        )

    @given(
        x_inputs=pos_sequence_st,
        y_inputs=pos_sequence_st,
    )
    @settings(max_examples=150)
    def test_ema_recurrence_with_default_alpha(
        self, x_inputs: list[float], y_inputs: list[float]
    ) -> None:
        """The recurrence relation holds with the default alpha=0.4."""
        alpha = 0.4
        engine = _make_engine(alpha=alpha)

        n = min(len(x_inputs), len(y_inputs))

        engine_ema_x_values: list[float] = []
        engine_ema_y_values: list[float] = []

        for i in range(n):
            engine.on_tilt_position(x_inputs[i], y_inputs[i])
            with patch("pyautogui.moveTo"):
                asyncio.run(engine._tick())
            engine_ema_x_values.append(engine._tilt_pos_ema_x)
            engine_ema_y_values.append(engine._tilt_pos_ema_y)

        # Verify first element initialization
        assert math.isclose(engine_ema_x_values[0], x_inputs[0], rel_tol=1e-9, abs_tol=1e-12)
        assert math.isclose(engine_ema_y_values[0], y_inputs[0], rel_tol=1e-9, abs_tol=1e-12)

        # Verify recurrence for all subsequent elements
        for i in range(1, n):
            expected_x = alpha * x_inputs[i] + (1 - alpha) * engine_ema_x_values[i - 1]
            expected_y = alpha * y_inputs[i] + (1 - alpha) * engine_ema_y_values[i - 1]
            assert math.isclose(engine_ema_x_values[i], expected_x, rel_tol=1e-9, abs_tol=1e-12)
            assert math.isclose(engine_ema_y_values[i], expected_y, rel_tol=1e-9, abs_tol=1e-12)
