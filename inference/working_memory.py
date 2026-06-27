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
    """True when DA_RESUME_MEMORY is truthy. Default ON (R3.4) — gates the
    crash-resume seed (``DevAgent._resume_seed_context``)."""
    return os.environ.get("DA_RESUME_MEMORY", "1").strip().lower() in (
        "1", "true", "yes", "on",
    )


def session_memory_enabled() -> bool:
    """True when DA_SESSION_MEMORY is truthy. Default **OFF** (R4.4) — gates the
    cross-session seed (``DevAgent._session_seed_context``), which pulls compact
    working-memory from recent *related* prior runs into a fresh plan. Independent
    of ``memory_enabled`` so each capability rolls back on its own."""
    return os.environ.get("DA_SESSION_MEMORY", "0").strip().lower() in (
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


# --------------------------------------------------------------------------- #
# Cross-session memory (specs/resume-working-memory R4): seed a *fresh* plan
# with compact working-memory from recent *related* prior runs, so a new task
# builds on what earlier runs already learned about the same files instead of
# re-investigating from zero. Same derive-don't-store discipline as above —
# pure, deterministic, no LLM, no schema change. Gated by DA_SESSION_MEMORY.
# --------------------------------------------------------------------------- #

# Trivial words carry no goal-relevance signal; dropping them keeps the Jaccard
# overlap from being dominated by filler ("the fix in", "update the").
_STOPWORDS: frozenset = frozenset({
    "a", "an", "the", "to", "of", "in", "on", "for", "and", "or", "with",
    "is", "are", "be", "it", "this", "that", "fix", "update", "add", "make",
})


def _tokens(text: str) -> set:
    """Lowercased alphanumeric word set with stopwords dropped (deterministic)."""
    out = set()
    for raw in (text or "").lower().split():
        word = "".join(ch for ch in raw if ch.isalnum())
        if word and word not in _STOPWORDS:
            out.add(word)
    return out


def score_relevance(new_goal: str, prior_goal: str) -> float:
    """Lexical similarity of two goals in [0.0, 1.0] (R4.1).

    Jaccard overlap over content-word sets — deterministic, no LLM, no
    embeddings. Identical content words → 1.0; disjoint → 0.0."""
    a, b = _tokens(new_goal), _tokens(prior_goal)
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if not inter:
        return 0.0
    return inter / len(a | b)


def select_related_runs(
    new_goal: str,
    candidate_runs: list[dict],
    *,
    top_k: int = 3,
    min_score: float = 0.2,
) -> list[dict]:
    """Pick the ``top_k`` prior runs most relevant to ``new_goal`` (R4.2).

    Scores each candidate's ``goal`` via :func:`score_relevance`, keeps those at
    or above ``min_score``, and returns them sorted by score desc then ``ts``
    desc (stable, deterministic). Empty list when nothing clears the bar — the
    caller then injects no seed (byte-identical to today)."""
    scored = []
    for run in candidate_runs or []:
        score = score_relevance(new_goal, run.get("goal", "") or "")
        if score >= min_score:
            scored.append((score, float(run.get("ts") or 0.0), run))
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return [run for _, _, run in scored[:top_k]]


def render_session_seed(
    mems: list[tuple[str, WorkingMemory]],
    *,
    max_chars: int = 2000,
) -> str:
    """Render related prior-run memories to a bounded seed block (R4.3).

    ``mems`` is ordered ``(goal, WorkingMemory)`` pairs (most-relevant first).
    One short subsection per run; deterministically ordered (files → notes →
    failure within each). Returns ``""`` when nothing renders so the caller's
    context is unchanged. The tag is distinct from ``render_seed``'s
    ``<resumed-task-memory>`` so the planner can tell "the run I'm resuming"
    from "earlier related runs"."""
    sections: list[str] = []
    for goal, mem in mems or []:
        if mem.is_empty():
            continue
        sub = [f"  prior run — {_clip(goal, 120)}"]
        if mem.files:
            sub.append("    files touched: " + ", ".join(mem.files))
        if mem.notes:
            sub.append("    observed: " + "; ".join(mem.notes))
        if mem.last_failure:
            sub.append(f"    last failure: {mem.last_failure}")
        sections.append("\n".join(sub))
    if not sections:
        return ""
    block = "\n".join(
        ['<prior-session-memory note="what earlier related runs found">']
        + sections
        + ["</prior-session-memory>"]
    )
    return block if len(block) <= max_chars else block[:max_chars] + "\n…[truncated]"
