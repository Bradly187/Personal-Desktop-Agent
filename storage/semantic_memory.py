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
    REPROBE_INTERVAL_S = 60.0   # min seconds between ChromaDB re-probe attempts

    def __init__(
        self,
        chroma_dir: str = CHROMA_DIR,
        agent_db: Optional["AgentDB"] = None,
        encoder=None,  # Optional custom SentenceTransformer; None = ChromaDB default
    ) -> None:
        self._chroma_dir = chroma_dir
        self._agent_db = agent_db
        self._encoder = encoder
        import typing
        self._client: typing.Any = None
        self._collection: typing.Any = None
        self._available: bool = False
        self._bg_tasks: set = set()  # keeps fire-and-forget create_task refs alive
        # Lazy re-probe (C4): retry start() after a failure, but only once the
        # store has been started at least once, and at most once per interval.
        self._started_once: bool = False
        self._last_probe_ts: float = 0.0
        self._model_router = None

    def set_model_router(self, router) -> None:
        self._model_router = router

    def set_agent_db(self, db) -> None:
        self._agent_db = db

    async def start(self) -> bool:
        """Initialize ChromaDB client. Returns True if available."""
        self._started_once = True
        self._last_probe_ts = time.monotonic()
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

    async def _maybe_reprobe(self) -> None:
        """Lazily retry ChromaDB init after a failure, at most once per
        REPROBE_INTERVAL_S. No-op until the store has been started at least once
        — so never-started instances stay in AgentDB-fallback mode (the prior
        behaviour relied on by callers/tests)."""
        if self._available or not self._started_once:
            return
        now = time.monotonic()
        if now - self._last_probe_ts < self.REPROBE_INTERVAL_S:
            return
        self._last_probe_ts = now
        try:
            await self.start()
            if self._available:
                log.info("SemanticMemory: ChromaDB re-probe succeeded — vector store restored")
        except Exception as exc:
            log.debug("SemanticMemory: re-probe failed: %s", exc)

    async def query_similar(self, text: str, n: int = 5) -> list[dict]:
        """Return n most similar successful commands.

        ChromaDB path: cosine distance, filtered where success=True.
        Fallback path: AgentDB Jaccard scoring via get_recent_successful_commands().
        """
        await self._maybe_reprobe()
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

            candidates = await self._agent_db.commands.get_recent_successful_commands(limit=200)

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
        """Remove the EVICT_COUNT oldest documents by ts metadata.
        Synthesizes an insight via ModelRouter before deletion if available."""
        if not self._available or self._collection is None:
            return

        try:
            result = await asyncio.to_thread(
                self._collection.get, include=["metadatas", "documents"]
            )

            ids: list[str] = result.get("ids", [])
            metadatas: list[dict] = result.get("metadatas", [])
            documents: list[str] = result.get("documents", [])

            if not ids:
                return

            # Pair each id with its ts and document, then sort ascending (oldest first)
            paired = sorted(
                zip(ids, metadatas, documents),
                key=lambda x: x[1].get("ts", 0.0),
            )

            to_evict = paired[: self.EVICT_COUNT]
            ids_to_delete = [doc_id for doc_id, _, _ in to_evict]

            if not ids_to_delete:
                return

            # Memory Consolidation Reflection Loop
            if self._model_router is not None and self._agent_db is not None:
                try:
                    records_text = "\n".join(
                        f"[{meta.get('ts', 0.0)}] {meta.get('action', 'UNKNOWN')}: {doc}" 
                        for _, meta, doc in to_evict
                    )
                    prompt = (
                        "Summarize the following chronological memory records into a concise insight. "
                        "Focus on identifying patterns, preferences, and long-term behaviors.\n\n"
                        f"{records_text}"
                    )
                    inference_result = await self._model_router.infer(
                        domain="general", 
                        user_text=prompt
                    )
                    if inference_result and inference_result.text:
                        await self._agent_db.memory.insert_episodic_memory(
                            kind="insight",
                            goal="Memory Consolidation",
                            summary=inference_result.text.strip(),
                            domain="general"
                        )
                        log.info("SemanticMemory: synthesized insight from %d old records", len(to_evict))
                except Exception as insight_exc:
                    log.warning("SemanticMemory: failed to generate insight during eviction: %s", insight_exc)

            await asyncio.to_thread(self._collection.delete, ids=ids_to_delete)
            log.info("SemanticMemory: evicted %d oldest documents", len(ids_to_delete))

        except Exception as exc:
            log.warning("SemanticMemory._evict_oldest() failed: %s — disabling ChromaDB", exc)
            self._available = False

    @property
    def available(self) -> bool:
        return self._available
