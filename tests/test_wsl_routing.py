"""Tests for WSL terminal routing (specs/wsl-terminal-routing).

Pure helpers (path translation, compatibility boundary, argv build) run on any OS;
the routing decision is exercised with monkeypatched WSL availability so no actual
`wsl.exe` is invoked. Scope-preservation: translation maps 1:1 and refuses
untranslatable paths rather than guessing.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import inference.sandbox as sb


# --------------------------------------------------------------------------- #
# to_wsl_path — scope-preserving, refuses what it can't map (R2.2/R2.3)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("win,wsl", [
    ("E:\\proj\\x", "/mnt/e/proj/x"),
    ("D:/x/y", "/mnt/d/x/y"),
    ("E:\\", "/mnt/e"),
    ("C:\\Users\\b\\repo", "/mnt/c/Users/b/repo"),
])
def test_to_wsl_path_maps_drive_paths(win, wsl):
    assert sb.to_wsl_path(win) == wsl


@pytest.mark.parametrize("bad", ["\\\\srv\\share", "//srv/share", "relative/path", "", None])
def test_to_wsl_path_refuses_untranslatable(bad):
    assert sb.to_wsl_path(bad) is None      # → native fallback, never a guess


# --------------------------------------------------------------------------- #
# command_is_wsl_safe — the compatibility boundary (R2.1)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("cmd", [
    "pytest -q",
    "pip install x && git status",
    "python -m pytest tests/",
    "ls -la | grep foo",
    "npm install && npm test",
])
def test_wsl_safe_commands(cmd):
    assert sb.command_is_wsl_safe(cmd) is True


@pytest.mark.parametrize("cmd", [
    "powershell -c ls",
    "pwsh -c x",
    "cmd /c dir",
    "where python",
    "C:\\tools\\foo.exe --run",       # drive-anchored
    "thing.exe --x",                   # .exe
    "weirdtool --x",                   # unknown exe, default policy=native
    "pytest && powershell x",          # any segment native → whole native
])
def test_non_wsl_safe_commands(cmd):
    assert sb.command_is_wsl_safe(cmd) is False


def test_unknown_command_safe_under_wsl_policy():
    assert sb.command_is_wsl_safe("weirdtool --x", {"unknown_command_policy": "wsl"}) is True


def test_config_force_native_extends_denylist():
    assert sb.command_is_wsl_safe("mytool --x", {"unknown_command_policy": "wsl",
                                  "compatibility": {"force_native": ["mytool"]}}) is False


# --------------------------------------------------------------------------- #
# build_wsl_argv
# --------------------------------------------------------------------------- #

def test_build_wsl_argv_with_distro():
    argv = sb.build_wsl_argv(["bwrap", "/bin/sh", "-c", "ls"], "Ubuntu")
    assert argv[:4] == ["wsl.exe", "-d", "Ubuntu", "-e"]
    assert argv[-3:] == ["/bin/sh", "-c", "ls"]


def test_build_wsl_argv_default_distro():
    argv = sb.build_wsl_argv(["bwrap"], "")
    assert argv == ["wsl.exe", "-e", "bwrap"]


# --------------------------------------------------------------------------- #
# _maybe_run_wsl decision order (monkeypatched availability)
# --------------------------------------------------------------------------- #

def _enable(monkeypatch, cfg=None, available=True):
    monkeypatch.setattr(sb, "_POSIX", False)            # pretend Windows host
    monkeypatch.setattr(sb, "_wsl_routing_config",
                        lambda: cfg if cfg is not None else {"enabled": True, "distro": "Ubuntu"})
    monkeypatch.setattr(sb, "wsl_available", lambda distro="": available)


def test_routes_safe_command_through_wsl_jail(monkeypatch):
    _enable(monkeypatch)
    captured = {}

    def fake_run_capped(argv, **kw):
        captured["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    monkeypatch.setattr(sb, "run_capped", fake_run_capped)
    res = sb._maybe_run_wsl("pytest -q", "E:\\proj", 60, False, 200_000)
    assert res is not None and res.sandboxed is True and res.returncode == 0
    argv = captured["argv"]
    assert argv[0] == "wsl.exe" and "Ubuntu" in argv and "bwrap" in argv
    # Binds the TRANSLATED project dir — never the Windows path (R1.2/R2.2).
    assert any("/mnt/e/proj" in a for a in argv)
    assert all("E:\\proj" not in a for a in argv)
    assert "--unshare-net" in argv                       # offline by default


def test_network_op_grants_network_in_wsl(monkeypatch):
    _enable(monkeypatch)
    captured = {}
    monkeypatch.setattr(sb, "run_capped",
                        lambda argv, **kw: captured.update(argv=argv) or
                        subprocess.CompletedProcess(argv, 0, "", ""))
    sb._maybe_run_wsl("pip install requests", "E:\\proj", 60, True, 1000)
    assert "--unshare-net" not in captured["argv"]       # allow_network honored


def test_disabled_returns_none(monkeypatch):
    _enable(monkeypatch, cfg={"enabled": False})
    assert sb._maybe_run_wsl("pytest", "E:\\p", 60, False, 1000) is None


def test_none_on_posix_host(monkeypatch):
    monkeypatch.setattr(sb, "_POSIX", True)
    monkeypatch.setattr(sb, "_wsl_routing_config", lambda: {"enabled": True})
    assert sb._maybe_run_wsl("pytest", "E:\\p", 60, False, 1000) is None


def test_unsafe_command_falls_back_native(monkeypatch):
    _enable(monkeypatch)
    assert sb._maybe_run_wsl("powershell -c ls", "E:\\p", 60, False, 1000) is None


def test_untranslatable_path_falls_back_native(monkeypatch):
    _enable(monkeypatch)
    assert sb._maybe_run_wsl("pytest", "\\\\srv\\share", 60, False, 1000) is None


def test_wsl_unavailable_falls_back_native(monkeypatch):
    _enable(monkeypatch, available=False)
    assert sb._maybe_run_wsl("pytest", "E:\\p", 60, False, 1000) is None


# --------------------------------------------------------------------------- #
# run_sandboxed integration
# --------------------------------------------------------------------------- #

def test_run_sandboxed_uses_wsl_result_when_offered(monkeypatch):
    fake = sb.SandboxResult(stdout="x", stderr="", returncode=0, sandboxed=True)
    monkeypatch.setattr(sb, "_maybe_run_wsl", lambda *a, **k: fake)
    assert sb.run_sandboxed("pytest -q", project_dir="E:\\proj", timeout=5) is fake


def test_run_sandboxed_native_when_wsl_declines(monkeypatch):
    monkeypatch.setattr(sb, "_maybe_run_wsl", lambda *a, **k: None)   # routing off/declined
    monkeypatch.setattr(sb, "sandbox_tool", lambda: None)            # force native fallback
    r = sb.run_sandboxed(f'{sys.executable} -c "print(1)"', timeout=30)
    assert r.sandboxed is False and r.returncode == 0                # legacy path unchanged


def test_default_config_is_enabled(tmp_path, monkeypatch):
    # No config file → default is ON (smoke gate passed 2026-06-21).
    monkeypatch.setattr(sb.Path, "home", classmethod(lambda cls: tmp_path))
    cfg = sb._wsl_routing_config()
    assert cfg.get("enabled") is True
    assert cfg.get("distro") == "Ubuntu"
