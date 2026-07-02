"""ChatServer access-token gate — every surface except /health requires the
token (header, query, or cookie); wrong/missing token is rejected with 401
before the handler runs. Mirrors the iPad bridge C1 pairing gate.
"""
from __future__ import annotations

import socket
import sys
from pathlib import Path

import aiohttp

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.chat_server import ChatServer, _TOKEN_COOKIE

TOKEN = "unit-test-token"


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def _booted() -> tuple[ChatServer, str]:
    port = _free_port()
    cs = ChatServer(host="127.0.0.1", port=port, token=TOKEN)
    await cs.start()
    return cs, f"http://127.0.0.1:{port}"


async def test_health_is_open_everything_else_is_gated():
    cs, base = await _booted()
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(base + "/health") as r:
                assert r.status == 200                      # liveness stays open
            for path in ("/", "/dashboard", "/api/metrics", "/api/goals"):
                async with s.get(base + path) as r:
                    assert r.status == 401, path
            async with s.post(base + "/upload") as r:
                assert r.status == 401                      # upload gated too
    finally:
        await cs.stop()


async def test_wrong_token_rejected_right_token_accepted():
    cs, base = await _booted()
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(base + "/api/metrics",
                             headers={"X-Agent-Token": "wrong"}) as r:
                assert r.status == 401
        # Fresh session per auth style so cookies can't leak between checks.
        async with aiohttp.ClientSession() as s:
            async with s.get(base + "/api/metrics",
                             headers={"X-Agent-Token": TOKEN}) as r:
                assert r.status == 200
        async with aiohttp.ClientSession() as s:
            async with s.get(base + f"/api/metrics?token={TOKEN}") as r:
                assert r.status == 200
    finally:
        await cs.stop()


async def test_query_token_sets_cookie_that_authenticates_ws():
    cs, base = await _booted()
    try:
        # unsafe=True: aiohttp's default jar drops cookies from IP origins;
        # browsers accept them from 127.0.0.1 (this mirrors browser behavior).
        jar = aiohttp.CookieJar(unsafe=True)
        async with aiohttp.ClientSession(cookie_jar=jar) as s:
            async with s.get(base + f"/?token={TOKEN}") as r:
                assert r.status in (200, 404)               # 404 = assets absent
                assert r.cookies.get(_TOKEN_COOKIE) is not None
            # Cookie now rides the handshake — no token in the WS URL.
            async with s.ws_connect(base + "/chat") as ws:
                msg = await ws.receive(timeout=2)
                assert msg.type == aiohttp.WSMsgType.TEXT
    finally:
        await cs.stop()


async def test_ws_without_token_is_rejected():
    cs, base = await _booted()
    try:
        async with aiohttp.ClientSession() as s:
            try:
                await s.ws_connect(base + "/chat")
                raise AssertionError("unauthenticated WS handshake succeeded")
            except aiohttp.WSServerHandshakeError as exc:
                assert exc.status == 401
    finally:
        await cs.stop()


def test_url_with_token_only_when_asked():
    cs = ChatServer(token=TOKEN)
    assert TOKEN not in cs.url()
    assert cs.url(with_token=True).endswith(f"?token={TOKEN}")
