"""Shared file-search primitives — grep + glob.

Single source of truth backing BOTH the DevAgent ``GREP`` plan-verb
(``inference/dev_agent.py``) and the first-class ``grep`` / ``glob_files`` MCP
tools (``desktop_mcp_server.py``), so the two never drift. Pure functions, no
LLM, deterministic.

**Scope enforcement (deny-by-default for the MCP surface).** ``search_text`` /
``glob_paths`` accept an optional ``scopes`` allowlist. When given, a target
outside the allowlist is refused via the hardened realpath check
(``core.goal_session._path_in_scope`` — the single source of truth for path
boundaries, AGENTS.md #7). The MCP tools pass the writable-root allowlist so a
direct ``grep``/``glob_files`` call can't read outside it; the in-process
DevAgent verb passes ``scopes=None`` to preserve its existing repo-wide read.

Conventions mirror ``mcp_server/tools/screen.py``: a dict return shape, no async,
optional-dep-free (stdlib only).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

# Directories never worth searching (build/vendor/VCS noise). Shared by grep+glob.
_SKIP_DIRS = {
    "__pycache__", ".git", "node_modules", "venv", ".venv",
    "chroma_db", "DerivedData",
}

# Text file extensions grep scans by default (binary/asset files are skipped).
_TEXT_EXTENSIONS = {".py", ".swift", ".md", ".txt", ".json", ".yaml", ".yml"}


def _scope_ok(path: str, scopes: Optional[list[str]]) -> bool:
    """True if ``path`` is allowed: no scopes given = unrestricted (in-process)."""
    if scopes is None:
        return True
    try:
        # Reuse the hardened realpath-normalized boundary check (AGENTS.md #7).
        from core.goal_session import _path_in_scope
        return _path_in_scope(path, scopes)
    except Exception:
        # Fail-closed: if we can't validate the scope, deny (AGENTS.md #4).
        return False


def search_text(
    pattern: str,
    path: str = ".",
    max_lines: int = 100,
    scopes: Optional[list[str]] = None,
) -> dict:
    """Regex-search text files under ``path``.

    Returns ``{"ok": bool, "matches": [str], "count": int, "truncated": bool,
    "error": str|None}``. Each match is ``"file:lineno: content"``. Uses Python
    ``re`` (portable — no system ``grep`` dependency). A bad regex or
    out-of-scope path returns ``ok=False`` with an ``error`` (never raises).
    """
    if not _scope_ok(path, scopes):
        return {"ok": False, "matches": [], "count": 0, "truncated": False,
                "error": f"{path} is outside the allowed search roots"}

    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        return {"ok": False, "matches": [], "count": 0, "truncated": False,
                "error": f"invalid regex {pattern!r}: {exc}"}

    root = Path(path)
    if not root.exists():
        return {"ok": False, "matches": [], "count": 0, "truncated": False,
                "error": f"path does not exist: {path}"}

    results: list[str] = []

    def _search_file(fp: Path) -> None:
        if len(results) >= max_lines:
            return
        try:
            for lineno, line in enumerate(
                fp.read_text(encoding="utf-8", errors="replace").splitlines(),
                start=1,
            ):
                if compiled.search(line):
                    results.append(f"{fp}:{lineno}: {line.rstrip()}")
                    if len(results) >= max_lines:
                        break
        except OSError:
            pass

    if root.is_file():
        _search_file(root)
    else:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for fname in filenames:
                if Path(fname).suffix in _TEXT_EXTENSIONS:
                    _search_file(Path(dirpath) / fname)
                if len(results) >= max_lines:
                    break

    truncated = len(results) >= max_lines
    return {"ok": True, "matches": results, "count": len(results),
            "truncated": truncated, "error": None}


def glob_paths(
    pattern: str,
    path: str = ".",
    max_results: int = 200,
    scopes: Optional[list[str]] = None,
) -> dict:
    """Glob for files matching ``pattern`` under ``path``.

    Supports ``*.py`` (one level) and ``**/*.py`` (recursive) via
    ``pathlib.Path.glob``. Prunes the shared skip-dir set and returns only files.
    Returns ``{"ok": bool, "paths": [str], "count": int, "truncated": bool,
    "error": str|None}``. Sorted for deterministic output.
    """
    if not _scope_ok(path, scopes):
        return {"ok": False, "paths": [], "count": 0, "truncated": False,
                "error": f"{path} is outside the allowed search roots"}

    root = Path(path)
    if not root.exists():
        return {"ok": False, "paths": [], "count": 0, "truncated": False,
                "error": f"path does not exist: {path}"}

    try:
        hits: list[str] = []
        for p in root.glob(pattern):
            if not p.is_file():
                continue
            if any(part in _SKIP_DIRS for part in p.parts):
                continue
            hits.append(str(p))
    except (ValueError, OSError) as exc:
        return {"ok": False, "paths": [], "count": 0, "truncated": False,
                "error": f"bad glob {pattern!r}: {exc}"}

    hits.sort()
    truncated = len(hits) > max_results
    if truncated:
        hits = hits[:max_results]
    return {"ok": True, "paths": hits, "count": len(hits),
            "truncated": truncated, "error": None}


def format_grep_result(result: dict, pattern: str, path: str, max_lines: int) -> str:
    """Render a ``search_text`` result as the DevAgent GREP verb's legacy string.

    Keeps the verb's on-the-wire step-result format byte-identical so the plan
    loop / trajectory rendering are unaffected by the shared-module refactor.
    """
    if not result["ok"]:
        err = result["error"] or ""
        if err.startswith("path does not exist"):
            return f"Path does not exist: {path}"
        return err
    matches = result["matches"]
    if not matches:
        return f"No matches for pattern {pattern!r} in {path}"
    summary = f"Found {len(matches)} match(es)"
    if result["truncated"]:
        summary += f" (truncated at {max_lines})"
    return summary + "\n" + "\n".join(matches)
