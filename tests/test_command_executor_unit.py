"""Unit tripwires for core/command_executor.py pure helpers (Gap 4).

The CommandExecutor dispatcher is exercised end-to-end by many suites, but its
pure, decision-defining helpers had no direct test. These pin the idempotency
key (dedup contract), the coordinate-resolution fallback chain, and the verb-set
invariants — none of which touch pyautogui / the real desktop.
"""

from unittest.mock import patch

import pytest

from core.command_executor import (
    Command,
    CommandExecutor,
    TargetResolutionError,
    _IDEMPOTENT_VERBS,
    _VERIFY_FAIL_VERBS,
)


# --------------------------------------------------------------------------- #
# _make_idempotency_key — dedup contract (static, pure)
# --------------------------------------------------------------------------- #
def test_idempotency_key_is_deterministic_sha256():
    k1 = CommandExecutor._make_idempotency_key("CLICK", {"x": 1, "y": 2})
    k2 = CommandExecutor._make_idempotency_key("CLICK", {"x": 1, "y": 2})
    assert k1 == k2
    assert len(k1) == 64  # sha256 hexdigest


def test_idempotency_key_action_is_case_insensitive():
    assert (CommandExecutor._make_idempotency_key("click", {"x": 1})
            == CommandExecutor._make_idempotency_key("CLICK", {"x": 1}))


def test_idempotency_key_is_param_order_independent():
    assert (CommandExecutor._make_idempotency_key("WRITE_FILE", {"a": 1, "b": 2})
            == CommandExecutor._make_idempotency_key("WRITE_FILE", {"b": 2, "a": 1}))


def test_idempotency_key_differs_on_params_and_action():
    base = CommandExecutor._make_idempotency_key("CLICK", {"x": 1})
    assert base != CommandExecutor._make_idempotency_key("CLICK", {"x": 2})
    assert base != CommandExecutor._make_idempotency_key("SCROLL", {"x": 1})


# --------------------------------------------------------------------------- #
# _resolve_coords — fallback chain (explicit → uia → gaze → cursor; strict)
# --------------------------------------------------------------------------- #
def test_resolve_coords_uses_explicit_params_first():
    cmd = Command(text="", action="CLICK", source="touch", params={"x": 100, "y": 200})
    assert CommandExecutor._resolve_coords(cmd) == (100, 200, "explicit")


def test_resolve_coords_falls_back_to_gaze_coords():
    # No explicit params and no target text → skip UIA, use gaze coords.
    cmd = Command(text="", action="CLICK", source="voice", gaze_coords=(50, 60))
    assert CommandExecutor._resolve_coords(cmd) == (50, 60, "gaze")


def test_resolve_coords_strict_target_raises_when_unresolvable():
    # A named target that UIA can't resolve must surface rather than silently
    # clicking the cursor position (EH-1). Mock the COM walk so no real UIA runs.
    cmd = Command(text="nonexistent button", action="CLICK", source="voice")
    with patch("core.command_executor._run_on_com_thread", return_value=None):
        with pytest.raises(TargetResolutionError):
            CommandExecutor._resolve_coords(cmd, strict_target=True)


# --------------------------------------------------------------------------- #
# verb-set invariants
# --------------------------------------------------------------------------- #
def test_write_file_is_the_idempotent_verb():
    assert "WRITE_FILE" in _IDEMPOTENT_VERBS


def test_verify_fail_verbs_cover_click_open_close():
    assert {"CLICK", "OPEN", "CLOSE"} <= _VERIFY_FAIL_VERBS
