"""arxiv_server — paper search/fetch for a ML/QC researcher (stdio MCP skill).

Open arXiv Atom API over plain urllib (no new dependencies, no auth). Search or
list recent submissions in a category; abstracts route back through the
DevAgent's on-device summariser (intent flag "summarize": true). Downloaded
PDFs land in ~/Documents/papers/ — a Personal-KB-indexed directory, so fetched
papers become searchable ("what did that paper say about error correction?").

Synergy with proactivity: "every morning at 8 brief me on new quant-ph papers"
runs through the existing scheduler → plan → SKILL_QUERY chain.

Run standalone:  python -m skills.servers.arxiv_server
"""

from __future__ import annotations

import os
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("arxiv")

_API = "http://export.arxiv.org/api/query"
_PDF_BASE = "https://arxiv.org/pdf/"
_ATOM = "{http://www.w3.org/2005/Atom}"
_MAX_RESULTS = 15
_MAX_PDF_BYTES = 50 * 1024 * 1024
_TIMEOUT_S = 15


def _papers_dir() -> Path:
    return Path(os.environ.get("DA_PAPERS_DIR") or
                (Path.home() / "Documents" / "papers"))


def _http_get(url: str, timeout: float = _TIMEOUT_S) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "personal-desktop-agent"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(_MAX_PDF_BYTES + 1)


# ---------------------------------------------------------------------------
# Plain logic (unit-testable with a fake fetch)
# ---------------------------------------------------------------------------

def _parse_feed(xml_bytes: bytes, max_abstract: int = 600) -> list[dict]:
    root = ET.fromstring(xml_bytes)
    out: list[dict] = []
    for entry in root.findall(f"{_ATOM}entry"):
        raw_id = (entry.findtext(f"{_ATOM}id") or "").rsplit("/abs/", 1)[-1]
        authors = [a.findtext(f"{_ATOM}name") or ""
                   for a in entry.findall(f"{_ATOM}author")]
        out.append({
            "id": raw_id,
            "title": " ".join((entry.findtext(f"{_ATOM}title") or "").split()),
            "authors": authors[:5],
            "published": (entry.findtext(f"{_ATOM}published") or "")[:10],
            "abstract": " ".join(
                (entry.findtext(f"{_ATOM}summary") or "").split())[:max_abstract],
        })
    return out


def _format_papers(papers: list[dict]) -> str:
    if not papers:
        return "No papers found."
    blocks = []
    for p in papers:
        who = ", ".join(p["authors"])
        blocks.append(f"[{p['id']}] {p['title']} — {who} ({p['published']})\n"
                      f"  {p['abstract']}")
    return "\n\n".join(blocks)


def _query_api(search_query: str, n: int, *, fetch=_http_get,
               sort: str = "submittedDate") -> str:
    n = max(1, min(int(n), _MAX_RESULTS))
    qs = urllib.parse.urlencode({
        "search_query": search_query, "start": 0, "max_results": n,
        "sortBy": sort, "sortOrder": "descending",
    })
    try:
        return _format_papers(_parse_feed(fetch(f"{_API}?{qs}")))
    except Exception as exc:
        return f"arXiv query failed: {exc}"


# New-style (2406.01234[v2]) and old-style (quant-ph/0301001) identifiers.
_ID_RE = re.compile(r"^(?:[a-zA-Z\-\.]+/)?\d{4}\.?\d{3,5}(v\d+)?$")


def _fetch_pdf(arxiv_id: str, dest_dir: Path, *, fetch=_http_get) -> str:
    arxiv_id = (arxiv_id or "").strip()
    if not _ID_RE.match(arxiv_id):
        return f"That doesn't look like an arXiv id: {arxiv_id!r}"
    try:
        data = fetch(f"{_PDF_BASE}{arxiv_id}")
    except Exception as exc:
        return f"Download failed: {exc}"
    if len(data) > _MAX_PDF_BYTES:
        return "Download rejected: PDF exceeds the 50 MB bound."
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / (arxiv_id.replace("/", "_") + ".pdf")
    path.write_bytes(data)
    return (f"Saved {path.name} ({len(data) // 1024} KB) to {dest_dir} — "
            f"it will be searchable after the next notes index.")


# ---------------------------------------------------------------------------
# MCP tool wrappers
# ---------------------------------------------------------------------------

@mcp.tool()
def search_papers(query: str, max_results: int = 5) -> str:
    """Search arXiv by topic; returns titles, authors, and abstracts (read-only)."""
    return _query_api(f"all:{query}", max_results, sort="relevance")


@mcp.tool()
def recent_papers(category: str = "cs.LG", max_results: int = 5) -> str:
    """Latest submissions in an arXiv category, e.g. cs.LG, quant-ph, math.OC
    (read-only)."""
    return _query_api(f"cat:{category}", max_results)


@mcp.tool()
def fetch_paper(arxiv_id: str) -> str:
    """Download a paper's PDF by arXiv id (e.g. 2406.01234) into the papers
    folder, where the personal knowledge base indexes it."""
    return _fetch_pdf(arxiv_id, _papers_dir())


if __name__ == "__main__":
    mcp.run(transport="stdio")
