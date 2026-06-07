"""ChromaDB wrapper with AgentDB fallback for semantic command memory.

Maintains a vector store of past commands and outcomes using ChromaDB
(all-MiniLM-L6-v2 embeddings). Falls back to AgentDB Jaccard scoring
when ChromaDB is unavailable or fails to initialize.

pip install chromadb
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from storage.db import AgentDB

log = logging.getLogger(__name__)


class SemanticMemory:
    """ChromaDB-backed vector store for past commands.

    Falls back to AgentDB Jaccard scoring when ChromaDB is unavailable.
    All ChromaDB calls are wrapped in try/except; failures set _available=False.

    pip install chromadb
    """

    COLLECTION_NAME = "behavioral_memory"
    CHROMA_DIR = "./chroma_db"
    CAP = 10_000
    EVICT_COUNT = 1_000

    def __init__(
        self,
        chroma_dir: str = CHROMA_DIR,
        agent_db: Optional["AgentDB"] = None,
        encoder=None,  # Optional custom SentenceTransformer; None = ChromaDB default
    ) -> None:
        self._chroma_dir = chroma_dir
        self._agent_db = agent_db
        self._encoder = encoder
        self._client = None
        self._collection = None
        self._available: bool = False
        self._bg_tasks: set = set()  # keeps fire-and-forget create_task refs alive

    async def start(self) -> bool:
        """Initialize ChromaDB client. Returns True if available."""
        try:
            import chromadb  # noqa: PLC0415 — intentional lazy import for graceful ImportError

            os.makedirs(self._chroma_dir, exist_ok=True)

            # ChromaDB PersistentClient is synchronous — run in thread to avoid blocking the loop
            client = await asyncio.to_thread(
                chromadb.PersistentClient, path=self._chroma_dir
            )

            # Build kwargs for get_or_create_collection.
            # Pin cosine space (ChromaDB defaults to L2) so query_similar's
            # distances are genuine cosine distances — mirrors CodebaseIndexer.
            # Applies to newly created collections only; an existing L2
            # behavioral_memory collection keeps L2 until it is reindexed.
            collection_kwargs: dict = {
                "name": self.COLLECTION_NAME,
                "configuration": {"hnsw": {"space": "cosine"}},
            }
            if self._encoder is not None:
                collection_kwargs["embedding_function"] = self._encoder

            collection = await asyncio.to_thread(
                client.get_or_create_collection, **collection_kwargs
            )

            self._client = client
            self._collection = collection
            self._available = True
            return True

        except Exception as exc:  # includes ImportError, OSError, ChromaDB errors
            log.warning("SemanticMemory.start() failed: %s — ChromaDB unavailable", exc)
            self._available = False
            return False

    async def add(
        self,
        text: str,
        action: str,
        source: str,
        ts: float,
        success: bool,
        doc_id: Optional[str] = None,
    ) -> None:
        """Store a command document. Schedules eviction if cap reached."""
        if not self._available or self._collection is None:
            return

        try:
            if doc_id is None:
                doc_id = hashlib.sha256(f"{text}:{action}:{ts}".encode()).hexdigest()[:16]

            metadata = {
                "action": action,
                "source": source,
                "ts": ts,
                "success": 1 if success else 0,  # ChromaDB metadata must be int/float/str
            }

            await asyncio.to_thread(
                self._collection.add,
                documents=[text],
                metadatas=[metadata],
                ids=[doc_id],
            )

            if await self.count() >= self.CAP:
                t = asyncio.create_task(self._evict_oldest())
                self._bg_tasks.add(t)
                t.add_done_callback(self._bg_tasks.discard)

        except Exception as exc:
            log.warning("SemanticMemory.add() failed: %s — disabling ChromaDB", exc)
            self._available = False

    async def query_similar(self, text: str, n: int = 5) -> list[dict]:
        """Return n most similar successful commands.

        ChromaDB path: cosine distance, filtered where success=True.
        Fallback path: AgentDB Jaccard scoring via get_recent_successful_commands().
        """
        try:
            # ── ChromaDB path ──────────────────────────────────────────────
            if self._available and self._collection is not None:
                try:
                    results = await asyncio.to_thread(
                        self._collection.query,
                        query_texts=[text],
                        n_results=n,
                        where={"success": {"$eq": 1}},
                    )
                    # ChromaDB returns parallel lists wrapped in an outer list
                    # (one entry per query text — we always send exactly one)
                    documents = results.get("documents", [[]])[0]
                    metadatas = results.get("metadatas", [[]])[0]
                    distances = results.get("distances", [[]])[0]

                    return [
                        {
                            "text": doc,
                            "action": meta.get("action", ""),
                            "source": meta.get("source", ""),
                            "ts": meta.get("ts", 0.0),
                            "distance": dist,
                            # cosine similarity (collection is pinned to cosine);
                            # kept alongside distance for caller parity
                            "score": round(1.0 - dist, 4),
                        }
                        for doc, meta, dist in zip(documents, metadatas, distances)
                    ]
                except Exception as exc:
                    log.warning(
                        "SemanticMemory.query_similar() ChromaDB failed: %s — falling back to AgentDB",
                        exc,
                    )
                    self._available = False
                    # fall through to AgentDB fallback

            # ── AgentDB Jaccard fallback ───────────────────────────────────
            if self._agent_db is None:
                return []

            candidates = await self._agent_db.get_recent_successful_commands(limit=200)

            def jaccard(a: str, b: str) -> float:
                sa = set(a.lower().split())
                sb = set(b.lower().split())
                if not sa and not sb:
                    return 0.0
                return len(sa & sb) / len(sa | sb)

            scored = [
                (jaccard(text, row.get("text", "")), row)
                for row in candidates
            ]
            scored.sort(key=lambda x: x[0], reverse=True)

            return [
                {
                    "text": row.get("text", ""),
                    "action": row.get("action", ""),
                    "source": row.get("source", ""),
                    "ts": row.get("ts", 0.0),
                    "distance": 1.0 - sim,
                    "score": round(sim, 4),
                }
                for sim, row in scored[:n]
            ]

        except Exception as exc:
            log.warning("SemanticMemory.query_similar() unhandled error: %s", exc)
            return []

    async def stop(self) -> None:
        """Release ChromaDB references. Call before process exit or tempdir cleanup.

        On Windows, SQLite WAL files remain OS-locked even after this call; callers
        should use TemporaryDirectory(ignore_cleanup_errors=True) in tests to avoid
        WinError 32. The OS releases the locks when the process exits.
        """
        self._collection = None
        self._client = None
        self._available = False
        import gc
        gc.collect()

    async def count(self) -> int:
        """Current document count. Returns 0 if ChromaDB unavailable."""
        if not self._available or self._collection is None:
            return 0
        try:
            return await asyncio.to_thread(self._collection.count)
        except Exception as exc:
            log.warning("SemanticMemory.count() failed: %s", exc)
            return 0

    async def _evict_oldest(self) -> None:
        """Remove the EVICT_COUNT oldest documents by ts metadata."""
        if not self._available or self._collection is None:
            return

        try:
            result = await asyncio.to_thread(
                self._collection.get, include=["metadatas"]
            )

            ids: list[str] = result.get("ids", [])
            metadatas: list[dict] = result.get("metadatas", [])

            # Pair each id with its ts, then sort ascending (oldest first)
            paired = sorted(
                zip(ids, metadatas),
                key=lambda x: x[1].get("ts", 0.0),
            )

            ids_to_delete = [doc_id for doc_id, _ in paired[: self.EVICT_COUNT]]

            if not ids_to_delete:
                return

            await asyncio.to_thread(self._collection.delete, ids=ids_to_delete)
            log.info("SemanticMemory: evicted %d oldest documents", len(ids_to_delete))

        except Exception as exc:
            log.warning("SemanticMemory._evict_oldest() failed: %s — disabling ChromaDB", exc)
            self._available = False

    @property
    def available(self) -> bool:
        return self._available
