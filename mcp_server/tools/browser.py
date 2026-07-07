"""Browser / UI testing primitives — Playwright-backed ``preview_*`` tools.

Closes the Claude-Code parity gap for UI verification: start a headless browser
against a (local) dev server, screenshot it, inspect the DOM, click/fill, and
read console + network logs. Primary target is the agent's own chat UI
(``core/chat_server.py`` + ``web_client_chat/``), which has no automated coverage.

**Graceful degradation (CLAUDE.md convention):** Playwright is an *optional*
dependency. If it (or its Chromium build) is absent, every tool returns a clean
``{"ok": False, "disabled": True, "error": …}`` with the install hint — it never
crashes the MCP server. Install with ``pip install playwright && playwright
install chromium``.

**Thread isolation:** the MCP server calls ``_dispatch`` synchronously from
inside its asyncio loop, and Playwright's *sync* API refuses to run inside a
running event loop. So the browser session lives in a dedicated worker thread
that owns the Playwright objects and processes commands off a queue; the tool
functions marshal calls to it and block for the reply. This also gives a
persistent page across ``preview_*`` calls (start → click → snapshot).

**Safety:** ``preview_start`` refuses non-localhost URLs by default (the UI under
test is local; arbitrary external navigation is out of scope and matches the
link-safety posture). ``preview_click`` / ``preview_fill`` mutate page state and
are blocked in ``SAFE_MODE`` by the dispatcher, like ``keyboard_type``.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Any, Optional
from urllib.parse import urlparse

log = logging.getLogger(__name__)

try:
    from playwright.sync_api import sync_playwright
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised by the degradation test
    _PLAYWRIGHT_AVAILABLE = False

_INSTALL_HINT = (
    "playwright not installed — run: pip install playwright && "
    "playwright install chromium"
)

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def _disabled() -> dict:
    return {"ok": False, "disabled": True, "error": _INSTALL_HINT}


def is_local_url(url: str) -> bool:
    """True if ``url`` is an http(s) URL pointing at the loopback interface."""
    try:
        p = urlparse(url)
    except Exception:
        return False
    if p.scheme not in ("http", "https"):
        return False
    host = (p.hostname or "").lower()
    return host in _LOCAL_HOSTS


class _BrowserWorker:
    """Owns the Playwright sync session in one thread; serves commands via a queue."""

    def __init__(self) -> None:
        self._cmd_q: "queue.Queue" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, daemon=True)
                self._thread.start()

    def call(self, method: str, **kwargs: Any) -> dict:
        """Marshal a command to the worker thread and block for its reply."""
        self._ensure_thread()
        reply: "queue.Queue" = queue.Queue()
        self._cmd_q.put((method, kwargs, reply))
        return reply.get()

    # -- worker-thread internals (all Playwright access happens here) --------- #

    def _run(self) -> None:  # pragma: no cover - requires Playwright + Chromium
        pw = browser = page = None
        console_logs: list[dict] = []
        network: list[dict] = []

        def _attach(p) -> None:
            console_logs.clear()
            network.clear()
            p.on("console", lambda m: console_logs.append(
                {"type": m.type, "text": m.text}))
            p.on("requestfinished", lambda r: network.append(
                {"method": r.method, "url": r.url,
                 "status": (r.response().status if r.response() else None)}))
            p.on("requestfailed", lambda r: network.append(
                {"method": r.method, "url": r.url, "status": "failed",
                 "error": r.failure}))

        while True:
            method, kwargs, reply = self._cmd_q.get()
            try:
                if method == "start":
                    if pw is None:
                        pw = sync_playwright().start()
                    if browser is None:
                        browser = pw.chromium.launch(headless=True)
                    if page is not None:
                        page.close()
                    page = browser.new_page()
                    _attach(page)
                    resp = page.goto(kwargs["url"], wait_until="load",
                                     timeout=kwargs.get("timeout_ms", 15000))
                    reply.put({"ok": True, "url": page.url,
                               "status": resp.status if resp else None})
                elif page is None:
                    reply.put({"ok": False, "error": "no page — call preview_start first"})
                elif method == "screenshot":
                    png = page.screenshot(full_page=kwargs.get("full_page", False))
                    import base64
                    reply.put({"ok": True,
                               "image_base64": base64.b64encode(png).decode()})
                elif method == "snapshot":
                    reply.put({"ok": True, "url": page.url, "title": page.title(),
                               "text": page.inner_text("body")[:kwargs.get("max_chars", 4000)]})
                elif method == "click":
                    page.click(kwargs["selector"], timeout=kwargs.get("timeout_ms", 5000))
                    reply.put({"ok": True, "clicked": kwargs["selector"]})
                elif method == "fill":
                    page.fill(kwargs["selector"], kwargs["text"],
                              timeout=kwargs.get("timeout_ms", 5000))
                    reply.put({"ok": True, "filled": kwargs["selector"]})
                elif method == "console_logs":
                    reply.put({"ok": True, "logs": list(console_logs)})
                elif method == "network":
                    reply.put({"ok": True, "requests": list(network)})
                elif method == "stop":
                    if page is not None:
                        page.close(); page = None
                    if browser is not None:
                        browser.close(); browser = None
                    if pw is not None:
                        pw.stop(); pw = None
                    reply.put({"ok": True, "stopped": True})
                else:
                    reply.put({"ok": False, "error": f"unknown browser method {method}"})
            except Exception as exc:  # never let the worker thread die
                reply.put({"ok": False, "error": f"{method} failed: {exc}"})


_worker = _BrowserWorker()


# --------------------------------------------------------------------------- #
# Public tool functions (sync; return dicts) — mirror Claude Code's preview_*.
# --------------------------------------------------------------------------- #

def preview_start(url: str, timeout_ms: int = 15000, allow_external: bool = False) -> dict:
    """Launch headless Chromium and navigate to ``url`` (localhost-only by default)."""
    if not _PLAYWRIGHT_AVAILABLE:
        return _disabled()
    if not allow_external and not is_local_url(url):
        return {"ok": False, "error": f"refused non-localhost URL {url!r} "
                "(pass allow_external=true to override)"}
    return _worker.call("start", url=url, timeout_ms=timeout_ms)


def preview_screenshot(full_page: bool = False) -> dict:
    if not _PLAYWRIGHT_AVAILABLE:
        return _disabled()
    return _worker.call("screenshot", full_page=full_page)


def preview_snapshot(max_chars: int = 4000) -> dict:
    """Return the current page's url, title, and visible body text."""
    if not _PLAYWRIGHT_AVAILABLE:
        return _disabled()
    return _worker.call("snapshot", max_chars=max_chars)


def preview_click(selector: str) -> dict:
    if not _PLAYWRIGHT_AVAILABLE:
        return _disabled()
    return _worker.call("click", selector=selector)


def preview_fill(selector: str, text: str) -> dict:
    if not _PLAYWRIGHT_AVAILABLE:
        return _disabled()
    return _worker.call("fill", selector=selector, text=text)


def preview_console_logs() -> dict:
    if not _PLAYWRIGHT_AVAILABLE:
        return _disabled()
    return _worker.call("console_logs")


def preview_network() -> dict:
    if not _PLAYWRIGHT_AVAILABLE:
        return _disabled()
    return _worker.call("network")


def preview_stop() -> dict:
    if not _PLAYWRIGHT_AVAILABLE:
        return _disabled()
    return _worker.call("stop")
