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
# Effective route selection and integration (monkeypatched availability)
# --------------------------------------------------------------------------- #

def _enable(monkeypatch, cfg=None, available=True):
    monkeypatch.setattr(sb, "_POSIX", False)            # pretend Windows host
    monkeypatch.setattr(
        sb,
        "_wsl_routing_config",
        lambda: cfg if cfg is not None else {"enabled": True, "distro": "Ubuntu"},
    )
    monkeypatch.setattr(sb, "wsl_available", lambda distro="": available)


def test_routes_safe_command_through_wsl_jail(monkeypatch):
    _enable(monkeypatch)
    captured = {}

    def fake_run_capped(argv, **kw):
        captured["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    monkeypatch.setattr(sb, "run_capped", fake_run_capped)
    res = sb.run_sandboxed("pytest -q", project_dir="E:\\proj", timeout=60)
    assert res.sandboxed is True and res.returncode == 0
    assert res.containment.level is sb.ContainmentLevel.NAMESPACE_JAIL
    argv = captured["argv"]
    assert argv[0] == "wsl.exe" and "Ubuntu" in argv and "bwrap" in argv
    assert any("/mnt/e/proj" in a for a in argv)
    assert all("E:\\proj" not in a for a in argv)
    assert "--unshare-net" in argv


def test_network_op_grants_network_in_wsl(monkeypatch):
    _enable(monkeypatch)
    captured = {}
    monkeypatch.setattr(
        sb,
        "run_capped",
        lambda argv, **kw: captured.update(argv=argv)
        or subprocess.CompletedProcess(argv, 0, "", ""),
    )
    sb.run_sandboxed(
        "pip install requests",
        project_dir="E:\\proj",
        timeout=60,
        allow_network=True,
    )
    assert "--unshare-net" not in captured["argv"]


@pytest.mark.parametrize(
    "command,project,cfg,available,reason",
    [
        ("pytest", "E:\\p", {"enabled": False}, True, "disabled"),
        ("powershell -c ls", "E:\\p", None, True, "not WSL-compatible"),
        ("pytest", "\\\\srv\\share", None, True, "cannot be translated"),
        ("pytest", "E:\\p", None, False, "unavailable"),
    ],
)
def test_windows_fallback_reports_effective_reason(
    monkeypatch, command, project, cfg, available, reason
):
    _enable(monkeypatch, cfg=cfg, available=available)
    report = sb.detect_containment(command, project)
    assert report.level is sb.ContainmentLevel.NATIVE_UNCONFINED
    assert report.mechanism == "windows_native"
    assert reason in (report.reason or "")


def test_posix_without_tool_reports_resource_limited_fallback(monkeypatch):
    monkeypatch.setattr(sb, "_POSIX", True)
    monkeypatch.setattr(sb, "sandbox_tool", lambda: None)
    report = sb.detect_containment("pytest", "/project")
    assert report.level is sb.ContainmentLevel.CWD_AND_RESOURCE_LIMITS
    assert report.mechanism == "posix+rlimits"


def test_trusted_manual_native_fallback_still_executes(monkeypatch):
    _enable(monkeypatch, available=False)
    monkeypatch.setattr(sb, "sandbox_tool", lambda: None)
    monkeypatch.setattr(
        sb,
        "run_capped",
        lambda command, **kw: subprocess.CompletedProcess(command, 0, "1\n", ""),
    )
    result = sb.run_sandboxed(
        "pytest",
        project_dir="E:\\proj",
        timeout=30,
        work_trust=sb.WorkTrust.TRUSTED_MANUAL,
    )
    assert result.sandboxed is False and result.returncode == 0


def test_autonomous_native_fallback_denies_before_workload(monkeypatch):
    _enable(monkeypatch, available=False)
    called = False

    def fail_if_called(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("workload launched")

    monkeypatch.setattr(sb, "run_capped", fail_if_called)
    with pytest.raises(sb.ContainmentDeniedError):
        sb.run_sandboxed("pytest", project_dir="E:\\proj", timeout=30)
    assert called is False


def test_default_config_is_enabled(tmp_path, monkeypatch):
    # No config file → default is ON (smoke gate passed 2026-06-21).
    monkeypatch.setattr(sb.Path, "home", classmethod(lambda cls: tmp_path))
    cfg = sb._wsl_routing_config()
    assert cfg.get("enabled") is True
    assert cfg.get("distro") == "Ubuntu"
