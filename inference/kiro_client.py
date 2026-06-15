"""inference.kiro_client.py — Python client for the Kiro/VS Code Desktop Agent Bridge extension.

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
    git = await kiro.get_git_context()
    # git = {branch, commit, ahead, behind, staged, unstaged}

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

    async def get_git_context(self) -> Optional[dict]:
        """Return the current git repository state.

        Returns dict with keys: branch, commit, ahead, behind, staged, unstaged.
        Returns None if the extension is not running or no repo is open.
        """
        return await self._request("get_git_context")

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
            # Connect / reconnect — use state attribute (websockets ≥14) or
            # .closed as legacy fallback so both API versions work.
            def _is_closed(ws) -> bool:
                if ws is None:
                    return True
                try:
                    from websockets.connection import State  # type: ignore[import-untyped]
                    return ws.state is not State.OPEN
                except Exception:
                    return bool(getattr(ws, "closed", True))

            if _is_closed(self._ws):
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
        try:
            from websockets.connection import State  # type: ignore[import-untyped]
            connected = self._ws is not None and self._ws.state is State.OPEN
        except Exception:
            connected = self._ws is not None and not getattr(self._ws, "closed", True)
        return {
            "uri": self._uri,
            "available": self._available,
            "connected": connected,
        }
