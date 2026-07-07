"""Windows Action Proxy — HTTP bridge for desktop control from WSL.

Runs under Windows Python (.venv/Scripts/python.exe) and exposes an HTTP
server that executes pyautogui / win32gui actions on behalf of the WSL
main.py process.

Architecture:
    WSL main.py → CommandExecutor._proxy_execute() → POST /execute → here
                                                                       ↓
                                                     CommandExecutor._dispatch()
                                                     (Windows Python, full pyautogui)

Start (Windows PowerShell or start_windows_proxy.bat):
    .venv\\Scripts\\python.exe windows_action_proxy.py [--port 8768]

The WSL launch script sets WSL_ACTION_PROXY=http://127.0.0.1:8768 so
CommandExecutor knows to delegate here instead of calling tools directly.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# ── Project root + MCP tools on sys.path ─────────────────────────────────────
_ROOT = Path(__file__).parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "mcp_server"))

from aiohttp import web

log = logging.getLogger("action_proxy")

# Lazy import — CommandExecutor imports pyautogui/win32gui which must succeed
# here (we're on Windows Python).
_executor = None

def _get_executor():
    global _executor
    if _executor is None:
        from core.command_executor import CommandExecutor
        _executor = CommandExecutor()
    return _executor


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "pid": __import__("os").getpid()})


async def handle_execute(request: web.Request) -> web.Response:
    try:
        data: dict = await request.json()
    except Exception as exc:
        return web.json_response({"status": "error", "error": f"bad JSON: {exc}"}, status=400)

    try:
        from core.command_executor import Command
        cmd = Command(
            text=data.get("text", ""),
            action=data.get("action", ""),
            source=data.get("source", "proxy"),
            params=data.get("params") or {},
            whisper_logprob=float(data.get("whisper_logprob", 0.0)),
            gesture_confidence=float(data.get("gesture_confidence", 1.0)),
            gaze_coords=tuple(data["gaze_coords"]) if data.get("gaze_coords") else None,
            session_context=data.get("session_context") or [],
        )
        result = await _get_executor().execute(cmd)
        return web.json_response(result)
    except Exception as exc:
        log.error("proxy execute error: %s", exc, exc_info=True)
        return web.json_response({"status": "error", "error": str(exc)}, status=500)


# ---------------------------------------------------------------------------
# App wiring
# ---------------------------------------------------------------------------

def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/health", handle_health)
    app.router.add_post("/execute", handle_execute)
    return app


def main() -> None:
    ap = argparse.ArgumentParser(description="Windows desktop action proxy for WSL agent")
    ap.add_argument("--port", type=int, default=8768)
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )

    log.info("Windows Action Proxy starting on http://127.0.0.1:%d", args.port)
    log.info("Executor loaded: %s", _get_executor().__class__.__name__)
    web.run_app(build_app(), host="127.0.0.1", port=args.port, print=None)


if __name__ == "__main__":
    main()
