"""Tests for mcp_server/tools/browser.py — preview_* UI-testing primitives.

Spec: specs/browser-ui-testing/requirements.md. Playwright is an OPTIONAL dep;
the graceful-degradation, localhost-scope, and SAFE_MODE-gate paths are tested
without it (the env here has no Playwright). The real-browser smoke test is
guarded with skipif so it runs only where Playwright + Chromium are installed.
"""

from __future__ import annotations

import asyncio

import pytest

from mcp_server.tools import browser

_PW = browser._PLAYWRIGHT_AVAILABLE
_NEEDS_PW = pytest.mark.skipif(not _PW, reason="Playwright not installed")

# All preview_* tools that take no required args, for the degradation sweep.
_NOARG_TOOLS = [
    ("preview_screenshot", {}),
    ("preview_snapshot", {}),
    ("preview_console_logs", {}),
    ("preview_network", {}),
    ("preview_stop", {}),
]


# --- registry -----------------------------------------------------------------

def test_all_preview_tools_registered():
    import mcp_server.desktop_mcp_server as srv
    names = {t.name for t in asyncio.run(srv.list_tools())}
    for n in ("preview_start", "preview_screenshot", "preview_snapshot",
              "preview_click", "preview_fill", "preview_console_logs",
              "preview_network", "preview_stop"):
        assert n in names, f"{n} not registered"


# --- graceful degradation (Playwright absent) ---------------------------------

@pytest.mark.skipif(_PW, reason="only meaningful when Playwright is absent")
@pytest.mark.parametrize("fn,kwargs", _NOARG_TOOLS + [
    ("preview_start", {"url": "http://localhost:8770"}),
    ("preview_click", {"selector": "#x"}),
    ("preview_fill", {"selector": "#x", "text": "hi"}),
])
def test_degrades_when_playwright_absent(fn, kwargs):
    result = getattr(browser, fn)(**kwargs)
    assert result["ok"] is False
    assert result.get("disabled") is True
    assert "playwright not installed" in result["error"]


def test_module_import_never_crashes():
    # Importing the module + calling a tool must not raise even with no Playwright.
    assert browser.preview_snapshot()["ok"] in (True, False)


# --- localhost scoping --------------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://localhost:8770", "http://127.0.0.1:5000/x",
    "http://0.0.0.0:8080", "https://localhost/app",
])
def test_is_local_url_accepts_loopback(url):
    assert browser.is_local_url(url) is True


@pytest.mark.parametrize("url", [
    "https://evil.example.com", "http://10.0.0.5", "file:///etc/passwd",
    "ftp://localhost", "not-a-url", "",
])
def test_is_local_url_rejects_others(url):
    assert browser.is_local_url(url) is False


def test_preview_start_refuses_external_url(monkeypatch):
    # Force the "available" branch so the localhost guard (not degradation) runs;
    # an external URL is refused BEFORE the worker thread is ever touched.
    monkeypatch.setattr(browser, "_PLAYWRIGHT_AVAILABLE", True)
    r = browser.preview_start("https://evil.example.com")
    assert r["ok"] is False and "non-localhost" in r["error"]


# --- SAFE_MODE gating (dispatcher level) --------------------------------------

def test_safe_mode_blocks_click_and_fill(monkeypatch):
    import mcp_server.desktop_mcp_server as srv
    monkeypatch.setattr(srv, "SAFE_MODE", True)
    assert "SAFE_MODE" in srv._dispatch("preview_click", {"selector": "#x"})["error"]
    assert "SAFE_MODE" in srv._dispatch("preview_fill",
                                        {"selector": "#x", "text": "y"})["error"]


def test_safe_mode_does_not_block_readonly(monkeypatch):
    # Read-only preview tools are never SAFE_MODE-gated (they don't mutate).
    import mcp_server.desktop_mcp_server as srv
    monkeypatch.setattr(srv, "SAFE_MODE", True)
    r = srv._dispatch("preview_snapshot", {})
    assert "SAFE_MODE" not in str(r)  # may be disabled (no PW) but not safe-mode-blocked


# --- live smoke (only where Playwright + Chromium are installed) ---------------

@_NEEDS_PW
def test_live_preview_against_static_page(tmp_path):
    """Start a trivial local HTTP server, drive it, assert snapshot content."""
    import http.server
    import socketserver
    import threading
    import functools

    (tmp_path / "index.html").write_text(
        "<html><head><title>Probe</title></head>"
        "<body><h1 id='hdr'>hello preview</h1></body></html>",
        encoding="utf-8",
    )
    handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                                directory=str(tmp_path))
    with socketserver.TCPServer(("127.0.0.1", 0), handler) as httpd:
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            started = browser.preview_start(f"http://127.0.0.1:{port}")
            assert started["ok"] is True
            snap = browser.preview_snapshot()
            assert snap["ok"] is True
            assert snap["title"] == "Probe"
            assert "hello preview" in snap["text"]
        finally:
            browser.preview_stop()
            httpd.shutdown()
