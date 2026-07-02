"""ChatServer boots, serves the UI + /health, and completes a WS handshake.

Exercises the server half end-to-end (aiohttp app, static serving, WebSocket
upgrade, the initial 'ready' frame) without the heavy LLM pipeline.
"""
from __future__ import annotations

import json
import socket
import sys
from pathlib import Path

import aiohttp

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.chat_server import ChatServer


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def test_chat_server_boot_serves_ui_and_ws():
    port = _free_port()
    cs = ChatServer(host="127.0.0.1", port=port, token="test-token")
    await cs.start()
    try:
        base = f"http://127.0.0.1:{port}"
        # unsafe jar: aiohttp's default drops cookies from IP origins; browsers
        # accept them from 127.0.0.1, and the cookie must reach the WS below.
        jar = aiohttp.CookieJar(unsafe=True)
        async with aiohttp.ClientSession(cookie_jar=jar) as session:
            async with session.get(base + "/health") as r:
                assert r.status == 200
                assert (await r.json())["status"] == "ok"

            # First hit carries the token in the URL (as _open_chat_shell does);
            # the response cookie authenticates everything after, incl. the WS.
            async with session.get(base + "/?token=test-token") as r:
                assert r.status == 200
                html = await r.text()
                assert "Desktop Agent" in html      # index.html served

            async with session.ws_connect(base + "/chat") as ws:
                msg = await ws.receive(timeout=2)
                data = json.loads(msg.data)
                assert data["type"] == "ready"
                assert "allow_destructive" in data["config"]
    finally:
        await cs.stop()
    assert cs.is_healthy() is False
