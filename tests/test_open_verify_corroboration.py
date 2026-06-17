"""F3 — an OPEN with no perceptual diff is NOT a failure if the app is open.

Launching an already-running app (or one that only steals focus) produces a
~0% screen diff, which EH-1's verifier scored as ``verify_failed`` — emitting a
corrective CLARIFY and dragging the command-domain success rate below its SLO
floor (observed live: ``SLO breach domain=command success=0.81``). The executor
now corroborates an OPEN whose diff is ``no_change`` against the live window
list before flagging it, so a real launch is reported as success.

Run:
    python -m pytest tests/test_open_verify_corroboration.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "mcp_server"))

import core.command_executor as ce
from core.command_executor import Command, CommandExecutor
from desktop.action_verifier import VerifyResult


# ── _open_target_window_present — title matching ────────────────────────────

def test_window_present_matches_running_app(monkeypatch):
    import tools.windows as win
    monkeypatch.setattr(win, "list_windows",
                        lambda: [{"title": "Google Chrome - New Tab"}])
    assert ce._open_target_window_present("Chrome") is True


def test_window_present_multiword_partial(monkeypatch):
    import tools.windows as win
    monkeypatch.setattr(win, "list_windows",
                        lambda: [{"title": "main.py - Visual Studio Code"}])
    # "vs" is dropped (len < 3); "code" still matches the title.
    assert ce._open_target_window_present("VS Code") is True


def test_window_present_absent(monkeypatch):
    import tools.windows as win
    monkeypatch.setattr(win, "list_windows",
                        lambda: [{"title": "File Explorer"}])
    assert ce._open_target_window_present("Spotify") is False


def test_window_present_empty_target_is_false(monkeypatch):
    # No significant words → never corroborate (avoids matching everything).
    assert ce._open_target_window_present("") is False
    assert ce._open_target_window_present("a my") is False


def test_window_present_lister_error_is_false(monkeypatch):
    import tools.windows as win
    def _boom():
        raise RuntimeError("win32 down")
    monkeypatch.setattr(win, "list_windows", _boom)
    assert ce._open_target_window_present("Chrome") is False


# ── execute() integration — status contract ─────────────────────────────────

class _FakeVerifier:
    def snapshot(self) -> str:
        return "PRE"

    def verify(self, pre_b64, action, post_b64=""):
        return VerifyResult(success=False, diff_pct=0.0, reason="no_change")


def _patch_open_env(monkeypatch):
    """Stub the verifier (no_change) and the Win+S launch keystrokes."""
    monkeypatch.setattr(ce, "_get_action_verifier", lambda: _FakeVerifier())
    monkeypatch.setattr(ce.keyboard, "keyboard_hotkey", lambda *a, **k: {})
    monkeypatch.setattr(ce.keyboard, "keyboard_type", lambda *a, **k: {})
    monkeypatch.setattr(ce.keyboard, "keyboard_press", lambda *a, **k: {})
    monkeypatch.setattr(ce.time, "sleep", lambda *_a, **_k: None)


def _open(target: str) -> Command:
    # An unknown target falls through to the (stubbed) Win+S search path.
    return Command(text=target, action="OPEN", source="voice", params={})


async def test_open_no_change_corroborated_is_ok(monkeypatch):
    _patch_open_env(monkeypatch)
    monkeypatch.setattr(ce, "_open_target_window_present", lambda _t: True)
    res = await CommandExecutor().execute(_open("testapp"))
    assert res["status"] == "ok"
    assert res["verification"]["reason"] == "window_present"
    assert res["verification"]["success"] is True


async def test_open_no_change_not_present_is_verify_failed(monkeypatch):
    _patch_open_env(monkeypatch)
    monkeypatch.setattr(ce, "_open_target_window_present", lambda _t: False)
    res = await CommandExecutor().execute(_open("testapp"))
    assert res["status"] == "verify_failed"
    assert res["verification"]["reason"] == "no_change"
