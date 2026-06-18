"""RemoteIndexerService — CodebaseIndexer RAG offloaded to the laptop node.

Runs on the laptop (64 GB RAM, I/O + CPU bound — ideal). Wraps the existing
CodebaseIndexer pointed at the laptop's LOCAL repo clone (no SMB share needed
thanks to the git-clone setup) and serves query results over HTTP.

Protocol:
    GET /query/combined?q=<text>&n=5  -> {"results": [ {file,chunk_type,...}, ... ]}
    GET /query/codebase?q=<text>&n=5  -> {"results": [...]}
    GET /query/docs?q=<text>&n=3      -> {"results": [...]}
    GET /health                       -> {"status":"ok","available":true,"root":...}

Run on the laptop:
    .venv-laptop\\Scripts\\python.exe inference\\remote_indexer_service.py --port 9000 [--watch]
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import logging
import os
import sys

# Ensure project root on sys.path when run as a script.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("remote_indexer")

from aiohttp import web  # noqa: E402

from inference.codebase_indexer import CodebaseIndexer  # noqa: E402


class RemoteIndexerService:
    def __init__(self, project_root: str, watch: bool = False) -> None:
        self._root = project_root
        self._watch = watch
        self._indexer: CodebaseIndexer | None = None
        self._indexed = False
        self._index_task = None

    async def start(self) -> None:
        """Init ChromaDB (fast) and schedule the full index in the background.

        Returns quickly so aiohttp opens the listening port immediately; /health
        reports indexed=false until the background index() completes. Queries
        during indexing return whatever has been embedded so far.
        """
        self._indexer = CodebaseIndexer(project_root=self._root)
        ok = await self._indexer.start()
        if not ok:
            log.error("CodebaseIndexer unavailable (ChromaDB?) — service will report not-ready")
            return
        self._index_task = asyncio.create_task(self._do_index())

    async def _do_index(self) -> None:
        try:
            stats = await self._indexer.index()
            self._indexed = True
            log.info("Indexed: %s", stats)
            if self._watch:
                try:
                    if self._indexer.start_watching():
                        log.info("File watcher active")
                except Exception as exc:
                    log.warning("File watcher unavailable: %s", exc)
        except Exception as exc:
            log.error("background indexing failed: %s", exc)

    async def stop(self) -> None:
        if self._indexer is not None:
            try:
                await self._indexer.stop()
            except Exception:
                pass

    # --- handlers ---------------------------------------------------------- #

    def _ready(self) -> bool:
        return self._indexer is not None and self._indexer.available

    async def _query(self, request: web.Request, which: str) -> web.Response:
        if not self._ready():
            return web.json_response({"results": [], "error": "indexer not ready"}, status=503)
        q = request.query.get("q", "").strip()
        if not q:
            return web.json_response({"results": [], "error": "missing q"}, status=400)
        try:
            n = int(request.query.get("n", 5))
        except ValueError:
            n = 5
        try:
            if which == "codebase":
                results = await self._indexer.query_codebase(q, n=n)
            elif which == "docs":
                results = await self._indexer.query_docs(q, n=n)
            else:
                results = await self._indexer.query_combined(q, n=n)
            return web.json_response({"results": results})
        except Exception as exc:
            log.error("query (%s) failed: %s", which, exc)
            return web.json_response({"results": [], "error": str(exc)}, status=500)

    async def handle_combined(self, request: web.Request) -> web.Response:
        return await self._query(request, "combined")

    async def handle_codebase(self, request: web.Request) -> web.Response:
        return await self._query(request, "codebase")

    async def handle_docs(self, request: web.Request) -> web.Response:
        return await self._query(request, "docs")

    async def handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({
            "status": "ok" if (self._ready() and self._indexed) else "loading",
            "available": self._ready(),
            "indexed": self._indexed,
            "root": self._root,
        })


def _make_auth_middleware(token: str):
    """Require a shared bearer token on the data-returning /query/* routes (C2).

    /health stays open so ClusterHealthMonitor can probe liveness without the
    token. Constant-time comparison; any mismatch → 401.
    """
    @web.middleware
    async def _auth(request: web.Request, handler):
        if request.path.startswith("/query"):
            supplied = request.headers.get("Authorization", "")
            if not hmac.compare_digest(supplied, f"Bearer {token}"):
                return web.json_response({"results": [], "error": "unauthorized"}, status=401)
        return await handler(request)
    return _auth


def main() -> None:
    ap = argparse.ArgumentParser(description="Remote CodebaseIndexer service (laptop node)")
    ap.add_argument("--host", default="127.0.0.1",
                    help="Bind address (default 127.0.0.1 for loopback; set to 0.0.0.0 only with --token)")
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--root", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    help="Project root to index (default: this repo clone)")
    ap.add_argument("--watch", action="store_true", help="Watch files for incremental reindex")
    ap.add_argument("--token", default=None,
                    help="Shared bearer token; falls back to the INDEXER_TOKEN env var")
    args = ap.parse_args()

    # C2: refuse to run unauthenticated. The desktop client must present the
    # same token (cluster_config.json laptop.indexer_token).
    token = os.environ.get("INDEXER_TOKEN") or args.token
    if not token:
        log.error("No indexer token set (INDEXER_TOKEN env or --token). Refusing to "
                  "start an unauthenticated indexer service.")
        sys.exit(2)

    svc = RemoteIndexerService(args.root, watch=args.watch)

    async def _on_startup(app):
        await svc.start()

    async def _on_cleanup(app):
        await svc.stop()

    app = web.Application(middlewares=[_make_auth_middleware(token)])
    app.router.add_get("/query/combined", svc.handle_combined)
    app.router.add_get("/query/codebase", svc.handle_codebase)
    app.router.add_get("/query/docs", svc.handle_docs)
    app.router.add_get("/health", svc.handle_health)
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    log.info("RemoteIndexerService listening on %s:%d  root=%s", args.host, args.port, args.root)
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
