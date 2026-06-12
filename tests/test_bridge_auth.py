"""C1 — iPad bridge pairing-token authentication.

Verifies that the WebSocket bridge rejects unauthenticated connections before
the socket is accepted (so CommandExecutor is never reached), and accepts a
connection presenting the correct token via either the ?token= query parameter
or the X-Agent-Token header.

Run:
    python -m pytest tests/test_bridge_auth.py -q
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import aiohttp
import pytest

from core.ipad_bridge import IPadBridge

BRIDGE_PORT = 8771  # unique to this test module
_TOKEN = "correct-horse-battery-staple"
_BASE = f"ws://localhost:{BRIDGE_PORT}/ws"


@contextlib.asynccontextmanager
async def _running_bridge(token: str = _TOKEN):
    """Start an in-process bridge with a known token; tear it down cleanly."""
    bridge = IPadBridge(port=BRIDGE_PORT, token=token)
    task = asyncio.create_task(bridge.run(no_mdns=True))
    await asyncio.sleep(0.8)  # let TCPSite bind
    try:
        yield bridge
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.1)  # let the port release before the next test


async def test_no_token_rejected():
    """A connection with no token is rejected with HTTP 401."""
    async with _running_bridge():
        async with aiohttp.ClientSession() as session:
            with pytest.raises(aiohttp.WSServerHandshakeError) as exc:
                await session.ws_connect(_BASE)
            assert exc.value.status == 401


async def test_wrong_token_rejected():
    """A connection with the wrong token is rejected with HTTP 401."""
    async with _running_bridge():
        async with aiohttp.ClientSession() as session:
            with pytest.raises(aiohttp.WSServerHandshakeError) as exc:
                await session.ws_connect(_BASE + "?token=not-the-token")
            assert exc.value.status == 401


async def test_correct_token_query_accepted():
    """The correct token via ?token= is accepted and yields the welcome status."""
    async with _running_bridge():
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(_BASE + f"?token={_TOKEN}") as ws:
                msg = await asyncio.wait_for(ws.receive_json(), timeout=5.0)
                assert msg.get("type") == "status"


async def test_correct_token_header_accepted():
    """The correct token via the X-Agent-Token header is accepted."""
    async with _running_bridge():
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(
                _BASE, headers={"X-Agent-Token": _TOKEN}
            ) as ws:
                msg = await asyncio.wait_for(ws.receive_json(), timeout=5.0)
                assert msg.get("type") == "status"


async def test_executor_never_reached_without_token():
    """On the unauthorized path the handshake fails — the executor is never invoked."""
    async with _running_bridge() as bridge:
        with patch.object(bridge._executor, "execute") as spy:
            async with aiohttp.ClientSession() as session:
                with pytest.raises(aiohttp.WSServerHandshakeError):
                    await session.ws_connect(_BASE + "?token=wrong")
            spy.assert_not_called()
