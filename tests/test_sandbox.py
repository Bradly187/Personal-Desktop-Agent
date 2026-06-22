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
from inference.sandbox import (
    build_sandbox_argv, run_sandboxed, sandbox_tool, command_needs_network,
)


# ---------------------------------------------------------------------------
# command_needs_network classifier
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cmd", [
    "pip install requests",
    "pip3 download numpy",
    "uv sync",
    "uv add httpx",
    "npm install",
    "yarn add react",
    "cargo fetch",
    "go get ./...",
    "git push origin main",
    "git fetch --all",
    "git clone https://x/y",
    "gh pr create",
    "curl https://example.com",
    "wget https://example.com/f",
    "poetry add flask",
    "pytest && pip install foo",        # any segment needing net → True
])
def test_commands_needing_network(cmd):
    assert command_needs_network(cmd) is True


@pytest.mark.parametrize("cmd", [
    "pytest -q",
    "pip list",                          # read-only pip subcommand
    "pip show numpy",
    "git status",                        # local git
    "git diff",
    "git commit -m x",
    "ls -la",
    "python script.py",
    "npm test",                          # 'test' is not a net subcommand
    "cargo build",
    "",
])
def test_commands_not_needing_network(cmd):
    assert command_needs_network(cmd) is False


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
    monkeypatch.setattr(sb, "_wsl_routing_config", lambda: {})  # disable WSL routing; test native bwrap path
    captured = {}

    import subprocess as _sp

    def _fake_run(argv, **kw):
        captured["argv"] = argv
        captured["kw"] = kw
        return _sp.CompletedProcess(argv, 0, "out", "")

    # run_sandboxed now dispatches through proc_utils.run_capped (tree-kill on
    # timeout) rather than subprocess.run directly — patch where it's used.
    monkeypatch.setattr(sb, "run_capped", _fake_run)
    r = run_sandboxed("echo hi", project_dir="/proj", timeout=10)
    assert r.sandboxed is True
    assert captured["argv"][0] == "bwrap"
    assert "--unshare-net" in captured["argv"]
    assert captured["kw"].get("capture_output") is True


# ---------------------------------------------------------------------------
# Interactive-hang hardening (../specs/sandbox-interactive-hardening/)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cmd,expected", [
    # R2.2: apt install/upgrade gets -y injected when no assume-yes present.
    ("apt-get install vim", "apt-get install -y vim"),
    ("apt install vim curl", "apt install -y vim curl"),
    ("sudo apt-get upgrade", "sudo apt-get upgrade -y"),
    ("apt-get dist-upgrade", "apt-get dist-upgrade -y"),
    # Idempotent: an existing yes-flag is left untouched.
    ("apt-get install -y vim", "apt-get install -y vim"),
    ("apt install --yes vim", "apt install --yes vim"),
    ("apt-get install --assume-yes vim", "apt-get install --assume-yes vim"),
    # R2.2: non-apt commands are passed through byte-identical (no blind mutation).
    ("pip install requests", "pip install requests"),
    ("npm install", "npm install"),
    ("pytest -q", "pytest -q"),
    ("git pull", "git pull"),
    ("", ""),
])
def test_inject_noninteractive_flags(cmd, expected):
    assert sb.inject_noninteractive_flags(cmd) == expected


def test_noninteractive_env_sets_the_vars():
    # R2.1: the curated non-interactive vars are present.
    env = sb.noninteractive_env({})
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["DEBIAN_FRONTEND"] == "noninteractive"
    assert env["PIP_NO_INPUT"] == "1"
    assert env["PIP_DISABLE_PIP_VERSION_CHECK"] == "1"


def test_noninteractive_env_does_not_clobber_user_value():
    # R2.1: merged with setdefault — a value the user already set wins.
    env = sb.noninteractive_env({"GIT_TERMINAL_PROMPT": "1", "FOO": "bar"})
    assert env["GIT_TERMINAL_PROMPT"] == "1"
    assert env["FOO"] == "bar"
    assert env["DEBIAN_FRONTEND"] == "noninteractive"   # still added


def test_run_sandboxed_passes_devnull_stdin_and_env(monkeypatch):
    # R1.1 + R2.1: both the fallback and jail branches must close stdin and pass
    # the non-interactive env into run_capped.
    import subprocess as _sp
    captured = {}

    def _fake_run(args, **kw):
        captured["kw"] = kw
        return _sp.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(sb, "run_capped", _fake_run)
    monkeypatch.setattr(sb, "sandbox_tool", lambda: None)   # fallback branch
    r = run_sandboxed("echo hi", timeout=5)
    assert r.returncode == 0
    assert captured["kw"].get("stdin") == _sp.DEVNULL
    assert captured["kw"]["env"]["PIP_NO_INPUT"] == "1"


def test_run_sandboxed_stdin_closed_fails_fast(monkeypatch):
    # R1.2: a command that reads stdin must get immediate EOF (DEVNULL) and
    # return promptly — NOT block until the wall timeout. Real run_capped,
    # fallback path; if stdin were a live TTY/pipe this would time out.
    monkeypatch.setattr(sb, "sandbox_tool", lambda: None)
    r = run_sandboxed(
        f'{sys.executable} -c "import sys; print(repr(sys.stdin.read()))"',
        timeout=10,
    )
    assert r.returncode == 0
    assert "''" in r.stdout        # read() saw EOF immediately, no hang
