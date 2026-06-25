"""Tests for mcp_server/tools/web.py — first-class fetch_url MCP primitive.

Spec: specs/first-class-search-tools/requirements.md. urllib is mocked so no
network is touched. Covers the scheme gate (fail-closed), HTML stripping,
truncation, and graceful network-failure handling.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from mcp_server.tools import web


class _FakeResp:
    """Context-manager stand-in for urllib's HTTPResponse."""

    def __init__(self, body: bytes, content_type: str = "text/html", status: int = 200):
        self._body = body
        self.status = status
        self.headers = MagicMock()
        self.headers.get_content_type.return_value = content_type

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body

    def getcode(self):
        return self.status


# --- scheme gate (fail-closed) ------------------------------------------------

@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://host/x", "javascript:1", ""])
def test_non_http_scheme_refused(url):
    r = web.fetch_url(url)
    assert r["ok"] is False and "http(s)" in r["error"]


# --- happy path ---------------------------------------------------------------

def test_fetch_strips_html():
    html = b"<html><head><style>x{}</style></head><body><p>Hello  world</p></body></html>"
    with patch("urllib.request.urlopen", return_value=_FakeResp(html)):
        r = web.fetch_url("https://example.com")
    assert r["ok"] is True
    assert "Hello world" in r["text"]
    assert "<p>" not in r["text"] and "style" not in r["text"]


def test_fetch_non_html_returned_raw():
    body = b'{"key": "value"}'
    with patch("urllib.request.urlopen", return_value=_FakeResp(body, content_type="application/json")):
        r = web.fetch_url("https://api.example.com/data")
    assert r["ok"] is True and '"key": "value"' in r["text"]


def test_fetch_truncates_to_max_chars():
    body = b"x" * 5000
    with patch("urllib.request.urlopen", return_value=_FakeResp(body, content_type="text/plain")):
        r = web.fetch_url("https://example.com", max_chars=100)
    assert r["ok"] is True and r["truncated"] is True
    assert "truncated at 100" in r["text"]


# --- failure handling ---------------------------------------------------------

def test_fetch_network_error_is_graceful():
    with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
        r = web.fetch_url("https://unreachable.example")
    assert r["ok"] is False and "fetch failed" in r["error"] and r["text"] == ""
