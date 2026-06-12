"""diagrams_server — render and open wireframes/diagrams (stdio MCP skill).

The LLM is the *generator* (the plan/code models already write good Mermaid and
SVG); this skill is the *renderer/saver/opener*. Two kinds:

  kind="mermaid"  — flowcharts, sequence/ER/state diagrams. Saved as a
                    self-contained HTML wrapper (mermaid.js from CDN) plus the
                    raw .mmd source, opened in the default browser. No Node or
                    mermaid-cli dependency.
  kind="svg"      — UI wireframes and free-form drawings. Saved + opened as-is.

Everything lands in ~/Documents/diagrams/ (path-locked) — a Personal-KB-indexed
location, so "find my diagram about the auth flow" works via the .mmd sources.

Typical flow: "draw a diagram of the sensor pipeline" → planner generates the
Mermaid source → SKILL_QUERY diagrams create_diagram {...} → browser opens.

Run standalone:  python -m skills.servers.diagrams_server
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("diagrams")

_MAX_SOURCE_CHARS = 200_000

_HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>{title}</title>
<script type="module">
  import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs";
  mermaid.initialize({{ startOnLoad: true, theme: "neutral" }});
</script>
<style>body {{ font-family: sans-serif; margin: 2rem; }}</style>
</head>
<body>
<h2>{title}</h2>
<pre class="mermaid">
{source}
</pre>
</body>
</html>
"""


def _diagrams_dir() -> Path:
    return Path(os.environ.get("DA_DIAGRAMS_DIR") or
                (Path.home() / "Documents" / "diagrams"))


def _slugify(text: str, max_len: int = 48) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug[:max_len].rstrip("-") or "diagram"


def _in_dir(path: Path, root: Path) -> bool:
    target = os.path.normcase(os.path.abspath(str(path)))
    base = os.path.normcase(os.path.abspath(str(root)))
    return target.startswith(base + os.sep)


# ---------------------------------------------------------------------------
# Plain logic (unit-testable with a tmp dir and a no-op opener)
# ---------------------------------------------------------------------------

def _create_diagram(root: Path, title: str, source: str, kind: str = "mermaid",
                    *, opener=None) -> str:
    source = (source or "").strip()
    if not source:
        return "No diagram source provided."
    if len(source) > _MAX_SOURCE_CHARS:
        return "Diagram source too large."
    kind = (kind or "mermaid").lower()
    if kind not in ("mermaid", "svg"):
        return f"Unsupported kind {kind!r} — use 'mermaid' or 'svg'."
    if kind == "svg" and not source.lstrip().lower().startswith("<svg"):
        return "kind='svg' requires the source to be an <svg> document."

    root.mkdir(parents=True, exist_ok=True)
    slug = _slugify(title)
    stamp = time.strftime("%Y-%m-%d")
    if kind == "svg":
        out = root / f"{stamp}-{slug}.svg"
        payload = source
    else:
        # Save the raw source alongside the HTML so the Personal KB can index it.
        (root / f"{stamp}-{slug}.mmd").write_text(source, encoding="utf-8")
        out = root / f"{stamp}-{slug}.html"
        payload = _HTML_TEMPLATE.format(title=title or slug, source=source)
    if not _in_dir(out, root):
        return "Diagram path rejected (outside the diagrams folder)."
    out.write_text(payload, encoding="utf-8")
    try:
        (opener or os.startfile)(str(out))   # noqa: S606 — fixed, path-locked dir
        opened = " and opened it"
    except Exception:
        opened = ""
    return f"Saved {out.name}{opened}."


def _list_diagrams(root: Path, n: int = 10) -> str:
    if not root.is_dir():
        return "No diagrams yet."
    files = sorted(
        (p for p in root.iterdir() if p.suffix in (".html", ".svg")),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )[:max(1, min(int(n), 25))]
    return "\n".join(f"- {p.name}" for p in files) or "No diagrams yet."


# ---------------------------------------------------------------------------
# MCP tool wrappers
# ---------------------------------------------------------------------------

@mcp.tool()
def create_diagram(title: str, source: str, kind: str = "mermaid") -> str:
    """Render and open a diagram. kind='mermaid' for flowcharts/sequence/ER
    diagrams (pass Mermaid source); kind='svg' for UI wireframes (pass a full
    <svg> document). Saved into the diagrams folder (path-locked)."""
    return _create_diagram(_diagrams_dir(), title, source, kind)


@mcp.tool()
def list_diagrams(n: int = 10) -> str:
    """List the most recent saved diagrams (read-only)."""
    return _list_diagrams(_diagrams_dir(), n)


if __name__ == "__main__":
    mcp.run(transport="stdio")
