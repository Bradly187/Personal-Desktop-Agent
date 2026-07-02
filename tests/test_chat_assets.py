"""specs/chat-workbench-parity R2.5 — the transcript renderer is vendored.

The markdown pipeline (marked + DOMPurify) must be served from
web_client_chat/vendor/ so the core transcript works offline; only the
optional DAG pane may import from a CDN (mermaid, pre-existing).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

_WEB = Path(__file__).parent.parent / "web_client_chat"


def test_vendor_assets_exist_and_are_real():
    marked = _WEB / "vendor" / "marked.min.js"
    purify = _WEB / "vendor" / "purify.min.js"
    assert marked.exists() and marked.stat().st_size > 10_000
    assert purify.exists() and purify.stat().st_size > 10_000
    assert "marked" in marked.read_text(encoding="utf-8", errors="replace")[:300]
    assert "DOMPurify" in purify.read_text(encoding="utf-8", errors="replace")[:300]


def test_index_references_vendored_renderer_not_cdn():
    html = (_WEB / "index.html").read_text(encoding="utf-8")
    assert "/static/vendor/marked.min.js" in html
    assert "/static/vendor/purify.min.js" in html
    # The transcript renderer must not ride a CDN (R2.5). Mermaid (DAG pane)
    # is the only allowed external import.
    for m in re.finditer(r"https?://[^\"'\s]+", html):
        assert "mermaid" in m.group(0), f"unexpected CDN reference: {m.group(0)}"


def test_chat_js_has_plaintext_fallback():
    js = (_WEB / "chat.js").read_text(encoding="utf-8")
    # R2.4 — markdown failure/absence degrades to textContent, never a blank bubble.
    assert "mdAvailable" in js and "textContent = text" in js
    # R2.2 — output is sanitized.
    assert "DOMPurify.sanitize" in js
