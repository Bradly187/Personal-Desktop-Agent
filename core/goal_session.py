"""core/goal_session.py — Goal-level authorization session for the agent pipeline.

A GoalSession lets the user authorize a high-level goal once (via voice) so that
the individual Claude Code tool calls and DevAgent steps that make up that goal
can run silently — without per-tool voice prompts.

Signal file: ~/.claude/approval/goal_session.json
  Written by: DevAgent._approve_plan_upfront() or HybridCoordinator "authorize" phrase
  Read by:    approval_hook.py (Claude Code PreToolUse) and DevAgent._confirm_destructive_op()
  Deleted by: DevAgent on plan completion/cancellation, or GoalSessionStore.cancel()

Thread safety: reads and writes use atomic replace (write-to-tmp then rename) so the
approval_hook (a subprocess) never reads a half-written file.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_SESSION_PATH = Path.home() / ".claude" / "approval" / "goal_session.json"

# High-risk shell patterns that ALWAYS require an explicit per-call voice gate,
# even inside an authorized goal session (gap #5). These are destructive,
# irreversible, or remote-code-exec shaped — auto-approving them under a broad
# "coding goal" is exactly the over-trust we want to avoid.
_HIGH_RISK_BASH: tuple[re.Pattern, ...] = tuple(re.compile(p, re.IGNORECASE) for p in (
    r"\brm\s+-[a-z]*[rf]",            # rm -rf / -fr / -r / -f
    r"\bsudo\b",
    r"\bmkfs\b", r"\bdd\b",
    r">\s*/dev/sd", r"\bof=/dev/",
    r"\bshutdown\b", r"\breboot\b", r"\bhalt\b",
    r":\s*\(\s*\)\s*\{",              # fork bomb :(){
    r"\bchmod\s+-R\b", r"\bchown\s+-R\b",
    r"\bgit\s+push\b.*--force", r"\bgit\s+push\b.*\s-f\b",
    r"\bgit\s+reset\s+--hard\b", r"\bgit\s+clean\s+-[a-z]*f",
    r"\b(curl|wget)\b.*\|\s*(sudo\s+)?(sh|bash|zsh)\b",   # curl … | sh
))


def _is_high_risk_bash(command: str) -> bool:
    """True if a shell command matches any always-gated high-risk pattern."""
    if not command:
        return False
    return any(p.search(command) for p in _HIGH_RISK_BASH)


# Allowlist (gap G): auto-approval under a goal is DENY-by-default. A Bash command
# is auto-approved only if EVERY segment of it (split on ; | & && ||) runs a
# known-safe executable. This inverts the weak denylist (which a clever command
# could slip past) into a positive list, and — because all segments must pass —
# it defeats compound-command injection like `pytest && rm -rf /` (rm is not on
# the list) and `curl x | sh` (sh is not on the list).
#
# Executables auto-approvable with any args (read-only, build, test, format).
# NOTE intentionally EXCLUDED (audit 2026-06-09): `npx` (fetches + runs
# arbitrary remote packages) and `bash`/`sh` (already absent). `git config` is
# excluded from safe git subcommands below because it can set core.hooksPath /
# alias.x='!cmd' → code exec on the next allowlisted git call.
_SAFE_BASH_EXE: frozenset[str] = frozenset({
    "ls", "dir", "pwd", "cat", "type", "head", "tail", "wc", "echo", "printf",
    "grep", "rg", "find", "tree", "diff", "sort", "uniq", "cut", "awk", "sed",
    "pytest", "tox", "nox", "coverage", "ruff", "black", "mypy", "flake8",
    "isort", "pylint", "pyright",
    "node", "npm", "yarn", "pnpm", "tsc", "eslint", "prettier",
    "cargo", "rustc", "go", "make", "cmake", "ctest",
    "true", "false", "which", "where", "whoami", "date", "env", "test",
})
# Multi-mode tools whose first SUBCOMMAND must be on the safe list. Mutating /
# remote / history-rewriting subcommands (push, reset, checkout, clean, rebase,
# install, config, …) are intentionally absent → they require explicit approval.
_SAFE_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "git": frozenset({
        "status", "diff", "log", "show", "branch", "add", "commit", "stash",
        "fetch", "rev-parse", "describe", "blame", "remote", "tag",
    }),
    "pip": frozenset({"list", "show", "freeze", "check"}),       # NOT install/uninstall
    "pip3": frozenset({"list", "show", "freeze", "check"}),
    "uv": frozenset({"run", "lock", "sync", "tree", "pip"}),
    "python": frozenset(),    # handled specially below (script ok, -c inline NOT)
    "python3": frozenset(),
    "py": frozenset(),
}
# Interpreters: a script invocation is fine, but inline-code flags are arbitrary
# execution → require approval even though the interpreter itself is "safe".
_INLINE_CODE_FLAGS: frozenset[str] = frozenset({"-c", "-e", "--eval", "--command"})
_INTERPRETERS: frozenset[str] = frozenset({"python", "python3", "py", "node"})

# Shell metacharacters that grant write/exec power the per-segment exe check
# can't see (audit 2026-06-09):
#   >, >>, <>, n>   — output redirection: `echo evil > C:\anywhere` writes any
#                     file via a "safe" exe, bypassing the cwd_scope jail.
#   $( … ), ` … `   — command substitution: `cat $(python -c "evil")` hides
#                     arbitrary code behind a safe-looking head exe.
#   <( … ), >( … )  — process substitution: same.
# Any of these → not allowlisted (require explicit approval). Detected on the
# raw command, ignoring quoted spans so a literal '>' inside a string is fine.
# Command substitution ($(…), backticks) IS expanded by bash inside DOUBLE
# quotes (only single quotes suppress it) — so for these we strip only
# single-quoted spans before scanning (#11: `echo "$(rm -rf x)"` must still be
# caught; the previous code stripped double-quoted spans too and let it slip).
_SUBST_DQ_RE = re.compile(r"\$\(|`")
# Redirection and process substitution (<(…), >(…)) are literal/disabled inside
# ANY quotes — strip both quote styles before scanning so a literal '>' inside a
# string (`echo "a > b"`) is data, not an operator.
_REDIR_PROC_RE = re.compile(r"<\(|>\(|(?<![0-9])>|>>|<>|\d+>")


def _strip_single_quoted(command: str) -> str:
    """Remove single-quoted spans (bash expands nothing inside '…')."""
    return re.sub(r"'[^']*'", "", command)


def _strip_all_quoted(command: str) -> str:
    """Remove single- and double-quoted spans. Crude but fail-safe: an
    unterminated quote leaves the tail in place (still scanned)."""
    return re.sub(r"'[^']*'|\"[^\"]*\"", "", command)


def _has_dangerous_shell_ops(command: str) -> bool:
    """True if the command uses redirection or command/process substitution the
    per-segment exe check can't see. Quote handling mirrors bash: $()/backtick
    expand inside double quotes (scan with only single quotes stripped);
    redirection and <()/>() are literal in quotes (scan with both stripped)."""
    if _SUBST_DQ_RE.search(_strip_single_quoted(command)):
        return True
    if _REDIR_PROC_RE.search(_strip_all_quoted(command)):
        return True
    return False


def _bash_is_allowlisted(command: str) -> bool:
    """True if EVERY segment of `command` runs a known-safe executable (gap G).

    Deny-by-default: unknown executables, inline interpreter code (`python -c`),
    output redirection (`> file`), command/process substitution (`$(…)`, backticks),
    unbalanced quotes, or any unsafe segment → False (requires explicit approval).
    """
    import shlex

    if not command or not command.strip():
        return False
    # Redirection / command substitution bypass the per-segment exe check
    # entirely (a safe head exe + `>` writes arbitrary files; `$(…)` runs
    # arbitrary code) — reject before splitting.
    if _has_dangerous_shell_ops(command):
        return False
    # Split on shell operators: ; | & cover ;, |, ||, &&, & (and their doublings).
    for seg in re.split(r"[;&|\n]+", command):
        seg = seg.strip()
        if not seg:
            continue
        try:
            tokens = shlex.split(seg)
        except ValueError:
            return False   # unbalanced quotes etc. → unsafe
        # Drop leading VAR=value env assignments.
        while tokens and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
            tokens = tokens[1:]
        if not tokens:
            continue
        exe = tokens[0].rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
        if exe.endswith(".exe"):
            exe = exe[:-4]
        rest = tokens[1:]

        if exe in _INTERPRETERS and any(t in _INLINE_CODE_FLAGS for t in rest):
            return False   # inline code execution → approval
        if exe in _SAFE_SUBCOMMANDS:
            if exe in _INTERPRETERS:
                continue   # interpreter with a script (no inline flag) → safe
            sub = next((t for t in rest if not t.startswith("-")), None)
            if sub not in _SAFE_SUBCOMMANDS[exe]:
                return False
        elif exe not in _SAFE_BASH_EXE:
            return False
    return True


def _path_in_scope(path: str, scopes: list[str]) -> bool:
    """True if `path` resolves under one of the allowed scope prefixes.

    Uses real-path normalisation so `..` traversal can't escape the scope.
    An empty/missing path is out of scope (fail-safe: can't validate → deny).
    """
    if not path:
        return False
    try:
        # realpath (not abspath) resolves symlinks/junctions and Windows 8.3
        # short names, so a junction planted inside the scope can't redirect a
        # write outside it (#3). abspath only collapses '..' lexically.
        target = os.path.normcase(os.path.realpath(path))
    except Exception:
        return False
    for scope in scopes:
        try:
            root = os.path.normcase(os.path.realpath(scope))
        except Exception:
            continue
        if target == root or target.startswith(root + os.sep):
            return True
    return False

# Claude Code tool names that are safe to auto-approve under any coding goal
_CODING_TOOLS: frozenset[str] = frozenset({
    "Read", "Edit", "Write", "Glob", "Grep",
    "WebFetch", "WebSearch", "Bash", "NotebookEdit",
})

# Plan goals additionally allow Agent (spawning sub-agents)
_PLAN_TOOLS: frozenset[str] = _CODING_TOOLS | frozenset({"Agent"})

# Tools never auto-approved regardless of goal (require explicit per-call voice gate)
_NEVER_AUTO: frozenset[str] = frozenset({
    "PowerShell",   # shell with broader system access — keep gated
})


def _tools_for_domain(domain: str) -> frozenset[str]:
    """Return the allowed Claude Code tool set for a given goal domain."""
    if domain == "plan":
        return _PLAN_TOOLS
    return _CODING_TOOLS   # coding / math / vision / general all get the same set


@dataclass
class GoalSession:
    goal: str
    allowed_tools: list[str]       # JSON-serialisable; frozenset on load
    expires_at: float              # wall-clock time.time() timestamp
    action_count: int = 0
    max_actions: int = 50
    domain: str = "coding"
    # Optional path scope (gap #5): when non-empty, Write/Edit are auto-approved
    # only for files resolving under one of these prefixes. Empty = unrestricted
    # (backward-compatible default).
    cwd_scope: list[str] = field(default_factory=list)

    def is_active(self) -> bool:
        return time.time() < self.expires_at and self.action_count < self.max_actions

    def allows(self, tool_name: str) -> bool:
        if tool_name in _NEVER_AUTO:
            return False
        return tool_name in self.allowed_tools and self.is_active()

    def allows_action(self, tool_name: str, tool_input: Optional[dict] = None) -> bool:
        """Tool-name allowance PLUS argument-level scoping (gap #5).

        Adds two guards on top of allows():
          - Write/Edit outside cwd_scope (when set) are NOT auto-approved.
          - Bash is DENY-by-default (gap G): auto-approved only when every segment
            of the command runs a known-safe executable (_bash_is_allowlisted) and
            it trips no high-risk pattern. Unknown commands, inline interpreter
            code (`python -c`), and compound injections (`pytest && rm -rf`) all
            fall through to the explicit voice gate.
        Everything else an authorized goal allows still auto-approves.

        NOTE: a coding goal allows `python`/`pytest`/`node`, which is arbitrary
        code execution by nature — the allowlist blocks the obvious/accidental
        dangerous commands and injection, but true isolation needs a sandbox
        (cwd jail / container), tracked as a future hardening step.
        """
        if not self.allows(tool_name):
            return False
        tool_input = tool_input or {}
        if tool_name in ("Write", "Edit") and self.cwd_scope:
            if not _path_in_scope(tool_input.get("file_path", ""), self.cwd_scope):
                log.info("GoalSession: %s outside cwd_scope — requires explicit approval",
                         tool_name)
                return False
        if tool_name == "Bash":
            cmd = tool_input.get("command", "")
            # Deny-by-default allowlist (gap G) + denylist as defense-in-depth.
            if not _bash_is_allowlisted(cmd) or _is_high_risk_bash(cmd):
                log.info("GoalSession: Bash command not allowlisted — requires explicit approval")
                return False
        return True

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "GoalSession":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class GoalSessionStore:
    """File-backed store for the active GoalSession.

    All methods are synchronous and safe to call from the approval_hook subprocess.
    Atomic writes (tmp + rename) prevent the hook from reading a partial file.
    """

    PATH: Path = _SESSION_PATH

    # ── Write ──────────────────────────────────────────────────────────────────

    @classmethod
    def create(
        cls,
        goal: str,
        domain: str = "coding",
        duration_s: float = 900.0,
        max_actions: int = 50,
        cwd_scope: Optional[list[str]] = None,
    ) -> GoalSession:
        """Create and persist a new goal session.  Overwrites any existing session.

        cwd_scope: optional path prefixes that bound auto-approved Write/Edit
        (gap #5). None/empty leaves writes unrestricted (backward-compatible).
        """
        tools = list(_tools_for_domain(domain))
        session = GoalSession(
            goal=goal,
            allowed_tools=tools,
            expires_at=time.time() + duration_s,
            action_count=0,
            max_actions=max_actions,
            domain=domain,
            cwd_scope=list(cwd_scope or []),
        )
        cls._write(session)
        log.info("GoalSession created: goal=%r domain=%s tools=%d expires_in=%.0fs",
                 goal[:60], domain, len(tools), duration_s)
        return session

    @classmethod
    def cancel(cls) -> None:
        """Delete the session file (goal completed, cancelled, or expired)."""
        try:
            cls.PATH.unlink(missing_ok=True)
            log.info("GoalSession cancelled")
        except OSError as exc:
            log.debug("GoalSession.cancel() error: %s", exc)

    # ── Read / consume ─────────────────────────────────────────────────────────

    @classmethod
    def get_active(cls) -> Optional[GoalSession]:
        """Return the active session, or None if absent/expired/exhausted."""
        session = cls._read()
        if session is None:
            return None
        if not session.is_active():
            cls.cancel()   # clean up stale file
            return None
        return session

    @classmethod
    def consume(cls) -> bool:
        """Increment action_count. Returns True if the session is still active after the increment."""
        session = cls._read()
        if session is None:
            return False
        session.action_count += 1
        if not session.is_active():
            cls.cancel()
            log.info("GoalSession exhausted after %d actions", session.action_count)
            return False
        cls._write(session)
        return True

    # ── Internal ───────────────────────────────────────────────────────────────

    @classmethod
    def _write(cls, session: GoalSession) -> None:
        cls.PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = cls.PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(session.to_dict(), indent=2), encoding="utf-8")
        os.replace(tmp, cls.PATH)   # atomic on POSIX and Windows (same volume)

    @classmethod
    def _read(cls) -> Optional[GoalSession]:
        try:
            raw = cls.PATH.read_text(encoding="utf-8")
            return GoalSession.from_dict(json.loads(raw))
        except FileNotFoundError:
            return None
        except Exception as exc:
            log.debug("GoalSession._read() error: %s", exc)
            return None
