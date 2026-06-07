"""Regression tests for CodebaseIndexer's swappable embedder.

Guards the 2026-06-07 fix for the latent bug that silently disabled code-RAG on
the production startup path: main.py constructed

    CodebaseIndexer(project_root=..., embedder=_vllm_embedder)

but __init__ accepted no `embedder` kwarg, so construction raised
TypeError and main.py's broad try/except set indexer=None — dev_agent.set_indexer
was never reached and RAG context was silently absent for every dev-domain query.

These tests pin:
  1. CodebaseIndexer(embedder=...) constructs without TypeError (the core bug).
  2. start() selects a caller-supplied *callable* embedder as the embedding fn.
  3. A non-callable embedder (e.g. a raw VLLMEmbedder) falls back to the
     MiniLM default rather than silently breaking add()/query() later.
  4. Collections are created with cosine space so query_codebase()'s
     `score = 1.0 - distance` is genuine cosine similarity.

Run with:
    pytest tests/test_codebase_indexer_embedder.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "mcp_server"))

from inference.codebase_indexer import CodebaseIndexer


# ---------------------------------------------------------------------------
# A minimal ChromaDB-compatible embedding function (callable + name()).
# Lets start() create collections without loading a real model — start() only
# creates + counts, so the EF is never actually invoked to embed.
# ---------------------------------------------------------------------------

class _FakeEmbeddingFunction:
    def __call__(self, input):  # noqa: A002 — ChromaDB EF signature
        return [[0.1, 0.2, 0.3] for _ in input]

    @staticmethod
    def name() -> str:
        return "fake_ef"


class _NotAnEmbeddingFunction:
    """Mimics VLLMEmbedder: has encode() but is NOT callable as a Chroma EF."""

    async def encode(self, texts):
        return [[0.0] for _ in texts]


# ---------------------------------------------------------------------------
# Construction — the core regression (no TypeError on the embedder kwarg)
# ---------------------------------------------------------------------------

class TestConstruction:
    def test_accepts_embedder_kwarg(self, tmp_path):
        # This is the exact call shape main.py uses. Before the fix it raised
        # TypeError: __init__() got an unexpected keyword argument 'embedder'.
        ef = _FakeEmbeddingFunction()
        idx = CodebaseIndexer(project_root=str(tmp_path), embedder=ef)
        assert idx._embedder is ef

    def test_embedder_defaults_to_none(self, tmp_path):
        idx = CodebaseIndexer(project_root=str(tmp_path))
        assert idx._embedder is None
        # Nothing selected until start() runs.
        assert idx._embedding_fn is None

    def test_embedder_none_explicit(self, tmp_path):
        # main.py passes embedder=_vllm_embedder which is None by default.
        idx = CodebaseIndexer(project_root=str(tmp_path), embedder=None)
        assert idx._embedder is None


# ---------------------------------------------------------------------------
# start() — embedder threading + cosine space (requires chromadb)
# ---------------------------------------------------------------------------

@pytest.fixture
def _chroma():
    return pytest.importorskip("chromadb")


def _assert_cosine(col):
    """Assert a collection was created with cosine space, when introspectable."""
    cfg = getattr(col, "configuration_json", None)
    if isinstance(cfg, dict) and isinstance(cfg.get("hnsw"), dict):
        assert cfg["hnsw"]["space"] == "cosine"


class TestStartEmbedderSelection:
    async def test_start_uses_supplied_callable_embedder(self, tmp_path, _chroma):
        ef = _FakeEmbeddingFunction()
        idx = CodebaseIndexer(
            project_root=str(tmp_path),
            chroma_dir=str(tmp_path / "chroma"),
            embedder=ef,
        )
        try:
            assert await idx.start() is True
            # The supplied embedder was selected — not the MiniLM default.
            assert idx._embedding_fn is ef
            # Collections pinned to cosine space (score = 1.0 - distance).
            _assert_cosine(idx._codebase_col)
            _assert_cosine(idx._documents_col)
        finally:
            await idx.stop()

    async def test_start_falls_back_when_embedder_not_callable(self, tmp_path, _chroma):
        bad = _NotAnEmbeddingFunction()  # has encode(), not __call__ — like VLLMEmbedder
        idx = CodebaseIndexer(
            project_root=str(tmp_path),
            chroma_dir=str(tmp_path / "chroma"),
            embedder=bad,
        )
        try:
            assert await idx.start() is True
            # Did NOT adopt the non-callable embedder; fell back (MiniLM EF or
            # ChromaDB default None when the model can't load).
            assert idx._embedding_fn is not bad
        finally:
            await idx.stop()

    async def test_start_default_embedder_is_not_the_fake(self, tmp_path, _chroma):
        idx = CodebaseIndexer(
            project_root=str(tmp_path),
            chroma_dir=str(tmp_path / "chroma"),
        )
        try:
            assert await idx.start() is True
            # Default path uses MiniLM (or ChromaDB default), never a stray object.
            assert not isinstance(idx._embedding_fn, _FakeEmbeddingFunction)
        finally:
            await idx.stop()
