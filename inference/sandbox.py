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

Trust-sensitive fallback: trusted manual work may run below namespace isolation
after the existing scope/approval controls. Autonomous, scheduled, unknown, or
retrieval-tainted work requires a namespace jail and is denied before its requested
workload process is created when that boundary is unavailable.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum, IntEnum
from pathlib import Path
from threading import Lock
from typing import Callable, Optional

from core.proc_utils import run_capped
from core.proxy_auth import sanitized_child_env

log = logging.getLogger(__name__)

# Resource ceilings for a single RUN_TERMINAL (mistake-containment, not security).
_CPU_SECONDS = 60          # wall timeout still applies separately
_MEM_BYTES = 2 * 1024 ** 3  # 2 GB address space
_OUTPUT_CAP = 200_000       # bytes of stdout/stderr retained

_POSIX = os.name == "posix"


class ContainmentLevel(IntEnum):
    """Effective isolation strength for one terminal workload."""

    NATIVE_UNCONFINED = 0
    CWD_AND_RESOURCE_LIMITS = 1
    NAMESPACE_JAIL = 2


class WorkTrust(str, Enum):
    """Conservative provenance class used for terminal admission."""

    TRUSTED_MANUAL = "trusted_manual"
    AUTONOMOUS_OR_UNTRUSTED = "autonomous_or_untrusted"


@dataclass(frozen=True)
class ContainmentReport:
    level: ContainmentLevel
    mechanism: str
    reason: Optional[str] = None

    def to_dict(self) -> dict[str, object]:
        return {
            "level": self.level.name.lower(),
            "level_value": int(self.level),
            "mechanism": self.mechanism,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, payload: object) -> "ContainmentReport":
        if not isinstance(payload, dict):
            raise ValueError("containment report must be an object")
        level_name = payload.get("level")
        if not isinstance(level_name, str):
            raise ValueError("containment level is missing")
        try:
            level = ContainmentLevel[level_name.strip().upper()]
        except KeyError as exc:
            raise ValueError("containment level is invalid") from exc
        level_value = payload.get("level_value")
        if level_value is not None and level_value != int(level):
            raise ValueError("containment level value mismatch")
        mechanism = payload.get("mechanism")
        if not isinstance(mechanism, str) or not mechanism.strip():
            raise ValueError("containment mechanism is invalid")
        reason = payload.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise ValueError("containment reason is invalid")
        return cls(level, mechanism, reason)


class ContainmentDeniedError(PermissionError):
    """The effective containment is below the minimum for this work."""

    def __init__(self, report: ContainmentReport, work_trust: WorkTrust) -> None:
        self.report = report
        self.work_trust = work_trust
        super().__init__(
            "RUN_TERMINAL denied: "
            f"{work_trust.value} work requires namespace_jail; "
            f"effective containment is {report.level.name.lower()} "
            f"({report.mechanism}: {report.reason or 'no reason reported'})"
        )


_MANUAL_SOURCES = frozenset({
    "chat", "voice", "voice_local", "voice_correction", "touch",
    "trackpad", "gesture", "multimodal", "sound_action", "manual",
})


def work_trust_from_source(source: Optional[str]) -> WorkTrust:
    """Map existing provenance to trust; unknown/malformed values fail closed."""
    normalized = str(source or "").strip().lower()
    return (
        WorkTrust.TRUSTED_MANUAL
        if normalized in _MANUAL_SOURCES
        else WorkTrust.AUTONOMOUS_OR_UNTRUSTED
    )


def combine_work_trust(*values: WorkTrust) -> WorkTrust:
    """Trust is monotonic: one autonomous/untrusted input taints the run."""
    if any(v is WorkTrust.AUTONOMOUS_OR_UNTRUSTED for v in values):
        return WorkTrust.AUTONOMOUS_OR_UNTRUSTED
    return WorkTrust.TRUSTED_MANUAL


class ContainmentPolicy:
    """Thread-safe owner of admission, cached status, and transition notices."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._report = ContainmentReport(
            ContainmentLevel.NATIVE_UNCONFINED,
            "not_assessed",
            "no terminal workload has been assessed in this process",
        )
        self._last_state: tuple[int, str, Optional[str]] | None = None
        self._notice_sink: Optional[Callable[[str, str], None]] = None
        self._publish_metrics(self._report)

    def set_notice_sink(
        self, sink: Optional[Callable[[str, str], None]]
    ) -> None:
        with self._lock:
            self._notice_sink = sink
            # The next observed state is the first state visible to this sink.
            self._last_state = None

    def cached_report(self) -> ContainmentReport:
        with self._lock:
            return self._report

    def status(self) -> dict[str, object]:
        return self.cached_report().to_dict()

    def metrics(self) -> dict[str, float]:
        report = self.cached_report()
        return {
            "containment_level": float(report.level),
            "containment_namespace_jail": float(
                report.level is ContainmentLevel.NAMESPACE_JAIL
            ),
        }

    def observe(self, report: ContainmentReport, work_trust: WorkTrust) -> None:
        state = (int(report.level), report.mechanism, report.reason)
        sink: Optional[Callable[[str, str], None]] = None
        title = ""
        body = ""
        with self._lock:
            self._report = report
            changed = state != self._last_state
            self._last_state = state
            if changed and report.level < ContainmentLevel.NAMESPACE_JAIL:
                sink = self._notice_sink
                title = "Terminal isolation reduced"
                body = (
                    "Approved manual terminal work may continue with the current "
                    "fallback; automated terminal work is blocked until namespace "
                    "isolation is available."
                )
        self._publish_metrics(report)
        if sink is not None:
            try:
                sink(title, body)
            except Exception as exc:
                log.debug("containment notice sink failed: %s", exc)

    @staticmethod
    def _publish_metrics(report: ContainmentReport) -> None:
        try:
            from monitoring.metrics import get_metrics
            metrics = get_metrics()
            metrics.set("containment_level", float(report.level))
            metrics.set_component_status("containment", report.to_dict())
        except Exception as exc:
            log.debug("containment metrics publication failed: %s", exc)

    def require(self, report: ContainmentReport, work_trust: WorkTrust) -> None:
        self.observe(report, work_trust)
        if (
            work_trust is WorkTrust.AUTONOMOUS_OR_UNTRUSTED
            and report.level < ContainmentLevel.NAMESPACE_JAIL
        ):
            raise ContainmentDeniedError(report, work_trust)


_CONTAINMENT_POLICY: Optional[ContainmentPolicy] = None
_CONTAINMENT_POLICY_LOCK = Lock()


def get_containment_policy() -> ContainmentPolicy:
    global _CONTAINMENT_POLICY
    if _CONTAINMENT_POLICY is None:
        with _CONTAINMENT_POLICY_LOCK:
            if _CONTAINMENT_POLICY is None:
                _CONTAINMENT_POLICY = ContainmentPolicy()
    return _CONTAINMENT_POLICY


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

    GAP-3 (Pillar 1 — Egress Governance): While `DevAgent._fetch_url` uses
    an active IP blocklist to prevent SSRF against RFC-1918 / loopback, the
    terminal sandbox (`bwrap`/`firejail`) handles egress control via this coarse
    all-or-nothing boolean. It is not practical to enforce a granular IP deny-list
    at the OS namespace level without a proxy or iptables inside the jail. The
    attack surface is bounded upstream by the strict voice-approval gate for commands.
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
    """Inherited env, minus proxy auth, plus non-interactive defaults."""
    env = sanitized_child_env(base)
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


# --------------------------------------------------------------------------- #
# WSL terminal routing (specs/wsl-terminal-routing) — make the bwrap/firejail
# jail apply on a WINDOWS host by running a WSL-safe command inside WSL2 via
# wsl.exe. Routing defaults ON, remains scope-preserving (the Windows writable-
# roots check runs upstream; translation can only narrow), and reports the
# effective fallback when a command/path/tool cannot use the jail.
# --------------------------------------------------------------------------- #

# Executables that must ALWAYS run on the native Windows path (never WSL).
_WSL_FORCE_NATIVE = ("powershell", "pwsh", "cmd", "where", "wsl",
                     "explorer", "reg", "sc", "net", "tasklist", "taskkill")

# Known POSIX-friendly dev executables that are safe to run inside WSL. Under the
# default `unknown_command_policy=native`, a command is WSL-routed only if EVERY
# segment's executable is in this set (conservative — an unknown exe stays native).
_WSL_SAFE_EXECS = frozenset({
    "pytest", "python", "python3", "pip", "pip3", "uv", "pipx", "poetry",
    "ls", "cat", "grep", "rg", "find", "sed", "awk", "echo", "printf",
    "make", "bash", "sh", "node", "npm", "npx", "yarn", "pnpm", "git",
    "cargo", "rustc", "go", "gcc", "g++", "cc", "clang", "ruff", "black",
    "mkdir", "rm", "cp", "mv", "touch", "head", "tail", "wc", "sort", "uniq",
    "diff", "test", "true", "false", "env", "pwd", "cd", "tee", "xargs",
})

_DRIVE_RE = re.compile(r"^([A-Za-z]):[\\/](.*)$")
_wsl_avail_cache: "dict[str, bool]" = {}


_WSL_DEFAULT_CONFIG: dict = {"enabled": True, "distro": "Ubuntu",
                             "unknown_command_policy": "native"}


def _wsl_routing_config() -> dict:
    """Read the ``wsl_terminal_routing`` config block.

    Default (no file / no block): routing ON with distro=Ubuntu after the smoke
    gate passed 2026-06-21 (R4.1). Set ``enabled: false`` to opt out.
    """
    try:
        cfg_path = Path.home() / ".claude" / "ipad_bridge" / "config.json"
        if not cfg_path.exists():
            return _WSL_DEFAULT_CONFIG
        block = json.loads(cfg_path.read_text(encoding="utf-8")).get("wsl_terminal_routing")
        return block if isinstance(block, dict) else _WSL_DEFAULT_CONFIG
    except Exception as exc:                          # never let config break a run
        log.debug("wsl_terminal_routing config unreadable (%s)", exc)
        return _WSL_DEFAULT_CONFIG


def to_wsl_path(win_path: Optional[str]) -> Optional[str]:
    """`E:\\proj\\x` → `/mnt/e/proj/x`. Returns None for UNC / non-drive paths so
    the caller falls back to native rather than guess (R2.3 — never widens scope)."""
    if not win_path:
        return None
    p = str(win_path)
    if p.startswith("\\\\") or p.startswith("//"):    # UNC / network share
        return None
    m = _DRIVE_RE.match(p)
    if not m:
        return None
    drive, rest = m.group(1).lower(), m.group(2).replace("\\", "/").strip("/")
    return f"/mnt/{drive}/{rest}" if rest else f"/mnt/{drive}"


def command_is_wsl_safe(command: str, cfg: Optional[dict] = None) -> bool:
    """The compatibility boundary: is every segment a POSIX dev command safe to run
    in WSL? A force-native exe, an ``*.exe``, or a drive-letter-anchored invocation
    keeps the whole command on the native path (R2.1)."""
    cfg = cfg or {}
    force_native = set(_WSL_FORCE_NATIVE) | {
        str(x).lower() for x in (cfg.get("compatibility", {}) or {}).get("force_native", []) or []
    }
    policy = str(cfg.get("unknown_command_policy", "native")).lower()
    segments = [s.strip() for s in re.split(r"[;&|\n]+", command or "") if s.strip()]
    if not segments:
        return False
    for seg in segments:
        try:
            tokens = shlex.split(seg)
        except ValueError:
            return False
        while tokens and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
            tokens = tokens[1:]                        # skip leading VAR=val
        if not tokens:
            continue
        raw = tokens[0]
        if re.match(r"^[A-Za-z]:[\\/]", raw):          # C:\...\foo.exe
            return False
        exe = raw.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
        if exe.endswith(".exe") or exe in force_native:
            return False
        if exe not in _WSL_SAFE_EXECS and policy != "wsl":
            return False
    return True


def wsl_available(distro: str = "") -> bool:
    """True if WSL is present AND a jail tool (bwrap) exists inside the chosen
    distro. Cached per (distro) per process — never on the 60 Hz path (R3.2)."""
    if distro in _wsl_avail_cache:
        return _wsl_avail_cache[distro]
    ok = _probe_wsl(distro)
    _wsl_avail_cache[distro] = ok
    return ok


def _probe_wsl(distro: str) -> bool:
    if _POSIX or not shutil.which("wsl"):             # Windows-host feature only
        return False
    try:
        base = ["wsl.exe"] + (["-d", distro] if distro else [])
        r = run_capped(base + ["-e", "sh", "-c", "command -v bwrap"],
                       capture_output=True, text=True, timeout=10)
        return r.returncode == 0 and bool((r.stdout or "").strip())
    except Exception as exc:
        log.info("WSL routing: probe failed (%s) — native fallback", exc)
        return False


def build_wsl_argv(jail_argv: list, distro: str = "") -> list:
    """Wrap a jail argv (from build_sandbox_argv) in `wsl.exe [-d distro] -e …`."""
    base = ["wsl.exe"] + (["-d", distro] if distro else [])
    return base + ["-e"] + list(jail_argv)


@dataclass(frozen=True)
class _ContainmentRoute:
    report: ContainmentReport
    kind: str
    project_dir: str
    tool: Optional[str] = None
    distro: str = ""


def _select_containment(command: str, project_dir: str) -> _ContainmentRoute:
    """Resolve the effective route without launching the requested workload."""
    enabled = _enabled()
    if enabled and not _POSIX:
        cfg = _wsl_routing_config()
        if not cfg.get("enabled"):
            wsl_reason = "WSL terminal routing is disabled"
        elif not command_is_wsl_safe(command, cfg):
            wsl_reason = "command is not WSL-compatible"
        else:
            wsl_project = to_wsl_path(project_dir)
            if wsl_project is None:
                wsl_reason = "project path cannot be translated to WSL"
            else:
                distro = str(cfg.get("distro", "") or "")
                if wsl_available(distro):
                    return _ContainmentRoute(
                        ContainmentReport(
                            ContainmentLevel.NAMESPACE_JAIL,
                            "wsl+bwrap",
                            None,
                        ),
                        "wsl",
                        wsl_project,
                        tool="bwrap",
                        distro=distro,
                    )
                wsl_reason = "WSL or bwrap is unavailable"
    elif not enabled:
        wsl_reason = "disabled by DA_SANDBOX"
    elif _POSIX:
        wsl_reason = "native POSIX containment selected"
    else:  # pragma: no cover - defensive for patched platforms
        wsl_reason = "namespace routing unavailable"

    if enabled and _POSIX:
        tool = sandbox_tool()
        if tool:
            return _ContainmentRoute(
                ContainmentReport(
                    ContainmentLevel.NAMESPACE_JAIL,
                    tool,
                    None,
                ),
                "native_jail",
                project_dir,
                tool=tool,
            )

    if _POSIX:
        reason = (
            "disabled by DA_SANDBOX"
            if not enabled
            else "no bwrap or firejail executable is available"
        )
        return _ContainmentRoute(
            ContainmentReport(
                ContainmentLevel.CWD_AND_RESOURCE_LIMITS,
                "posix+rlimits",
                reason,
            ),
            "native_fallback",
            project_dir,
        )

    return _ContainmentRoute(
        ContainmentReport(
            ContainmentLevel.NATIVE_UNCONFINED,
            "windows_native",
            wsl_reason,
        ),
        "native_fallback",
        project_dir,
    )


def detect_containment(command: str, project_dir: Optional[str] = None) -> ContainmentReport:
    """Return the effective containment report for this command and cwd."""
    return _select_containment(command, _project_dir(project_dir)).report


def _run_wsl_route(
    route: _ContainmentRoute,
    command: str,
    timeout: float,
    allow_network: bool,
    env: dict,
):
    jail = build_sandbox_argv(
        route.tool or "bwrap", command, route.project_dir, allow_network
    )
    argv = build_wsl_argv(jail, route.distro)
    log.info("WSL routing: %.60s → wsl+bwrap (%s)", command, route.project_dir)
    return run_capped(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        stdin=subprocess.DEVNULL,
    )


def _rlimits() -> None:  # pragma: no cover - runs in the child process
    """preexec_fn: cap CPU time + address space for the child (POSIX only)."""
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (_CPU_SECONDS, _CPU_SECONDS))  # type: ignore[attr-defined]
        resource.setrlimit(resource.RLIMIT_AS, (_MEM_BYTES, _MEM_BYTES))  # type: ignore[attr-defined]
    except Exception:
        pass


@dataclass
class SandboxResult:
    stdout: str
    stderr: str
    returncode: int
    sandboxed: bool
    containment: Optional[ContainmentReport] = None


def run_sandboxed(
    command: str,
    project_dir: Optional[str] = None,
    timeout: float = 60.0,
    allow_network: bool = False,
    output_cap: int = _OUTPUT_CAP,
    *,
    work_trust: WorkTrust = WorkTrust.AUTONOMOUS_OR_UNTRUSTED,
    containment_policy: Optional[ContainmentPolicy] = None,
    extra_env: Optional[dict[str, str]] = None,
) -> SandboxResult:
    """Run a command only when its effective containment meets its trust policy.

    Unknown provenance defaults to autonomous/untrusted. Detection and status
    publication happen before admission; a denied target never reaches its
    workload ``run_capped`` call. ``sandboxed`` remains the compatibility view
    of whether a namespace jail applied.
    """
    proj = _project_dir(project_dir)
    try:
        trust = WorkTrust(work_trust)
    except (TypeError, ValueError):
        trust = WorkTrust.AUTONOMOUS_OR_UNTRUSTED
    policy = containment_policy or get_containment_policy()
    route = _select_containment(command, proj)
    policy.require(route.report, trust)

    command = inject_noninteractive_flags(command)
    child_env = dict(os.environ)
    if extra_env:
        child_env.update(extra_env)
    env = noninteractive_env(child_env)
    preexec = _rlimits if _POSIX else None

    if route.kind == "wsl":
        proc = _run_wsl_route(route, command, timeout, allow_network, env)
    elif route.kind == "native_jail":
        argv = build_sandbox_argv(
            route.tool or "", command, route.project_dir, allow_network
        )
        proc = run_capped(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            preexec_fn=preexec,
            env=env,
            stdin=subprocess.DEVNULL,
        )
    else:
        log.warning(
            "RUN_TERMINAL namespace jail unavailable — %s; trusted manual "
            "fallback admitted with %s",
            route.report.reason,
            route.report.level.name.lower(),
        )
        proc = run_capped(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=proj,
            preexec_fn=preexec,
            env=env,
            stdin=subprocess.DEVNULL,
        )

    return SandboxResult(
        stdout=(proc.stdout or "")[:output_cap],
        stderr=(proc.stderr or "")[:output_cap],
        returncode=proc.returncode,
        sandboxed=route.report.level is ContainmentLevel.NAMESPACE_JAIL,
        containment=route.report,
    )
