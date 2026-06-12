"""PersonalKB — chunkers, intent markers, and the index/query lifecycle
(real ChromaDB in tmp dirs, fake embedder so MiniLM never loads).

Run:
    python -m pytest tests/test_personal_kb.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from storage.personal_kb import (
    PersonalKB, chunk_text_document, is_personal_query, _MAX_CHUNK_CHARS,
)


class FakeEmbedder:
    """Deterministic tiny embedding function — no model download."""

    def __call__(self, input):
        out = []
        for t in input:
            v = [0.0] * 8
            for i, ch in enumerate(t[:64]):
                v[i % 8] += ord(ch) / 1000.0
            out.append(v)
        return out

    def embed_query(self, input):
        # chroma >= 1.x routes query texts through embed_query
        return self(input)

    @staticmethod
    def name():
        return "fake-embedder"


# ---------------------------------------------------------------------------
# Chunkers / intent markers (pure functions)
# ---------------------------------------------------------------------------

def test_chunk_markdown_carries_headings(tmp_path):
    p = tmp_path / "notes.md"
    chunks = chunk_text_document(
        p, "# Doctor visit\n\nDiscussed new medication.\n\n# Recipes\n\nPasta steps.", 1.0)
    by_heading = {c.name: c.text for c in chunks}
    assert "Discussed new medication." in by_heading["Doctor visit"]
    assert "Pasta steps." in by_heading["Recipes"]


def test_chunk_coalesces_paragraphs(tmp_path):
    text = "\n\n".join(f"para {i} " + "x" * 400 for i in range(10))   # ~4 KB total
    chunks = chunk_text_document(tmp_path / "a.txt", text, 1.0)
    assert 1 < len(chunks) < 10                      # coalesced, not 1:1
    assert all(len(c.text) <= _MAX_CHUNK_CHARS + 10 for c in chunks)


def test_chunk_oversized_paragraph_split(tmp_path):
    chunks = chunk_text_document(tmp_path / "a.txt", "y" * (_MAX_CHUNK_CHARS * 3), 1.0)
    assert len(chunks) >= 3
    assert all(len(c.text) <= _MAX_CHUNK_CHARS for c in chunks)


def test_is_personal_query_markers():
    assert is_personal_query("what did I write in my notes about my doctor")
    assert is_personal_query("search my documents for the pasta recipe")
    assert is_personal_query("find my note about the dosage")
    assert not is_personal_query("write a python function to train a model")
    assert not is_personal_query("scroll down")


def test_is_personal_query_never_hijacks_dev_utterances():
    # Review BLOCKER regression guard: bare verb-anchored markers ("find my",
    # "search my", "my files") used to steal these from code/plan routing.
    for utterance in (
        "find my bug in the parser",
        "search my codebase for the fusion tick loop",
        "find my function that handles tilt taps",
        "grep my files for TODO comments",
        "find my iPad on the network",
    ):
        assert not is_personal_query(utterance), utterance


# ---------------------------------------------------------------------------
# Index / query lifecycle (real chroma, fake embedder)
# ---------------------------------------------------------------------------

@pytest.fixture
async def kb(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "health.md").write_text(
        "# Doctor\n\nDiscussed methotrexate dosage on Tuesday.", encoding="utf-8")
    (docs / "recipes.txt").write_text(
        "Gluten-free pasta: rice flour, eggs, salt.", encoding="utf-8")
    k = PersonalKB(
        roots=[str(docs)],
        chroma_dir=tmp_path / "chroma",
        embedder=FakeEmbedder(),
        config_path=tmp_path / "config.json",
        state_path=tmp_path / "state.json",
    )
    assert await k.start()
    yield k, docs
    await k.stop()


async def test_index_and_query(kb):
    k, _docs = kb
    stats = await k.index()
    assert stats["indexed"] == 2 and stats["total_chunks"] >= 2
    hits = await k.query("doctor")
    assert hits and all({"file", "name", "text", "score"} <= set(h) for h in hits)


async def test_reindex_skips_unchanged(kb):
    k, _docs = kb
    await k.index()
    stats = await k.index()
    assert stats["indexed"] == 0 and stats["skipped"] == 2


async def test_prune_deleted_file(kb):
    k, docs = kb
    await k.index()
    (docs / "recipes.txt").unlink()
    stats = await k.index()
    assert stats["pruned"] == 1
    assert all("recipes" not in h["file"] for h in await k.query("pasta", n=10))


async def test_paused_index_is_noop(kb):
    k, _docs = kb
    k.pause()
    assert (await k.index()).get("skipped") == "paused"
    k.resume()
    assert (await k.index())["indexed"] == 2


async def test_stop_event_cancels_index_cooperatively(kb):
    k, _docs = kb
    k._stop_event.set()           # simulates stop() racing a background index
    stats = await k.index()
    assert stats.get("stopped") is True
    assert stats["indexed"] == 0  # exited at the first file boundary


async def test_unavailable_query_returns_empty(tmp_path):
    k = PersonalKB(roots=[str(tmp_path)], chroma_dir=tmp_path / "c",
                   config_path=tmp_path / "cfg.json", state_path=tmp_path / "s.json")
    # never started → unavailable
    assert await k.query("anything") == []
    assert (await k.index()).get("error") == "unavailable"


async def test_config_roots_respected(tmp_path):
    docs = tmp_path / "alt"
    docs.mkdir()
    (docs / "n.md").write_text("zettel", encoding="utf-8")
    cfg = tmp_path / "config.json"
    cfg.write_text('{"roots": ["%s"]}' % str(docs).replace("\\", "\\\\"),
                   encoding="utf-8")
    k = PersonalKB(chroma_dir=tmp_path / "chroma", embedder=FakeEmbedder(),
                   config_path=cfg, state_path=tmp_path / "state.json")
    assert [str(r) for r in k._roots] == [str(docs)]
    assert await k.start()
    assert (await k.index())["indexed"] == 1
    await k.stop()
