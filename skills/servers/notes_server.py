"""notes_server — voice note capture into local markdown (stdio MCP skill).

Completes the Personal KB loop: the KB *reads* ~/Notes; this skill *writes* it.
"Add a note: methotrexate at 6pm" lands in ~/Notes/inbox/ as a dated markdown
file, and "what did I write in my notes about methotrexate?" finds it after the
next KB index. Pure-local, auth-free.

All writes are path-locked under the notes root (default ~/Notes, override with
the DA_NOTES_ROOT env var) — the same containment philosophy as the executor's
writable-root allowlist. The write tools are deliberately NOT send-gated: gating
"add a note" behind a voice approval would double the speaking effort for a
hands-limited user, and the blast radius is new files inside one directory.

Run standalone:  python -m skills.servers.notes_server
"""

from __future__ import annotations

import os
import re
import time
from datetime import datetime
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("notes")

_MAX_NOTE_CHARS = 20_000
_MAX_RECENT = 20


def _notes_root() -> Path:
    return Path(os.environ.get("DA_NOTES_ROOT") or (Path.home() / "Notes"))


def _in_root(path: Path, root: Path) -> bool:
    try:
        target = os.path.normcase(os.path.abspath(str(path)))
        base = os.path.normcase(os.path.abspath(str(root)))
        return target == base or target.startswith(base + os.sep)
    except Exception:
        return False


def _slugify(text: str, max_len: int = 48) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug[:max_len].rstrip("-") or "note"


# ---------------------------------------------------------------------------
# Plain logic (unit-testable with a tmp root)
# ---------------------------------------------------------------------------

def _add_note(root: Path, text: str, title: str = "", *, now: float | None = None) -> str:
    text = (text or "").strip()[:_MAX_NOTE_CHARS]
    if not text:
        return "Nothing to save — the note was empty."
    ts = datetime.fromtimestamp(now if now is not None else time.time())
    slug = _slugify(title or " ".join(text.split()[:6]))
    inbox = root / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    path = inbox / f"{ts:%Y-%m-%d}-{slug}.md"
    i = 2
    while path.exists():
        path = inbox / f"{ts:%Y-%m-%d}-{slug}-{i}.md"
        i += 1
    if not _in_root(path, root):
        return "Note path rejected (outside the notes root)."
    header = f"# {title.strip()}\n\n" if title.strip() else ""
    path.write_text(f"{header}{text}\n\n*captured {ts:%Y-%m-%d %H:%M} by voice*\n",
                    encoding="utf-8")
    return f"Saved note {path.name}."


def _append_journal(root: Path, text: str, *, now: float | None = None) -> str:
    text = (text or "").strip()[:_MAX_NOTE_CHARS]
    if not text:
        return "Nothing to add — the entry was empty."
    ts = datetime.fromtimestamp(now if now is not None else time.time())
    journal = root / "journal"
    journal.mkdir(parents=True, exist_ok=True)
    path = journal / f"{ts:%Y-%m}.md"
    if not _in_root(path, root):
        return "Journal path rejected (outside the notes root)."
    with path.open("a", encoding="utf-8") as f:
        f.write(f"\n## {ts:%Y-%m-%d %H:%M}\n\n{text}\n")
    return f"Added a journal entry to {path.name}."


def _read_recent_notes(root: Path, n: int = 5) -> str:
    n = max(1, min(int(n), _MAX_RECENT))
    if not root.is_dir():
        return "No notes yet."
    files = sorted(
        (p for p in root.rglob("*.md")),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )[:n]
    if not files:
        return "No notes yet."
    lines = []
    for p in files:
        preview = " ".join(
            p.read_text(encoding="utf-8", errors="replace").split())[:200]
        lines.append(f"- {p.relative_to(root)}: {preview}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# MCP tool wrappers
# ---------------------------------------------------------------------------

@mcp.tool()
def add_note(text: str, title: str = "") -> str:
    """Save a new markdown note in the notes inbox (write, path-locked)."""
    return _add_note(_notes_root(), text, title)


@mcp.tool()
def append_journal(text: str) -> str:
    """Append a timestamped entry to this month's journal file (write, path-locked)."""
    return _append_journal(_notes_root(), text)


@mcp.tool()
def read_recent_notes(n: int = 5) -> str:
    """List the most recently modified notes with a short preview (read-only)."""
    return _read_recent_notes(_notes_root(), n)


if __name__ == "__main__":
    mcp.run(transport="stdio")
