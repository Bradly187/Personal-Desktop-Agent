"""Tests for the OPEN verb's detached app launch (#5).

The old fire-and-forget `Popen([exe])` shared the agent's signal group (an agent
Ctrl-C could tear down the app the user just opened) and dropped the handle (a
launched app that exits while the agent lives lingered as a POSIX zombie).
_launch_detached fixes both: a new process group/session, and a tracked handle
that is opportunistically poll()-reaped.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import core.command_executor as ce


class _FakeProc:
    def __init__(self, alive=True):
        self._alive = alive

    def poll(self):
        return None if self._alive else 0


@pytest.fixture(autouse=True)
def _clear_registry():
    ce._launched_procs.clear()
    yield
    ce._launched_procs.clear()


def test_launch_detached_uses_new_group(monkeypatch):
    captured = {}

    def _fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _FakeProc(alive=True)

    monkeypatch.setattr(ce.subprocess, "Popen", _fake_popen)
    ce._launch_detached(["C:/x/app.exe"])

    assert captured["argv"] == ["C:/x/app.exe"]
    if os.name == "nt":
        assert captured["kwargs"].get("creationflags", 0) & ce.subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert captured["kwargs"].get("start_new_session") is True


def test_launch_detached_tracks_handle(monkeypatch):
    monkeypatch.setattr(ce.subprocess, "Popen", lambda argv, **k: _FakeProc(alive=True))
    assert len(ce._launched_procs) == 0
    ce._launch_detached(["app"])
    assert len(ce._launched_procs) == 1


def test_exited_launches_are_reaped(monkeypatch):
    # Seed the registry with an already-exited process.
    ce._launched_procs.append(_FakeProc(alive=False))
    monkeypatch.setattr(ce.subprocess, "Popen", lambda argv, **k: _FakeProc(alive=True))

    ce._launch_detached(["app"])

    # The dead one was pruned; only the new (alive) handle remains.
    assert len(ce._launched_procs) == 1
    assert ce._launched_procs[0].poll() is None


def test_reap_survives_poll_error(monkeypatch):
    class _BadProc:
        def poll(self):
            raise OSError("handle gone")

    ce._launched_procs.append(_BadProc())
    monkeypatch.setattr(ce.subprocess, "Popen", lambda argv, **k: _FakeProc(alive=True))
    ce._launch_detached(["app"])     # must not raise
    assert len(ce._launched_procs) == 1
