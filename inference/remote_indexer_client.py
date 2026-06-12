"""RemoteIndexerClient — desktop-side client for the laptop indexer service.

Used by DevAgent._rag_context() when a remote indexer URL is configured and the
laptop indexer is healthy. Returns the same list-of-dicts shape that
CodebaseIndexer.query_combined() produces, so DevAgent's formatting is unchanged.

Async (aiohttp): DevAgent runs on the event loop.
"""

from __future__ import annotations

import logging
from typing import List, Optional
from urllib.parse import urlencode

import aiohttp

log = logging.getLogger(__name__)


class RemoteIndexerClient:
    def __init__(self, url: str, timeout: float = 5.0, token: Optional[str] = None) -> None:
        self._base = url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        # C2: present the shared bearer token on every query so the service
        # accepts the request (it 401s the /query/* routes otherwise).
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}

    async def _get(self, path: str, query: str, n: int) -> List[dict]:
        qs = urlencode({"q": query, "n": n})
        url = f"{self._base}{path}?{qs}"
        async with aiohttp.ClientSession(timeout=self._timeout) as session:
            async with session.get(url, headers=self._headers) as resp:
                data = await resp.json()
        return data.get("results", []) or []

    async def query_combined(self, query: str, n: int = 5) -> List[dict]:
        return await self._get("/query/combined", query, n)

    async def query_codebase(self, query: str, n: int = 5) -> List[dict]:
        return await self._get("/query/codebase", query, n)

    async def query_docs(self, query: str, n: int = 5) -> List[dict]:
        return await self._get("/query/docs", query, n)
