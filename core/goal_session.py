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
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_SESSION_PATH = Path.home() / ".claude" / "approval" / "goal_session.json"

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

    def is_active(self) -> bool:
        return time.time() < self.expires_at and self.action_count < self.max_actions

    def allows(self, tool_name: str) -> bool:
        if tool_name in _NEVER_AUTO:
            return False
        return tool_name in self.allowed_tools and self.is_active()

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
    ) -> GoalSession:
        """Create and persist a new goal session.  Overwrites any existing session."""
        tools = list(_tools_for_domain(domain))
        session = GoalSession(
            goal=goal,
            allowed_tools=tools,
            expires_at=time.time() + duration_s,
            action_count=0,
            max_actions=max_actions,
            domain=domain,
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
