"""Live repo-context ingestion (specs/repo-context-ingestion, Gap A).

Collects the *stable* workspace facts a planner should know before it plans —
the repo layout, the project's own AGENTS.md/CLAUDE.md house rules, README +
manifest excerpts, and git branch/log — into one clipped, deterministic text
block. This is the PDA counterpart of mini-coding-agent's ``WorkspaceContext``.

**Pure / deterministic / no LLM.** Identical inputs yield byte-identical output.
Every git call and file read degrades to omission on failure (AGENTS.md #4 —
never crash the plan path). Reads ONLY inside ``repo_root`` (AGENTS.md #7) — a
symlink or ``..`` escaping the root is skipped, not followed.

The block is built at most once per ``DevAgent`` and memoized (the facts are
stable for the session); ``DevAgent`` prepends it to the plan ``extra_ctx`` ahead
of the dynamic RAG/git-status context. Gated by ``DA_REPO_CONTEXT`` — default OFF
until the eval baseline locks; when off, plan prompts are byte-identical to today.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from inference.workspace_utils import _git_facts, _layout

log = logging.getLogger(__name__)

# Stable docs to excerpt, in order. AGENTS.md first — it is the behavioral rules
# the planner is supposed to obey but never saw before this feature.
_DEFAULT_INGEST: tuple[str, ...] = ("AGENTS.md", "CLAUDE.md", "README.md")
# First present of these is included as the dependency/manifest fact.
_MANIFESTS: tuple[str, ...] = (
    "pyproject.toml", "requirements.txt", "package.json", "setup.py",
)


def _clip(text: str, limit: int) -> str:
    """Truncate to ``limit`` chars with a visible marker (matches DevAgent style)."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… [truncated at {limit} chars]"


def _resolve_in_root(repo_root: Path, name: str) -> Path | None:
    """Resolve ``name`` under ``repo_root``, refusing anything that escapes it
    (symlink / ``..``). Returns the resolved path or None (AGENTS.md #7)."""
    try:
        root = repo_root.resolve()
        target = (root / name).resolve()
        # Python 3.9+: is_relative_to
        if target == root or root in target.parents:
            return target
    except (OSError, ValueError) as exc:
        log.debug("workspace_context: refuse %r: %s", name, exc)
    return None


def build_workspace_context(
    repo_root: str | os.PathLike | None = None,
    *,
    max_chars: int = 6000,
    per_file_chars: int = 1200,
    log_count: int = 5,
    ingest: tuple[str, ...] = _DEFAULT_INGEST,
) -> tuple[str, dict]:
    """Build the stable workspace-facts block. Deterministic; reads only inside
    ``repo_root``; git/file failures degrade to omission, never raise.

    Returns ``(block_text, stats)`` where stats =
    ``{has_git, files_read, chars_out, truncated}``. ``block_text`` is ``""`` when
    nothing could be collected.
    """
    root = Path(repo_root) if repo_root is not None else Path(os.getcwd())
    sections: list[str] = []
    stats = {"has_git": False, "files_read": 0, "chars_out": 0, "truncated": False}

    git_lines = _git_facts(root, log_count)
    if git_lines:
        stats["has_git"] = True
        sections.append("## Git state\n" + "\n".join(git_lines))

    layout = _layout(root)
    if layout:
        sections.append("## Repo layout (top level)\n" + "\n".join(layout))

    for name in (*ingest, _first_manifest(root)):
        if not name:
            continue
        target = _resolve_in_root(root, name)
        if target is None or not target.is_file():
            continue
        try:
            text = target.read_text(encoding="utf-8", errors="replace").strip()
        except OSError as exc:
            log.debug("workspace_context: read %s failed: %s", name, exc)
            continue
        if not text:
            continue
        sections.append(f"## {name}\n{_clip(text, per_file_chars)}")
        stats["files_read"] += 1

    if not sections:
        return "", stats

    block = (
        "<workspace-context note=\"stable repo facts; honor AGENTS.md/CLAUDE.md "
        "rules below\">\n" + "\n\n".join(sections) + "\n</workspace-context>"
    )
    clipped = _clip(block, max_chars)
    stats["truncated"] = len(clipped) != len(block)
    stats["chars_out"] = len(clipped)
    return clipped, stats


def _first_manifest(root: Path) -> str:
    for m in _MANIFESTS:
        if _resolve_in_root(root, m) is not None and (root / m).is_file():
            return m
    return ""
