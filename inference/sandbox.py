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
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from core.proc_utils import run_capped

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


# --------------------------------------------------------------------------- #
# Interactive-hang hardening: make a prompting command fail FAST (stdin=DEVNULL
# → immediate EOF) instead of blocking on a TTY until the 60 s wall timeout.
# Mistake-containment, same threat model as the timeout/tree-kill/rlimits above —
# so it is unconditional (no flag), additive, and never changes the semantics of
# a command the user already wrote non-interactively.
# --------------------------------------------------------------------------- #

# Tells common tools not to prompt. Merged with setdefault → never clobbers a
# value the user already set in their environment.
_NONINTERACTIVE_ENV: "dict[str, str]" = {
    "GIT_TERMINAL_PROMPT": "0",          # git never blocks on credentials
    "DEBIAN_FRONTEND": "noninteractive",  # apt/debconf suppress prompts
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    "PIP_NO_INPUT": "1",                  # pip never prompts
}

# apt's install/upgrade confirmation is NOT covered by an env var — it needs a
# flag. Only apt/apt-get and only these verbs are ever rewritten; every other
# command is passed through byte-identical (no blind mutation).
_APT_VERB_RE = re.compile(
    r"\b(apt-get|apt)\s+(install|upgrade|dist-upgrade|full-upgrade|remove|purge)\b")
_ASSUME_YES_RE = re.compile(r"(?:^|\s)(--yes|--assume-yes|-y)\b")


def noninteractive_env(base: Optional[dict] = None) -> dict:
    """Inherited env + the non-interactive vars (setdefault — user wins)."""
    env = dict(base if base is not None else os.environ)
    for k, v in _NONINTERACTIVE_ENV.items():
        env.setdefault(k, v)
    return env


def inject_noninteractive_flags(command: str) -> str:
    """Insert `-y` after an apt/apt-get install/upgrade verb when no assume-yes
    flag is already present. Allowlisted to apt only; returns `command`
    unchanged for everything else and when a yes-flag is already there
    (idempotent). Conservative: rewrites only the first such verb."""
    if not command or not _APT_VERB_RE.search(command):
        return command
    if _ASSUME_YES_RE.search(command):
        return command
    return _APT_VERB_RE.sub(lambda m: f"{m.group(1)} {m.group(2)} -y", command, count=1)


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

    # Interactive-hang hardening: rewrite apt → -y where safe, run with a
    # non-interactive env, and (via run_capped's DEVNULL default) close stdin so
    # any prompt gets immediate EOF and fails fast instead of blocking to the
    # wall timeout. Additive — a command already written non-interactively is
    # unchanged.
    command = inject_noninteractive_flags(command)
    env = noninteractive_env()

    # run_capped (not subprocess.run): on a wall-clock timeout it kills the WHOLE
    # process tree, not just the direct child, so a runaway command that forked
    # grandchildren (a shell loop, npm → node) can't leave orphans burning CPU —
    # the real gap on the Windows-native path, which has no namespace jail to
    # --die-with-parent the tree for us.
    if tool:
        argv = build_sandbox_argv(tool, command, proj, allow_network)
        proc = run_capped(
            argv, capture_output=True, text=True, timeout=timeout, preexec_fn=preexec,
            env=env, stdin=subprocess.DEVNULL,
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
        proc = run_capped(
            command, shell=True, capture_output=True, text=True, timeout=timeout,
            cwd=proj, preexec_fn=preexec, env=env, stdin=subprocess.DEVNULL,
        )
        sandboxed = False

    return SandboxResult(
        stdout=(proc.stdout or "")[:output_cap],
        stderr=(proc.stderr or "")[:output_cap],
        returncode=proc.returncode,
        sandboxed=sandboxed,
    )
