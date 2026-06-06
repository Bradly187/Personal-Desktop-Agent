"""Tests for the RUN_TERMINAL sandbox (WSL2 namespace jail).

Pure arg-construction + tool detection are unit-tested everywhere; the actual
jailed run is WSL-only (bwrap), so the live path is exercised via the graceful
fallback (which runs on any OS) plus mocked tool detection.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import inference.sandbox as sb
from inference.sandbox import build_sandbox_argv, run_sandboxed, sandbox_tool


# ---------------------------------------------------------------------------
# Arg construction (pure)
# ---------------------------------------------------------------------------

def test_bwrap_argv_jails_cwd_and_denies_network():
    argv = build_sandbox_argv("bwrap", "pytest -q", "/home/u/proj", allow_network=False)
    assert argv[0] == "bwrap"
    assert "--unshare-net" in argv                       # network denied
    assert "--ro-bind" in argv                           # root read-only
    # project dir is bound read-write and is the chdir target
    assert argv[argv.index("--bind") + 1] == "/home/u/proj"
    assert argv[argv.index("--chdir") + 1] == "/home/u/proj"
    assert argv[-3:] == ["/bin/sh", "-c", "pytest -q"]   # command at the end


def test_bwrap_argv_allow_network_omits_unshare():
    argv = build_sandbox_argv("bwrap", "curl x", "/p", allow_network=True)
    assert "--unshare-net" not in argv


def test_firejail_argv_denies_network():
    argv = build_sandbox_argv("firejail", "ls", "/p", allow_network=False)
    assert argv[0] == "firejail"
    assert "--net=none" in argv
    assert argv[-3:] == ["/bin/sh", "-c", "ls"]


def test_unknown_tool_raises():
    with pytest.raises(ValueError):
        build_sandbox_argv("nope", "ls", "/p")


# ---------------------------------------------------------------------------
# Tool detection
# ---------------------------------------------------------------------------

def test_sandbox_tool_none_when_absent(monkeypatch):
    monkeypatch.setattr(sb, "_POSIX", True)
    monkeypatch.setattr(sb.shutil, "which", lambda _: None)
    assert sandbox_tool() is None


def test_sandbox_tool_prefers_bwrap(monkeypatch):
    monkeypatch.setattr(sb, "_POSIX", True)
    monkeypatch.setattr(sb.shutil, "which", lambda name: "/usr/bin/" + name if name == "bwrap" else None)
    assert sandbox_tool() == "bwrap"


def test_sandbox_tool_none_on_windows(monkeypatch):
    monkeypatch.setattr(sb, "_POSIX", False)
    assert sandbox_tool() is None


# ---------------------------------------------------------------------------
# run_sandboxed — fallback path (runs on any OS) + DA_SANDBOX flag + output cap
# ---------------------------------------------------------------------------

def test_run_sandboxed_fallback_executes(monkeypatch):
    monkeypatch.setattr(sb, "sandbox_tool", lambda: None)   # force fallback
    # A trivially-portable command: python prints OK.
    r = run_sandboxed(f'{sys.executable} -c "print(\'OK\')"', timeout=30)
    assert r.returncode == 0
    assert "OK" in r.stdout
    assert r.sandboxed is False


def test_da_sandbox_flag_disables(monkeypatch):
    monkeypatch.setenv("DA_SANDBOX", "0")
    assert sb._enabled() is False
    # With the flag off, sandbox_tool isn't consulted → fallback.
    r = run_sandboxed(f'{sys.executable} -c "print(1)"', timeout=30)
    assert r.sandboxed is False


def test_output_is_capped(monkeypatch):
    monkeypatch.setattr(sb, "sandbox_tool", lambda: None)
    big = f'{sys.executable} -c "print(\'x\'*100000)"'
    r = run_sandboxed(big, timeout=30, output_cap=500)
    assert len(r.stdout) <= 500


def test_run_sandboxed_uses_jail_when_tool_present(monkeypatch):
    """When a tool is detected, run_sandboxed builds the jail argv (mock subprocess)."""
    monkeypatch.setattr(sb, "sandbox_tool", lambda: "bwrap")
    captured = {}

    class _Proc:
        stdout, stderr, returncode = "out", "", 0

    def _fake_run(argv, **kw):
        captured["argv"] = argv
        captured["kw"] = kw
        return _Proc()

    monkeypatch.setattr(sb.subprocess, "run", _fake_run)
    r = run_sandboxed("echo hi", project_dir="/proj", timeout=10)
    assert r.sandboxed is True
    assert captured["argv"][0] == "bwrap"
    assert "--unshare-net" in captured["argv"]
    assert captured["kw"].get("capture_output") is True
