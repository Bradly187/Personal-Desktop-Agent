"""First-class web-fetch primitive for the MCP tool surface.

Exposes ``fetch_url`` so Claude can pull a page's text directly, rather than the
fetch being reachable only as a DevAgent ``FETCH_URL`` plan step. Stdlib-only
(``urllib``) and synchronous, so it slots into the MCP server's sync ``_dispatch``
and its returned ``text`` is automatically run through the server's existing
``MCPTrustClassifier`` (injection screen) in ``call_tool`` — no second copy of
that logic here.

Note: this is intentionally a *separate, simpler* implementation from
``DevAgent._fetch_url`` (which is async/aiohttp and lives inside the plan loop's
trust-scan). Both screen their output; this one bounds the injection surface by
capping the returned text. Conventions mirror ``mcp_server/tools/screen.py``.
"""

from __future__ import annotations

import logging
import re
import urllib.request
from urllib.error import URLError

log = logging.getLogger(__name__)

_USER_AGENT = "Mozilla/5.0 (compatible; DesktopAgent/1.0)"
_DEFAULT_MAX_CHARS = 4000
_DEFAULT_TIMEOUT = 10.0


def _strip_html(html: str) -> str:
    """Very simple HTML → plain text: strip script/style, tags, collapse space."""
    clean = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html,
                   flags=re.DOTALL | re.IGNORECASE)
    clean = re.sub(r"<[^>]+>", " ", clean)
    clean = re.sub(r"[ \t]+", " ", clean)
    clean = re.sub(r"\n{3,}", "\n\n", clean)
    return clean.strip()


def fetch_url(
    url: str,
    max_chars: int = _DEFAULT_MAX_CHARS,
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict:
    """Fetch ``url`` and return its extracted text.

    Returns ``{"ok": bool, "url": str, "text": str, "truncated": bool,
    "status": int|None, "error": str|None}``. Only ``http(s)`` URLs are allowed
    (no ``file://`` / ``ftp://`` — fail-closed, AGENTS.md #4). Never raises; a
    network/HTTP failure comes back as ``ok=False`` with an ``error``.
    """
    if not isinstance(url, str) or not url.lower().startswith(("http://", "https://")):
        return {"ok": False, "url": url, "text": "", "truncated": False,
                "status": None, "error": "only http(s) URLs are allowed"}

    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (scheme gated above)
            status = getattr(resp, "status", None) or resp.getcode()
            ctype = resp.headers.get_content_type() if resp.headers else ""
            raw = resp.read()
        body = raw.decode("utf-8", errors="replace")
        text = _strip_html(body) if "html" in (ctype or "") else body
        truncated = len(text) > max_chars
        if truncated:
            text = text[:max_chars] + f"\n… [truncated at {max_chars}]"
        log.info("fetch_url: %s (%d chars, status %s)", url, len(text), status)
        return {"ok": True, "url": url, "text": text, "truncated": truncated,
                "status": status, "error": None}
    except (URLError, OSError, ValueError) as exc:
        return {"ok": False, "url": url, "text": "", "truncated": False,
                "status": None, "error": f"fetch failed: {exc}"}
