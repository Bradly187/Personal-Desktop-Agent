"""FusionEngine edge cases — malformed sensor input must not break the cursor.

The tilt pipelines feed a OneEuroFilter and a sub-pixel accumulator, neither of
which guards against non-finite values. A single NaN/Inf (JSON parses the
`NaN`/`Infinity` tokens by default, so a buggy iPad packet can deliver one) would
poison that state permanently — NaN - NaN = NaN forever — after which round()/
int() in _tick raises on every tick and tilt silently dies until a filter reset.

FusionEngine now drops non-finite tilt frames at ingress (per the project's
"every sensor must degrade gracefully, never crash" rule). These tests pin it.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.fusion_engine import FusionEngine


def _engine() -> FusionEngine:
    fe = FusionEngine(screen_width=1920, screen_height=1080)
    fe._coordinator = MagicMock()
    fe._coordinator.route = AsyncMock(return_value={"status": "ok"})
    return fe


# ---------------------------------------------------------------------------
# Ingress validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rx,ry", [
    (float("nan"), 0.1),
    (0.1, float("nan")),
    (float("inf"), 0.1),
    (0.1, float("-inf")),
    (float("nan"), float("inf")),
])
def test_on_tilt_drops_non_finite(rx, ry):
    fe = _engine()
    fe.on_tilt(rx, ry)
    assert fe._tilt is None
    assert fe._last_tilt_sample is None


def test_on_tilt_accepts_finite():
    fe = _engine()
    fe.on_tilt(0.1, -0.2)
    assert fe._tilt == (0.1, -0.2)
    assert fe._last_tilt_sample == (0.1, -0.2)


@pytest.mark.parametrize("x,y", [
    (float("nan"), 0.5),
    (0.5, float("inf")),
    (float("-inf"), float("nan")),
])
def test_on_tilt_position_drops_non_finite(x, y):
    fe = _engine()
    fe.on_tilt_position(x, y)
    assert fe._tilt_position is None


def test_on_tilt_position_accepts_finite():
    fe = _engine()
    fe.on_tilt_position(0.3, 0.7)
    assert fe._tilt_position == (0.3, 0.7)


# ---------------------------------------------------------------------------
# Poison resistance — a dropped NaN frame must not break later valid frames
# ---------------------------------------------------------------------------

def test_nan_position_frame_does_not_poison_tilt(monkeypatch):
    with patch("pyautogui.moveTo") as mv, \
         patch("pyautogui.moveRel"), \
         patch("pyautogui.position", return_value=MagicMock(x=960, y=540)):
        fe = _engine()

        # NaN frame: dropped at ingress, tick is a no-op for tilt and must not raise.
        fe.on_tilt_position(float("nan"), float("nan"))
        asyncio.run(fe._tick())
        assert fe._tilt_position is None
        assert not mv.called

        # A subsequent valid frame still drives the cursor — the filter is healthy.
        fe.on_tilt_position(0.9, 0.9)
        asyncio.run(fe._tick())
        assert mv.called
        px_x, px_y = mv.call_args[0][:2]
        assert isinstance(px_x, int) and isinstance(px_y, int)
        assert 0 <= px_x < 1920 and 0 <= px_y < 1080


def test_nan_velocity_frame_does_not_poison_tilt(monkeypatch):
    with patch("pyautogui.moveRel") as mv, \
         patch("pyautogui.moveTo"), \
         patch("pyautogui.position", return_value=MagicMock(x=960, y=540)):
        fe = _engine()

        fe.on_tilt(float("inf"), float("nan"))   # dropped at ingress
        asyncio.run(fe._tick())
        assert fe._tilt is None

        # Large but finite tilt → cursor moves; accumulator was never poisoned.
        for _ in range(5):
            fe.on_tilt(0.5, 0.5)
            asyncio.run(fe._tick())
        assert mv.called
        for c in mv.call_args_list:
            dx, dy = c[0][:2]
            assert isinstance(dx, int) and isinstance(dy, int)
