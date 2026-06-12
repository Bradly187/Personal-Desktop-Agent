"""files_server — voice access to recent/named files (stdio MCP skill).

File management is among the most click-heavy daily tasks; this gives the
hands-limited user "open my latest download" / "find the file called budget" by
voice. NAME search only — the Personal KB owns *content* search.

Scoped MVP: recent/find/open. Everything is confined to an allowlisted set of
user folders (Downloads, Documents, Desktop, Notes); open_file refuses paths
outside them. Move/rename are deliberately deferred — they're the destructive
half and belong behind the send-gate in a follow-up.

Run standalone:  python -m skills.servers.files_server
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("files")

_MAX_RESULTS = 15
_MAX_SCAN = 4000          # per-root file bound
_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__",
              "AppData", "$RECYCLE.BIN", ".cache"}


def _roots() -> list[Path]:
    env = os.environ.get("DA_FILES_ROOTS")
    if env:
        return [Path(p) for p in env.split(";") if Path(p).is_dir()]
    home = Path.home()
    return [p for p in (home / "Downloads", home / "Documents",
                        home / "Desktop", home / "Notes") if p.is_dir()]


def _in_roots(path: Path, roots: list[Path]) -> bool:
    target = os.path.normcase(os.path.abspath(str(path)))
    for r in roots:
        base = os.path.normcase(os.path.abspath(str(r)))
        if target == base or target.startswith(base + os.sep):
            return True
    return False


def _iter_files(roots: list[Path]):
    for root in roots:
        count = 0
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if d not in _SKIP_DIRS and not d.startswith(".")]
            for fn in filenames:
                if count >= _MAX_SCAN:
                    break
                count += 1
                yield Path(dirpath) / fn
            if count >= _MAX_SCAN:
                break


def _fmt(path: Path) -> str:
    try:
        st = path.stat()
        age_h = (time.time() - st.st_mtime) / 3600
        when = (f"{age_h * 60:.0f} min ago" if age_h < 1
                else f"{age_h:.0f} h ago" if age_h < 48
                else f"{age_h / 24:.0f} days ago")
        return f"- {path.name}  ({when}, {st.st_size // 1024} KB)  {path}"
    except OSError:
        return f"- {path}"


# ---------------------------------------------------------------------------
# Plain logic (unit-testable with tmp roots)
# ---------------------------------------------------------------------------

def _recent_files(roots: list[Path], n: int = 5) -> str:
    n = max(1, min(int(n), _MAX_RESULTS))
    files = sorted(_iter_files(roots),
                   key=lambda p: p.stat().st_mtime if p.exists() else 0,
                   reverse=True)[:n]
    return "\n".join(_fmt(p) for p in files) or "No files found."


def _find_file(roots: list[Path], name_query: str, n: int = 5) -> str:
    q = (name_query or "").strip().lower()
    if not q:
        return "What file name should I look for?"
    n = max(1, min(int(n), _MAX_RESULTS))
    hits = [p for p in _iter_files(roots) if q in p.name.lower()]
    hits.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    return "\n".join(_fmt(p) for p in hits[:n]) or f"No file names match {name_query!r}."


def _open_file(roots: list[Path], path_str: str, *, opener=None) -> str:
    path = Path((path_str or "").strip().strip("'\""))
    if not _in_roots(path, roots):
        return f"Refusing to open {path} — outside the allowed folders."
    if not path.is_file():
        return f"File not found: {path}"
    try:
        (opener or os.startfile)(str(path))   # noqa: S606 — allowlisted roots only
        return f"Opened {path.name}."
    except Exception as exc:
        return f"Could not open {path.name}: {exc}"


# ---------------------------------------------------------------------------
# MCP tool wrappers
# ---------------------------------------------------------------------------

@mcp.tool()
def recent_files(n: int = 5) -> str:
    """The most recently modified files in Downloads/Documents/Desktop/Notes
    (read-only)."""
    return _recent_files(_roots(), n)


@mcp.tool()
def find_file(name_query: str, n: int = 5) -> str:
    """Find files by NAME under the allowed folders (read-only). For content
    search, use the personal knowledge base instead."""
    return _find_file(_roots(), name_query, n)


@mcp.tool()
def open_file(path: str) -> str:
    """Open a file in its default application. Refuses paths outside the
    allowed user folders."""
    return _open_file(_roots(), path)


if __name__ == "__main__":
    mcp.run(transport="stdio")
