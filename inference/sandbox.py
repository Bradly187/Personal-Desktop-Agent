"""WSL2 namespace jail for RUN_TERMINAL (sandbox — G's ceiling).

Threat model: this agent runs the USER'S OWN goals on their own machine. The risk
is the LLM *hallucinating a destructive/looping command*, not an adversary
escaping a jail. So this is MISTAKE-CONTAINMENT — bound the blast radius and
resources — layered BEHIND the goal-session Bash allowlist (core/goal_session.py),
NOT adversarial microVM isolation. (Container/microVM is the documented upgrade if
the threat model ever becomes "run untrusted third-party code"; see the plan.)

Mechanism (Linux/WSL2, where the pipeline already runs):
  - cwd-jail: bind the project dir read-write, the rest of the FS read-only.
  - no network: --unshare-net (bubblewrap) / --net=none (firejail).
  - resource limits: RLIMIT_CPU + RLIMIT_AS via a POSIX preexec_fn.
  - output cap: captured stdout/stderr truncated so a runaway `find /` can't
    flood memory/logs.
  - timeout: unchanged (60 s default).

Graceful fallback: if no sandbox tool is installed (or DA_SANDBOX=0, or on
Windows-native), it runs the command unsandboxed with a clear one-time WARNING +
the cwd + rlimits where possible — never blocks execution, just logs that the
boundary is absent.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# Resource ceilings for a single RUN_TERMINAL (mistake-containment, not security).
_CPU_SECONDS = 60          # wall timeout still applies separately
_MEM_BYTES = 2 * 1024 ** 3  # 2 GB address space
_OUTPUT_CAP = 200_000       # bytes of stdout/stderr retained

_POSIX = os.name == "posix"


def _enabled() -> bool:
    """DA_SANDBOX env flag (default on). '0'/'false'/'off' disables."""
    return os.environ.get("DA_SANDBOX", "1").strip().lower() not in ("0", "false", "off", "no", "")


def sandbox_tool() -> Optional[str]:
    """Return the available sandbox binary name ('bwrap' | 'firejail') or None."""
    if not _POSIX:
        return None
    for tool in ("bwrap", "firejail"):
        if shutil.which(tool):
            return tool
    return None


# Curated network-needing operations (the only commands granted network inside
# the jail). Maps an executable → the subcommands that legitimately need the
# network; None means "any invocation needs it". Everything NOT listed runs with
# --unshare-net. These are still gated by the goal-session allowlist (e.g.
# `pip install` is not auto-approved), so network is only granted to a command
# that already passed approval AND matches a known package/VCS/fetch operation.
_NETWORK_OPS: "dict[str, Optional[frozenset]]" = {
    "pip": frozenset({"install", "download", "wheel"}),       # NOT list/show/freeze
    "pip3": frozenset({"install", "download", "wheel"}),
    "uv": frozenset({"add", "sync", "lock", "pip"}),
    "pipx": frozenset({"install", "run", "upgrade"}),
    "poetry": frozenset({"add", "install", "update", "lock"}),
    "conda": frozenset({"install", "update", "create"}),
    "mamba": frozenset({"install", "update", "create"}),
    "npm": frozenset({"install", "i", "ci", "add", "update", "up"}),
    "yarn": frozenset({"install", "add", "up", "upgrade"}),
    "pnpm": frozenset({"install", "i", "add", "update"}),
    "cargo": frozenset({"fetch", "add", "install", "update", "publish"}),
    "go": frozenset({"get", "install", "mod"}),
    "git": frozenset({"fetch", "pull", "push", "clone", "remote", "submodule"}),
    "gh": None,        # GitHub CLI — inherently networked
    "curl": None, "wget": None,   # fetch tools (approval-gated by the allowlist)
    "apt": frozenset({"install", "update", "upgrade"}),
    "apt-get": frozenset({"install", "update", "upgrade"}),
    "brew": frozenset({"install", "update", "upgrade", "tap"}),
}


def command_needs_network(command: str) -> bool:
    """True if any segment runs a curated network-needing operation.

    Drives `allow_network` so `pip install` / `git push` / `npm install` work
    inside the jail while `pytest`, `ls`, and arbitrary commands stay offline.
    Still gated by the goal-session allowlist upstream — network is only granted
    to a command that already passed approval AND matches a known network op.
    """
    import re
    import shlex

    if not command:
        return False
    for seg in re.split(r"[;&|\n]+", command):
        seg = seg.strip()
        if not seg:
            continue
        try:
            tokens = shlex.split(seg)
        except ValueError:
            continue
        while tokens and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
            tokens = tokens[1:]
        if not tokens:
            continue
        exe = tokens[0].rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
        if exe.endswith(".exe"):
            exe = exe[:-4]
        if exe not in _NETWORK_OPS:
            continue
        subs = _NETWORK_OPS[exe]
        if subs is None:
            return True
        if any(t in subs for t in tokens[1:]):
            return True
    return False


def _project_dir(explicit: Optional[str]) -> str:
    if explicit:
        return str(Path(explicit).resolve())
    # Default to the repo root (this file is inference/sandbox.py).
    return str(Path(__file__).resolve().parents[1])


def build_sandbox_argv(
    tool: str,
    command: str,
    project_dir: str,
    allow_network: bool = False,
) -> list[str]:
    """Pure arg-builder (unit-testable): wrap a shell `command` in the jail.

    bubblewrap: read-only root, project dir bound read-write, /dev /proc /tmp
    provided, network unshared, chdir into the project. firejail is the simpler
    fallback (private cwd + net=none).
    """
    if tool == "bwrap":
        argv = [
            "bwrap",
            "--ro-bind", "/", "/",            # everything read-only by default
            "--bind", project_dir, project_dir,  # except the project dir (read-write)
            "--dev", "/dev",
            "--proc", "/proc",
            "--tmpfs", "/tmp",
            "--die-with-parent",
            "--chdir", project_dir,
        ]
        if not allow_network:
            argv.append("--unshare-net")
        argv += ["/bin/sh", "-c", command]
        return argv
    if tool == "firejail":
        argv = ["firejail", "--quiet", "--private-tmp"]
        if not allow_network:
            argv.append("--net=none")
        argv += [f"--whitelist={project_dir}", "/bin/sh", "-c", command]
        return argv
    raise ValueError(f"unknown sandbox tool: {tool!r}")


def _rlimits() -> None:  # pragma: no cover - runs in the child process
    """preexec_fn: cap CPU time + address space for the child (POSIX only)."""
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (_CPU_SECONDS, _CPU_SECONDS))
        resource.setrlimit(resource.RLIMIT_AS, (_MEM_BYTES, _MEM_BYTES))
    except Exception:
        pass


@dataclass
class SandboxResult:
    stdout: str
    stderr: str
    returncode: int
    sandboxed: bool


# Log the "unsandboxed" warning once per process, not per command.
_warned_unsandboxed = False


def run_sandboxed(
    command: str,
    project_dir: Optional[str] = None,
    timeout: float = 60.0,
    allow_network: bool = False,
    output_cap: int = _OUTPUT_CAP,
) -> SandboxResult:
    """Run a shell `command` inside the jail when possible, else fall back.

    Always returns a SandboxResult (never raises on a non-zero exit — the caller
    decides how to treat returncode). `sandboxed` reports whether the jail applied.
    """
    proj = _project_dir(project_dir)
    tool = sandbox_tool() if _enabled() else None
    preexec = _rlimits if _POSIX else None

    if tool:
        argv = build_sandbox_argv(tool, command, proj, allow_network)
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, preexec_fn=preexec,
        )
        sandboxed = True
    else:
        global _warned_unsandboxed
        if not _warned_unsandboxed:
            reason = "disabled (DA_SANDBOX=0)" if not _enabled() else (
                "no bwrap/firejail" if _POSIX else "Windows-native (no namespace jail)")
            log.warning("RUN_TERMINAL sandbox unavailable — %s; running unsandboxed "
                        "(allowlist still gates auto-approval)", reason)
            _warned_unsandboxed = True
        proc = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=timeout,
            cwd=proj, preexec_fn=preexec,
        )
        sandboxed = False

    return SandboxResult(
        stdout=(proc.stdout or "")[:output_cap],
        stderr=(proc.stderr or "")[:output_cap],
        returncode=proc.returncode,
        sandboxed=sandboxed,
    )
