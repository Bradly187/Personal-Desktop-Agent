"""EH-1 — close the verification loop (E1) and stop silent target misses (E2).

Before EH-1 the executor returned ``status="ok"`` regardless of the perceptual-
diff verdict, and ``_resolve_coords`` silently fell back to a cursor-position
click when a named target could not be located. A wrong/no-op click was then
recorded as a success and fed to ``trainer.record_success``.

Now:
- ``_resolve_coords(strict_target=True)`` raises ``TargetResolutionError`` when a
  named target resolves to nothing → executor returns ``status="resolve_miss"``.
- A verifiable click-like verb whose diff is ``no_change`` → executor returns
  ``status="verify_failed"`` (SCROLL is excluded — end-of-page is not a failure).
- The coordinator converts ``verify_failed``/``resolve_miss`` into an honest
  spoken CLARIFY, retries a vision-resolved CLICK once via UIAutomation, and
  reports the original miss so route() records a failure.

Run:
    python -m pytest tests/test_verify_loop.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import core.command_executor as ce
from core.command_executor import (
    Command,
    CommandExecutor,
    TargetResolutionError,
)
from core.hybrid_coordinator import CoordinatorConfig, HybridCoordinator
from desktop.action_verifier import VerifyResult


# ===========================================================================
# command_executor — _resolve_coords / _dispatch (E2)
# ===========================================================================

def _click(target: str = "", **params) -> Command:
    return Command(text=target, action="CLICK", source="voice", params=params)


def test_resolve_explicit_coords_bypass_everything():
    cmd = _click(x=10, y=20)
    assert CommandExecutor._resolve_coords(cmd, strict_target=True) == (10, 20, "explicit")


def test_resolve_strict_raises_on_named_target_miss(monkeypatch):
    # UIAutomation finds nothing; no explicit/gaze coords; a target was named.
    monkeypatch.setattr(ce, "_run_on_com_thread", lambda *a, **k: None)
    cmd = _click("submit button")
    with pytest.raises(TargetResolutionError) as ei:
        CommandExecutor._resolve_coords(cmd, strict_target=True)
    assert "submit button" in str(ei.value)


def test_resolve_nonstrict_named_miss_falls_back_to_cursor(monkeypatch):
    # Same miss, but non-strict (SCROLL path) keeps the cursor fallback.
    monkeypatch.setattr(ce, "_run_on_com_thread", lambda *a, **k: None)
    coords = CommandExecutor._resolve_coords(_click("nowhere"), strict_target=False)
    assert isinstance(coords, tuple) and len(coords) == 3
    assert coords[2] == "cursor"


def test_resolve_strict_no_target_keeps_cursor_fallback(monkeypatch):
    # No named target (e.g. voice "click") → cursor fallback is legitimate even
    # in strict mode; must NOT raise.
    monkeypatch.setattr(ce, "_run_on_com_thread", lambda *a, **k: None)
    coords = CommandExecutor._resolve_coords(_click(""), strict_target=True)
    assert isinstance(coords, tuple) and len(coords) == 3
    assert coords[2] == "cursor"


def test_resolve_uses_gaze_coords_before_raising(monkeypatch):
    monkeypatch.setattr(ce, "_run_on_com_thread", lambda *a, **k: None)
    cmd = Command(text="thing", action="CLICK", source="voice",
                  gaze_coords=(7, 8), params={})
    assert CommandExecutor._resolve_coords(cmd, strict_target=True) == (7, 8, "gaze")


def test_dispatch_click_raises_on_named_miss(monkeypatch):
    monkeypatch.setattr(ce, "_run_on_com_thread", lambda *a, **k: None)
    ex = CommandExecutor()
    with pytest.raises(TargetResolutionError):
        ex._dispatch("CLICK", _click("ghost element"))


# ===========================================================================
# command_executor — execute() status contract (E1/E2)
# ===========================================================================

class _FakeVerifier:
    """Stand-in ActionVerifier whose verdict is scripted."""

    def __init__(self, reason: str, success: bool):
        self._reason = reason
        self._success = success

    def snapshot(self) -> str:
        return "PRE"

    def verify(self, pre_b64, action, post_b64=""):
        return VerifyResult(success=self._success, diff_pct=0.0, reason=self._reason)


def _install_fake_verifier(monkeypatch, reason, success):
    monkeypatch.setattr(ce, "_get_action_verifier",
                        lambda: _FakeVerifier(reason, success))
    # Don't drive the real mouse during tests.
    monkeypatch.setattr(ce.mouse, "mouse_click",
                        lambda x, y, button="left": {"clicked": [x, y]})
    monkeypatch.setattr(ce.mouse, "mouse_scroll",
                        lambda x, y, direction="down", clicks=3: {"scrolled": clicks})


async def test_execute_click_verify_failed_status(monkeypatch):
    _install_fake_verifier(monkeypatch, reason="no_change", success=False)
    ex = CommandExecutor()
    res = await ex.execute(_click(x=5, y=6))
    assert res["status"] == "verify_failed"
    assert res["verification"]["reason"] == "no_change"


async def test_execute_scroll_no_change_is_not_a_failure(monkeypatch):
    # SCROLL is deliberately excluded from _VERIFY_FAIL_VERBS.
    _install_fake_verifier(monkeypatch, reason="no_change", success=False)
    ex = CommandExecutor()
    cmd = Command(text="", action="SCROLL", source="voice",
                  params={"x": 5, "y": 6, "direction": "down"})
    res = await ex.execute(cmd)
    assert res["status"] == "ok"


async def test_execute_click_changed_is_ok(monkeypatch):
    _install_fake_verifier(monkeypatch, reason="changed", success=True)
    ex = CommandExecutor()
    res = await ex.execute(_click(x=5, y=6))
    assert res["status"] == "ok"


async def test_execute_click_resolve_miss_status(monkeypatch):
    # No verifier (so no real screenshot), named target nothing resolves.
    monkeypatch.setattr(ce, "_get_action_verifier", lambda: None)
    monkeypatch.setattr(ce, "_run_on_com_thread", lambda *a, **k: None)
    ex = CommandExecutor()
    res = await ex.execute(_click("phantom button"))
    assert res["status"] == "resolve_miss"
    assert res["target"] == "phantom button"


# ===========================================================================
# hybrid_coordinator — _execute_action conversion + retry (E1/E2)
# ===========================================================================

class _ScriptedExecutor:
    """Records every execute() call and returns scripted statuses in order.

    A CLARIFY command always returns ok (it's the spoken clarification), so the
    script only covers the action verbs under test.
    """

    def __init__(self, statuses):
        self._statuses = list(statuses)
        self.calls = []  # list of exec_cmd

    async def execute(self, exec_cmd):
        self.calls.append(exec_cmd)
        if exec_cmd.action == "CLARIFY":
            return {"status": "ok", "action": "CLARIFY"}
        status = self._statuses.pop(0) if self._statuses else "ok"
        return {"status": status, "action": exec_cmd.action,
                "verification": {"reason": "no_change"}}


def _coord(executor) -> HybridCoordinator:
    coord = HybridCoordinator(config=CoordinatorConfig())
    coord._executor = executor
    return coord


def _voice_click(text="click submit") -> Command:
    return Command(text=text, action="", source="voice")


async def test_resolve_miss_converts_to_clarify():
    ex = _ScriptedExecutor(["resolve_miss"])
    coord = _coord(ex)
    coord._ground_target = AsyncMock(return_value=None)  # no vision coords

    result = await coord._execute_action("CLICK submit", _voice_click(), route_label="local")

    assert result["status"] == "resolve_miss"          # reported as a failure to route()
    assert result["spoke_clarification"] is True
    # A CLARIFY was spoken, mentioning the missing target.
    clarifies = [c for c in ex.calls if c.action == "CLARIFY"]
    assert clarifies and "submit" in clarifies[-1].text.lower()
    assert coord._pending_clarification


async def test_verify_failed_no_vision_converts_to_clarify():
    ex = _ScriptedExecutor(["verify_failed"])
    coord = _coord(ex)
    coord._ground_target = AsyncMock(return_value=None)

    result = await coord._execute_action("CLICK submit", _voice_click(), route_label="local")

    assert result["status"] == "verify_failed"
    assert result["spoke_clarification"] is True
    assert any(c.action == "CLARIFY" for c in ex.calls)


async def test_vision_click_verify_failed_retries_via_uia_then_ok():
    # First (vision-coords) click verify-fails; the one retry succeeds.
    ex = _ScriptedExecutor(["verify_failed", "ok"])
    coord = _coord(ex)
    coord._ground_target = AsyncMock(return_value=(100, 200))  # vision succeeds

    result = await coord._execute_action("CLICK submit", _voice_click(), route_label="local")

    assert result["status"] == "ok"
    action_calls = [c for c in ex.calls if c.action == "CLICK"]
    assert len(action_calls) == 2                       # exactly one retry
    assert "x" in action_calls[0].params               # first used vision coords
    assert "x" not in action_calls[1].params            # retry forced structured resolve
    assert not any(c.action == "CLARIFY" for c in ex.calls)


async def test_vision_click_double_verify_failed_clarifies():
    # Vision click fails, the UIA retry also fails → honest CLARIFY.
    ex = _ScriptedExecutor(["verify_failed", "verify_failed"])
    coord = _coord(ex)
    coord._ground_target = AsyncMock(return_value=(100, 200))

    result = await coord._execute_action("CLICK submit", _voice_click(), route_label="local")

    assert result["status"] == "verify_failed"
    assert len([c for c in ex.calls if c.action == "CLICK"]) == 2
    assert any(c.action == "CLARIFY" for c in ex.calls)


async def test_successful_click_does_not_clarify():
    ex = _ScriptedExecutor(["ok"])
    coord = _coord(ex)
    coord._ground_target = AsyncMock(return_value=(100, 200))

    result = await coord._execute_action("CLICK submit", _voice_click(), route_label="local")

    assert result["status"] == "ok"
    assert not any(c.action == "CLARIFY" for c in ex.calls)


# ===========================================================================
# hybrid_coordinator — Gate-1 gesture discard is recorded (E10)
# ===========================================================================

async def test_low_confidence_gesture_discard_is_logged():
    coord = HybridCoordinator(config=CoordinatorConfig())

    class _DB:
        available = True

        def __init__(self):
            self.insert_command = AsyncMock(return_value=1)

    db = _DB()
    coord._agent_db = db

    cmd = Command(text="swipe", action="", source="gesture", gesture_confidence=0.1)
    result = await coord.route(cmd)

    assert result["status"] == "discarded"
    assert result["reason"] == "gate1_gesture_conf"
    db.insert_command.assert_awaited_once()
    kwargs = db.insert_command.await_args.kwargs
    assert kwargs["gate_that_decided"] == "gate1_gesture_conf"
    assert kwargs["success"] is False
