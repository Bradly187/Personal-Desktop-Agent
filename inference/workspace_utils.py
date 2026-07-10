from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

_SKIP_DIRS: frozenset[str] = frozenset(
    {".git", "__pycache__", ".venv", "venv", "node_modules", ".mypy_cache",
     ".pytest_cache", ".ruff_cache", ".idea", ".vscode"}
)

def _git(repo_root: Path, args: list[str], timeout: float = 5.0) -> str | None:
    """Run a read-only git command in ``repo_root``; None on any failure."""
    try:
        r = subprocess.run(
            ["git", *args],
            cwd=str(repo_root), capture_output=True, text=True, timeout=timeout,
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("workspace_context: git %s failed: %s", args, exc)
    return None


def _git_facts(repo_root: Path, log_count: int) -> list[str]:
    """Branch + default branch + recent commit subjects + dirty-file count.

    Returns [] when ``repo_root`` is not a git repo (AGENTS.md #4) — never raises.
    The working-tree DIFF is intentionally NOT collected here (it is not stable;
    it stays in DevAgent._git_context's dynamic path)."""
    branch = _git(repo_root, ["rev-parse", "--abbrev-ref", "HEAD"])
    if branch is None:
        return []  # not a git repo (or git absent) — omit git facts entirely
    lines = [f"branch: {branch}"]
    # Default branch (origin/HEAD), best-effort.
    head = _git(repo_root, ["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"])
    if head:
        lines.append(f"default branch: {head.rsplit('/', 1)[-1]}")
    status = _git(repo_root, ["status", "--short"])
    if status is not None:
        n = len([ln for ln in status.splitlines() if ln.strip()])
        lines.append(f"uncommitted files: {n}")
    recent = _git(repo_root, ["log", f"-{max(1, log_count)}", "--oneline"])
    if recent:
        lines.append("recent commits:")
        lines.extend(f"  {ln}" for ln in recent.splitlines())
    return lines


def _layout(repo_root: Path, limit: int = 60) -> list[str]:
    """One-level repo layout: top-level dirs (skip-pruned) + key files."""
    try:
        entries = sorted(p.name + ("/" if p.is_dir() else "")
                         for p in repo_root.iterdir()
                         if p.name not in _SKIP_DIRS and not p.name.startswith("."))
    except OSError as exc:
        log.debug("workspace_context: layout failed: %s", exc)
        return []
    if not entries:
        return []
    return [" ".join(entries[:limit])]


