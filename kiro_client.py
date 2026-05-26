"""kiro_client.py — Python client for the Kiro/VS Code Desktop Agent Bridge extension.

The companion TypeScript extension (kiro-extension/) runs a WebSocket server on
localhost:8767 that exposes IDE state and edit commands to the Python pipeline.

This client is designed to be:
  - Non-fatal: every method returns None/False on connection failure
  - Lazy: connects only when first used; auto-reconnects on disconnect
  - Thread-safe: uses asyncio; call from the event loop (DevAgent, plan_and_run)

Wire into DevAgent:
    kiro = KiroClient()
    dev_agent.set_kiro(kiro)

Wire into main.py:
    if args.kiro:
        kiro = KiroClient()
        dev_agent.set_kiro(kiro)

Usage:
    ctx = await kiro.get_editor_context()
    # ctx = {file, language, cursor, selection, context_above, context_below,
    #         diagnostics_at_cursor, total_lines, is_dirty}

    git = await kiro.get_git_context()
    # git = {branch, commit, ahead, behind, staged, unstaged}

    await kiro.apply_edit(file="path/to/foo.py",
                          start=(10, 0), end=(10, 20), text="new_value()")

    await kiro.run_terminal("pytest tests/ -x")
    await kiro.open_file("path/to/foo.py", line=42)
    diags = await kiro.get_diagnostics()
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Optional

log = logging.getLogger(__name__)

_DEFAULT_PORT = 8767
_CONNECT_TIMEOUT = 2.0   # seconds — fast fail if extension not running
_REQUEST_TIMEOUT = 5.0   # seconds per request


class KiroClient:
    """Async WebSocket client for the Kiro/VS Code bridge extension.

    All public methods are safe to call when the extension is not running —
    they return None and log a DEBUG message rather than raising.
    """

    def __init__(self, port: int = _DEFAULT_PORT) -> None:
        self._port = port
        self._uri = f"ws://127.0.0.1:{port}"
        self._ws: Any = None          # websockets.WebSocketClientProtocol
        self._lock = asyncio.Lock()
        self._available: Optional[bool] = None   # None = not yet tried
        self._last_check: float = 0.0
        self._check_interval: float = 30.0       # re-probe availability every 30s

    # ---------------------------------------------------------------------- #
    # Public API
    # ---------------------------------------------------------------------- #

    async def get_editor_context(self) -> Optional[dict]:
        """Return the active editor state.

        Returns dict with keys: file, language, cursor, selection,
        context_above, context_below, diagnostics_at_cursor, total_lines, is_dirty.
        Returns None if the extension is not running.
        """
        return await self._request("get_editor_context")

    async def get_git_context(self) -> Optional[dict]:
        """Return the current git repository state.

        Returns dict with keys: branch, commit, ahead, behind, staged, unstaged.
        Returns None if the extension is not running or no repo is open.
        """
        return await self._request("get_git_context")

    async def apply_edit(
        self,
        file: str,
        start: tuple[int, int],   # (line, char) 0-indexed
        end: tuple[int, int],
        text: str,
    ) -> bool:
        """Replace a range in a file via the editor (adds to undo history, triggers LSP).

        Returns True on success, False if the extension is not running.
        Raises RuntimeError if the extension rejects the edit.
        """
        result = await self._request("apply_edit", {
            "file": file,
            "range": {
                "start": {"line": start[0], "char": start[1]},
                "end": {"line": end[0], "char": end[1]},
            },
            "text": text,
        })
        return result is not None and result.get("applied", False)

    async def run_terminal(self, command: str, cwd: Optional[str] = None) -> bool:
        """Send a command to the integrated terminal.

        Returns True if the command was sent, False if extension is unavailable.
        """
        payload: dict = {"command": command}
        if cwd:
            payload["cwd"] = cwd
        result = await self._request("run_terminal", payload)
        return result is not None and result.get("sent", False)

    async def open_file(self, file: str, line: int = 0) -> bool:
        """Open a file in the editor and jump to the given line.

        Returns True on success, False if extension is unavailable.
        """
        result = await self._request("open_file", {"file": file, "line": line})
        return result is not None and result.get("opened", False)

    async def get_diagnostics(self) -> list[dict]:
        """Return all workspace errors and warnings from the language server.

        Returns empty list if extension is unavailable.
        """
        result = await self._request("get_diagnostics")
        return result if isinstance(result, list) else []

    async def ping(self) -> bool:
        """Return True if the extension is reachable."""
        result = await self._request("ping")
        return result is not None and result.get("pong", False)

    def is_available(self) -> Optional[bool]:
        """Last known availability (None = never checked)."""
        return self._available

    def format_git_context_for_prompt(self, git: dict) -> str:
        """Format git state as a fenced block for LLM prompt injection."""
        if not git or "error" in git:
            return ""
        lines = [
            "```git-context",
            f"Branch: {git.get('branch', 'unknown')}",
        ]
        ahead = git.get("ahead", 0)
        behind = git.get("behind", 0)
        if ahead or behind:
            lines.append(f"Ahead/Behind: +{ahead} / -{behind}")
        staged = git.get("staged", [])
        if staged:
            lines.append(f"Staged ({len(staged)}):")
            for f in staged[:10]:
                lines.append(f"  {f['status']} {f['path']}")
        unstaged = git.get("unstaged", [])
        if unstaged:
            lines.append(f"Unstaged ({len(unstaged)}):")
            for f in unstaged[:10]:
                lines.append(f"  {f['status']} {f['path']}")
        if git.get("merge_conflicts"):
            lines.append(f"MERGE CONFLICTS: {git['merge_conflicts']} file(s)")
        lines.append("```")
        return "\n".join(lines)

    def format_editor_context_for_prompt(self, ctx: dict) -> str:
        """Format editor state as a fenced block for LLM prompt injection."""
        if not ctx:
            return ""
        file = ctx.get("file", "")
        lang = ctx.get("language", "")
        cursor = ctx.get("cursor", {})
        sel = ctx.get("selection")
        diags = ctx.get("diagnostics_at_cursor", [])

        lines = [f"```editor-context  file={file}  lang={lang}"]
        lines.append(f"Cursor: line {cursor.get('line', 0)}, col {cursor.get('char', 0)}")
        if sel:
            lines.append(f"Selection: {sel[:200]}")
        above = ctx.get("context_above", "")
        if above.strip():
            lines.append("--- context above cursor ---")
            lines.append(above[-800:].strip())   # last 800 chars
        below = ctx.get("context_below", "")
        if below.strip():
            lines.append("--- context below cursor ---")
            lines.append(below[:800].strip())
        if diags:
            lines.append("--- diagnostics at cursor ---")
            for d in diags[:5]:
                lines.append(f"  [{d.get('severity','?').upper()}] {d.get('message','')}")
        lines.append("```")
        return "\n".join(lines)

    # ---------------------------------------------------------------------- #
    # Internal
    # ---------------------------------------------------------------------- #

    async def _request(
        self,
        action: str,
        extra: Optional[dict] = None,
    ) -> Optional[Any]:
        """Send one request, return the `data` field of the response.

        Returns None on connection failure or timeout — never raises.
        """
        # Probe availability (cached for _check_interval seconds)
        now = time.monotonic()
        if self._available is False and (now - self._last_check) < self._check_interval:
            return None   # Extension not running — don't retry yet

        try:
            return await asyncio.wait_for(
                self._do_request(action, extra or {}),
                timeout=_REQUEST_TIMEOUT,
            )
        except asyncio.TimeoutError:
            log.debug("KiroClient: request %r timed out", action)
            self._available = False
            self._last_check = time.monotonic()
            self._ws = None
            return None
        except Exception as exc:
            log.debug("KiroClient: request %r failed: %s", action, exc)
            self._available = False
            self._last_check = time.monotonic()
            self._ws = None
            return None

    async def _do_request(self, action: str, extra: dict) -> Optional[Any]:
        """Core request logic — connect if needed, send, receive."""
        try:
            import websockets  # type: ignore[import-untyped]
        except ImportError:
            log.debug("KiroClient: websockets not installed (pip install websockets)")
            self._available = False
            return None

        async with self._lock:
            # Connect / reconnect
            if self._ws is None or getattr(self._ws, "closed", True):
                try:
                    self._ws = await asyncio.wait_for(
                        websockets.connect(
                            self._uri,
                            open_timeout=_CONNECT_TIMEOUT,
                            close_timeout=1.0,
                        ),
                        timeout=_CONNECT_TIMEOUT + 0.5,
                    )
                    self._available = True
                    self._last_check = time.monotonic()
                    log.info("KiroClient: connected to %s", self._uri)
                except Exception as exc:
                    self._available = False
                    self._last_check = time.monotonic()
                    log.debug("KiroClient: cannot connect to %s: %s", self._uri, exc)
                    return None

            # Send request
            req_id = str(uuid.uuid4())[:8]
            payload = {"id": req_id, "action": action, **extra}
            await self._ws.send(json.dumps(payload))

            # Receive response
            raw = await self._ws.recv()
            resp: dict = json.loads(raw)

            if not resp.get("ok", False):
                error = resp.get("error", "unknown error")
                log.warning("KiroClient: %r error: %s", action, error)
                return None

            return resp.get("data")

    # ---------------------------------------------------------------------- #
    # Status
    # ---------------------------------------------------------------------- #

    def get_status(self) -> dict:
        return {
            "uri": self._uri,
            "available": self._available,
            "connected": self._ws is not None and not getattr(self._ws, "closed", True),
        }
