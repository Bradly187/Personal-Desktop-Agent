"""ensure_ollama_running — start a local Ollama server if it isn't already up.

The command model and the ModelRouter specialists both run on Ollama, so the
agent bootstraps the server at startup (main.py). These tests pin the bootstrap
contract without a real Ollama: no-op when alive, detached launch + poll when
down, and graceful False (cloud fallback) when Ollama is missing or won't come up.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import inference.backends.ollama as li


def test_noop_when_already_alive(monkeypatch):
    calls = {"popen": 0}
    import subprocess
    monkeypatch.setattr(li, "_ollama_alive", lambda *a, **k: True)
    monkeypatch.setattr(subprocess, "Popen",
                        lambda *a, **k: calls.__setitem__("popen", calls["popen"] + 1))
    assert li.ensure_ollama_running() is True
    assert calls["popen"] == 0   # never tried to start a second server


def test_starts_server_when_down_then_comes_up(monkeypatch):
    # First probe (initial check) is down; after launch the poll sees it up.
    seq = iter([False, True])
    monkeypatch.setattr(li, "_ollama_alive", lambda *a, **k: next(seq))
    monkeypatch.setattr(li, "_find_ollama_exe", lambda: r"C:\fake\ollama.exe")

    launched = {}
    import subprocess

    def _fake_popen(cmd, **kwargs):
        launched["cmd"] = cmd
        launched["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(li.time, "sleep", lambda *_: None)   # no real delay

    assert li.ensure_ollama_running(wait_s=5.0) is True
    assert launched["cmd"] == [r"C:\fake\ollama.exe", "serve"]
    # Launched detached (not a child that dies with the agent).
    if sys.platform == "win32":
        assert "creationflags" in launched["kwargs"]
    else:
        assert launched["kwargs"].get("start_new_session") is True


def test_returns_false_when_ollama_not_installed(monkeypatch):
    calls = {"popen": 0}
    monkeypatch.setattr(li, "_ollama_alive", lambda *a, **k: False)
    monkeypatch.setattr(li, "_find_ollama_exe", lambda: None)
    import subprocess
    monkeypatch.setattr(subprocess, "Popen",
                        lambda *a, **k: calls.__setitem__("popen", calls["popen"] + 1))
    assert li.ensure_ollama_running() is False
    assert calls["popen"] == 0   # nothing to launch


def test_returns_false_when_launch_raises(monkeypatch):
    monkeypatch.setattr(li, "_ollama_alive", lambda *a, **k: False)
    monkeypatch.setattr(li, "_find_ollama_exe", lambda: r"C:\fake\ollama.exe")
    import subprocess

    def _boom(*a, **k):
        raise OSError("cannot exec")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    assert li.ensure_ollama_running() is False


def test_returns_false_when_never_comes_up(monkeypatch):
    monkeypatch.setattr(li, "_ollama_alive", lambda *a, **k: False)  # always down
    monkeypatch.setattr(li, "_find_ollama_exe", lambda: r"C:\fake\ollama.exe")
    import subprocess
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: object())
    monkeypatch.setattr(li.time, "sleep", lambda *_: None)
    # Tiny budget so the poll loop exits fast.
    assert li.ensure_ollama_running(wait_s=0.0) is False
