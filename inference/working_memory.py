"""Resume working-memory snapshot (specs/resume-working-memory, Gap C).

When the agent resumes a crashed plan, ``DevAgent.resume_pending_plan`` today
re-plans the goal from scratch with no memory of what the interrupted run already
did. The reference (mini-coding-agent) keeps a small ``memory{task, files, notes}``
alongside the full transcript and replays it on resume. PDA's transcript is already
durable (``agent_steps``), so this module **derives** the same compact snapshot
from those persisted steps at resume time — no new table, no schema change.

Pure / deterministic / no LLM. ``summarize_run`` reduces a run's steps to a
``WorkingMemory``; ``render_seed`` renders it to a bounded text block that
``DevAgent`` injects as ``seed_context`` into the resumed plan. Gated by
``DA_RESUME_MEMORY`` (default OFF) — when off, resume is byte-identical to today.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# Verbs whose `args` names a file path worth remembering as "touched".
_PATH_VERBS: frozenset = frozenset({"READ_FILE", "WRITE_FILE", "EDIT_FILE"})


def memory_enabled() -> bool:
    """True when DA_RESUME_MEMORY is explicitly truthy. Default ON (R3.4)."""
    return os.environ.get("DA_RESUME_MEMORY", "1").strip().lower() in (
        "1", "true", "yes", "on",
    )


@dataclass
class WorkingMemory:
    """Compact, resume-time snapshot derived from a run's persisted steps."""
    goal: str = ""
    files: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    last_failure: str | None = None

    def is_empty(self) -> bool:
        return not (self.files or self.notes or self.last_failure)


def _clip(text: str, limit: int) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def summarize_run(
    goal: str,
    steps: list[dict],
    *,
    max_files: int = 8,
    max_notes: int = 5,
    max_chars: int = 1500,
    note_chars: int = 160,
) -> WorkingMemory:
    """Derive a :class:`WorkingMemory` from a run's ordered steps (R1.1).

    Deterministic; no LLM. ``files`` = the ``max_files`` most-recent DISTINCT paths
    touched by path verbs; ``notes`` = the ``max_notes`` most-recent non-empty
    result snippets; ``last_failure`` = the last failed step's result (R1.2). The
    rendered block is bounded by ``max_chars`` in :func:`render_seed`.
    """
    if not steps:
        return WorkingMemory(goal=goal)

    files: list[str] = []          # most-recent-first, distinct
    notes: list[str] = []          # most-recent-first
    last_failure: str | None = None

    for s in reversed(steps):
        action = str(s.get("action", "")).upper()
        args = (s.get("args") or "").strip()
        result = s.get("result") or ""
        success = s.get("success")

        if action in _PATH_VERBS and args:
            path = args.split()[0].strip("'\"") if action == "GREP" else args.strip("'\"")
            if path and path not in files and len(files) < max_files:
                files.append(path)

        if last_failure is None and success in (False, 0):
            # Full failure text preserved within the block budget (R1.2).
            last_failure = _clip(result, max_chars)

        if result and result.strip() and len(notes) < max_notes:
            notes.append(f"{action} {args[:40]}".strip() + f" → {_clip(result, note_chars)}")

    files.reverse()
    notes.reverse()
    return WorkingMemory(goal=goal, files=files, notes=notes, last_failure=last_failure)


def render_seed(mem: WorkingMemory, *, max_chars: int = 1500) -> str:
    """Render a :class:`WorkingMemory` to a bounded, deterministically-ordered seed
    block for prompt injection (R1.3). Returns ``""`` when the memory is empty so
    the caller's context is unchanged."""
    if mem.is_empty():
        return ""
    lines = ["<resumed-task-memory note=\"what the interrupted run already did\">"]
    if mem.files:
        lines.append("files already touched: " + ", ".join(mem.files))
    if mem.notes:
        lines.append("recent steps:")
        lines.extend(f"  - {n}" for n in mem.notes)
    if mem.last_failure:
        lines.append(f"last failure (resume here): {mem.last_failure}")
    lines.append("</resumed-task-memory>")
    block = "\n".join(lines)
    return block if len(block) <= max_chars else block[:max_chars] + "\n…[truncated]"
