from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from abc import ABC, abstractmethod
from contextvars import ContextVar
from typing import Any, Optional

from core.command_executor import Command

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# VLLMEmbedder — vLLM LLM.encode() for semantic memory / codebase RAG
# ---------------------------------------------------------------------------

class VLLMEmbedder:
    """In-process embedding via vLLM's pooling API (LLM.encode).

    Replaces sentence-transformers (all-MiniLM-L6-v2) in SemanticMemory and
    CodebaseIndexer. Uses a dedicated small embedding model that stays resident
    at ~0.5–1 GB VRAM — negligible overhead alongside the specialist pool.

    Recommended models (HuggingFace):
        nomic-ai/nomic-embed-text-v1.5   — 137M params, 768-dim, best retrieval/size
        BAAI/bge-m3                      — 570M params, 1024-dim, best absolute quality
        Qwen/Qwen3-Embedding-0.6B        — 600M params, 1024-dim, multilingual

    The encoder is created lazily on first encode() call. It stays loaded
    permanently — embedding requests are cheap and frequent.

    Usage:
        embedder = VLLMEmbedder()
        vecs = await embedder.encode(["click the save button", "open terminal"])
        # vecs: list of numpy arrays, shape (dim,)
    """

    _GPU_UTIL: float = 0.05   # ~1.6 GB for a 0.5-1B embedding model on 32 GB

    def __init__(
        self,
        model: str = "nomic-ai/nomic-embed-text-v1.5",
        gpu_memory_utilization: float | None = None,
    ) -> None:
        self.model = model
        self._gpu_util = gpu_memory_utilization if gpu_memory_utilization is not None else self._GPU_UTIL
        self._llm: Any = None
        self._load_lock = asyncio.Lock()
        self._dim: int | None = None

    async def _ensure_loaded(self) -> None:
        if self._llm is not None:
            return
        async with self._load_lock:
            if self._llm is not None:
                return
            self._llm = await asyncio.to_thread(self._blocking_load)
            log.info("VLLMEmbedder: ready — %s", self.model)

    def _blocking_load(self) -> Any:
        try:
            from vllm import LLM
        except ImportError:
            raise RuntimeError("vllm not installed")
        # nomic-embed-text-v1.5 requires trust_remote_code for its custom pooling
        # class.  Revision is pinned so a compromised HF repo push can't execute
        # new code here.  Verify + update with: hf model-info nomic-ai/nomic-embed-text-v1.5
        return LLM(
            model=self.model,
            task="embed",
            gpu_memory_utilization=self._gpu_util,
            dtype="auto",
            trust_remote_code=True,
            revision="e9b6763023c676ca8431644204f50c2b100d9aab",  # verified 2026-05-31
        )

    async def encode(self, texts: list[str]) -> list[Any]:
        """Return a list of embedding vectors (numpy arrays), one per text."""
        await self._ensure_loaded()
        outputs = await asyncio.to_thread(
            self._llm.encode,
            texts,
            use_tqdm=False,
        )
        vecs = [o.outputs.embedding for o in outputs]
        if self._dim is None and vecs:
            self._dim = len(vecs[0])
            log.info("VLLMEmbedder: dim=%d", self._dim)
        return vecs

    def get_status(self) -> dict:
        return {
            "backend": "vllm_embed",
            "model": self.model,
            "available": self._llm is not None,
            "dim": self._dim,
        }

