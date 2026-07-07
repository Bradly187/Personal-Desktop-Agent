"""Property-based tests for SemanticMemory.

Feature: behavioral-twin-state
Properties covered: 1, 2, 16, 17
PBT library: Hypothesis (pip install hypothesis)

Structure
---------
Section A — ChromaDB-backed tests (skipped when chromadb is not installed)
    Property 1: round-trip semantic identity
    Property 2: query result count bounded by min(n, C_success)
    Property 16: query_similar filters to success=True only
    Property 17: SemanticMemory count bounded by CAP; after eviction count = CAP - EVICT_COUNT

Section B — Jaccard fallback tests (no ChromaDB required)
    Fallback ranking: results ranked by Jaccard similarity
    Fallback count: result count bounded by min(n, len(candidates))
"""

from __future__ import annotations

import asyncio
import hashlib
import time
import tempfile
import os
from unittest.mock import AsyncMock, MagicMock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from storage.semantic_memory import SemanticMemory


# ===========================================================================
# Section A — ChromaDB-backed property tests
# ===========================================================================
# Each test in this section calls pytest.importorskip("chromadb") to skip
# gracefully when the package is not installed.

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    """Run a coroutine in a fresh event loop."""
    return asyncio.run(coro)


def _make_chroma_memory(tmp_path: str) -> SemanticMemory:
    """Return a SemanticMemory backed by a temporary ChromaDB directory."""
    return SemanticMemory(chroma_dir=tmp_path)


async def _start_chroma_memory(tmp_path: str) -> SemanticMemory:
    """Create and start a SemanticMemory; return it (or raise if unavailable)."""
    mem = _make_chroma_memory(tmp_path)
    available = await mem.start()
    if not available:
        pytest.skip("ChromaDB unavailable at runtime")
    return mem


async def _with_chroma(tmp_path: str, coro_fn):
    """Run coro_fn(mem) then always call stop() before tempdir cleanup.

    Prevents WinError 32 (file locked) when TemporaryDirectory.__exit__ runs
    while ChromaDB still holds SQLite WAL handles open.
    """
    mem = await _start_chroma_memory(tmp_path)
    try:
        return await coro_fn(mem)
    finally:
        await mem.stop()


# ---------------------------------------------------------------------------
# Property 1: round-trip semantic identity
# ---------------------------------------------------------------------------

@settings(max_examples=20, deadline=None)
@given(
    text=st.text(min_size=3, max_size=200).filter(lambda t: t.strip()),
    action=st.sampled_from(["CLICK", "TYPE", "SCROLL", "OPEN", "CLOSE", "HOTKEY"]),
    source=st.sampled_from(["voice", "gesture", "touch", "tilt"]),
)
def test_property_1_round_trip_semantic_identity(text, action, source):
    # Feature: behavioral-twin-state, Property 1: round-trip semantic identity
    # Validates: Requirements 1.7
    #
    # For any non-empty command text t, storing t in SemanticMemory and then
    # calling query_similar(t, 1) SHALL return t as the top result.

    chromadb = pytest.importorskip("chromadb")  # skip if chromadb not installed

    async def _run_test():
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
            mem = await _start_chroma_memory(tmp_dir)
            try:
                ts = time.time()
                doc_id = hashlib.sha256(f"{text}:{action}:{ts}".encode()).hexdigest()[:16]

                await mem.add(
                    text=text,
                    action=action,
                    source=source,
                    ts=ts,
                    success=True,
                    doc_id=doc_id,
                )

                results = await mem.query_similar(text, n=1)

                assert len(results) == 1, (
                    f"Expected 1 result, got {len(results)} for text={text!r}"
                )
                assert results[0]["text"] == text, (
                    f"Top result text mismatch: expected {text!r}, got {results[0]['text']!r}"
                )
            finally:
                await mem.stop()

    asyncio.run(_run_test())


# ---------------------------------------------------------------------------
# Property 2: query result count bounded by min(n, C_success)
# ---------------------------------------------------------------------------

@settings(max_examples=30, deadline=None)
@given(
    c_success=st.integers(min_value=0, max_value=15),
    c_fail=st.integers(min_value=0, max_value=5),
    n=st.integers(min_value=1, max_value=20),
)
def test_property_2_query_result_count_bounded(c_success, c_fail, n):
    # Feature: behavioral-twin-state, Property 2: query result count bounded by min(n, C_success)
    # Validates: Requirements 1.6
    #
    # After storing C_success successful documents, query_similar(text, n) returns
    # at most min(n, C_success) results.

    chromadb = pytest.importorskip("chromadb")

    async def _run_test():
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
            mem = await _start_chroma_memory(tmp_dir)
            try:
                base_ts = time.time()

                for i in range(c_success):
                    text = f"successful command number {i} do something useful"
                    await mem.add(
                        text=text,
                        action="CLICK",
                        source="voice",
                        ts=base_ts + i,
                        success=True,
                        doc_id=f"success_{i}",
                    )

                for i in range(c_fail):
                    text = f"failed command number {i} do something else"
                    await mem.add(
                        text=text,
                        action="CLICK",
                        source="voice",
                        ts=base_ts + c_success + i,
                        success=False,
                        doc_id=f"fail_{i}",
                    )

                results = await mem.query_similar("do something", n=n)

                expected_max = min(n, c_success)
                assert len(results) <= expected_max, (
                    f"query_similar returned {len(results)} results, "
                    f"expected at most min(n={n}, C_success={c_success}) = {expected_max}"
                )
            finally:
                await mem.stop()

    asyncio.run(_run_test())


# ---------------------------------------------------------------------------
# Property 16: query_similar filters to success=True only
# ---------------------------------------------------------------------------

@settings(max_examples=30, deadline=None)
@given(
    c_success=st.integers(min_value=1, max_value=10),
    c_fail=st.integers(min_value=1, max_value=10),
    n=st.integers(min_value=1, max_value=20),
)
def test_property_16_query_filters_success_only(c_success, c_fail, n):
    # Feature: behavioral-twin-state, Property 16: query_similar filters to success=True only
    # Validates: Requirements 7.4
    #
    # Documents stored with success=False are never returned by query_similar(),
    # regardless of how similar they are to the query.

    chromadb = pytest.importorskip("chromadb")

    async def _run_test():
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
            mem = await _start_chroma_memory(tmp_dir)
            try:
                base_ts = time.time()
                query_text = "click the button now"

                for i in range(c_fail):
                    await mem.add(
                        text=query_text,
                        action="CLICK",
                        source="voice",
                        ts=base_ts + i,
                        success=False,
                        doc_id=f"fail_{i}",
                    )

                for i in range(c_success):
                    await mem.add(
                        text=f"click the button now variant {i}",
                        action="CLICK",
                        source="voice",
                        ts=base_ts + c_fail + i,
                        success=True,
                        doc_id=f"success_{i}",
                    )

                results = await mem.query_similar(query_text, n=n)

                for result in results:
                    assert result["text"] != query_text or c_success == 0, (
                        f"query_similar returned a success=False document: {result['text']!r}. "
                        f"The filter is not working correctly."
                    )
            finally:
                await mem.stop()

    asyncio.run(_run_test())


# ---------------------------------------------------------------------------
# Property 17: SemanticMemory count bounded by CAP; after eviction count = CAP - EVICT_COUNT
# ---------------------------------------------------------------------------

def test_property_17_count_bounded_by_cap_and_eviction():
    # Feature: behavioral-twin-state, Property 17: SemanticMemory count bounded by CAP;
    #   after eviction count = CAP - EVICT_COUNT + new_insertions
    # Validates: Requirements 7.5, 7.7
    #
    # This test uses a reduced CAP/EVICT_COUNT to keep runtime reasonable.
    # We subclass SemanticMemory to override the constants.

    chromadb = pytest.importorskip("chromadb")

    SMALL_CAP = 10
    SMALL_EVICT = 3

    class SmallCapMemory(SemanticMemory):
        CAP = SMALL_CAP
        EVICT_COUNT = SMALL_EVICT

    async def _run_test():
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
            mem = SmallCapMemory(chroma_dir=tmp_dir)
            available = await mem.start()
            if not available:
                pytest.skip("ChromaDB unavailable at runtime")
            try:
                base_ts = time.time()

                for i in range(SMALL_CAP):
                    await mem.add(
                        text=f"command {i}",
                        action="CLICK",
                        source="voice",
                        ts=base_ts + i,
                        success=True,
                        doc_id=f"doc_{i}",
                    )

                count_at_cap = await mem.count()
                assert count_at_cap == SMALL_CAP, (
                    f"Expected count={SMALL_CAP} after inserting CAP documents, got {count_at_cap}"
                )

                await mem.add(
                    text="command cap+1",
                    action="CLICK",
                    source="voice",
                    ts=base_ts + SMALL_CAP,
                    success=True,
                    doc_id="doc_cap_plus_1",
                )

                await asyncio.sleep(0)
                await asyncio.sleep(0)

                count_after_eviction = await mem.count()
                expected_count = SMALL_CAP - SMALL_EVICT + 1
                assert count_after_eviction == expected_count, (
                    f"After eviction: expected count={expected_count} "
                    f"(CAP={SMALL_CAP} - EVICT_COUNT={SMALL_EVICT} + 1 new), "
                    f"got {count_after_eviction}"
                )

                for i in range(SMALL_EVICT):
                    results = await mem.query_similar(f"command {i}", n=1)
                    if results:
                        assert results[0]["text"] != f"command {i}", (
                            f"Document 'command {i}' (one of the oldest {SMALL_EVICT}) "
                            f"was not evicted as expected."
                        )
            finally:
                await mem.stop()

    asyncio.run(_run_test())


# ===========================================================================
# Section B — Jaccard fallback tests (no ChromaDB required)
# ===========================================================================
# These tests exercise the AgentDB Jaccard fallback path by providing a mock
# AgentDB that returns a controlled list of command dicts. ChromaDB is not
# needed — SemanticMemory._available is left False (the default).

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fallback_memory(candidates: list[dict]) -> SemanticMemory:
    """Return a SemanticMemory in fallback mode (ChromaDB unavailable) backed
    by a mock AgentDB that returns the given candidates list."""
    mock_db = MagicMock()
    mock_db.get_recent_successful_commands = AsyncMock(return_value=candidates)

    mem = SemanticMemory(agent_db=mock_db)
    # _available defaults to False — no ChromaDB
    assert not mem.available
    return mem


def _jaccard(a: str, b: str) -> float:
    """Reference Jaccard similarity between two strings (word-level)."""
    sa = set(a.lower().split())
    sb = set(b.lower().split())
    if not sa and not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


# ---------------------------------------------------------------------------
# Fallback ranking: results ranked by Jaccard similarity
# ---------------------------------------------------------------------------

@settings(max_examples=100)
@given(
    query=st.text(min_size=1, max_size=100, alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd", "Zs"))).filter(lambda t: t.strip()),
    candidates=st.lists(
        st.fixed_dictionaries({
            "text": st.text(min_size=1, max_size=100, alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd", "Zs"))).filter(lambda t: t.strip()),
            "action": st.sampled_from(["CLICK", "TYPE", "SCROLL", "OPEN"]),
            "source": st.sampled_from(["voice", "gesture", "touch"]),
            "ts": st.floats(min_value=1_000_000.0, max_value=2_000_000.0, allow_nan=False, allow_infinity=False),
        }),
        min_size=1,
        max_size=20,
    ),
    n=st.integers(min_value=1, max_value=10),
)
def test_fallback_results_ranked_by_jaccard(query, candidates, n):
    # Feature: behavioral-twin-state (Jaccard fallback)
    # Validates: Requirements 1.6
    #
    # When ChromaDB is unavailable, query_similar() returns results ranked
    # by descending Jaccard similarity to the query text.

    async def _run_test():
        mem = _make_fallback_memory(candidates)
        results = await mem.query_similar(query, n=n)

        # Compute expected ranking
        scored = sorted(
            candidates,
            key=lambda row: _jaccard(query, row["text"]),
            reverse=True,
        )
        expected_texts = [row["text"] for row in scored[:min(n, len(candidates))]]

        assert len(results) == len(expected_texts), (
            f"Expected {len(expected_texts)} results, got {len(results)}"
        )

        for i, (result, expected_text) in enumerate(zip(results, expected_texts)):
            assert result["text"] == expected_text, (
                f"Result[{i}] text mismatch: expected {expected_text!r}, got {result['text']!r}. "
                f"query={query!r}, candidates={[c['text'] for c in candidates]}"
            )

    asyncio.run(_run_test())


# ---------------------------------------------------------------------------
# Fallback count: result count bounded by min(n, len(candidates))
# ---------------------------------------------------------------------------

@settings(max_examples=100)
@given(
    query=st.text(min_size=1, max_size=50).filter(lambda t: t.strip()),
    candidates=st.lists(
        st.fixed_dictionaries({
            "text": st.text(min_size=1, max_size=50).filter(lambda t: t.strip()),
            "action": st.just("CLICK"),
            "source": st.just("voice"),
            "ts": st.floats(min_value=1_000_000.0, max_value=2_000_000.0, allow_nan=False, allow_infinity=False),
        }),
        min_size=0,
        max_size=15,
    ),
    n=st.integers(min_value=1, max_value=20),
)
def test_fallback_result_count_bounded(query, candidates, n):
    # Feature: behavioral-twin-state (Jaccard fallback)
    # Validates: Requirements 1.6
    #
    # When ChromaDB is unavailable, query_similar(text, n) returns at most
    # min(n, len(candidates)) results.

    async def _run_test():
        mem = _make_fallback_memory(candidates)
        results = await mem.query_similar(query, n=n)

        expected_max = min(n, len(candidates))
        assert len(results) <= expected_max, (
            f"Fallback returned {len(results)} results, "
            f"expected at most min(n={n}, len(candidates)={len(candidates)}) = {expected_max}"
        )

    asyncio.run(_run_test())


# ---------------------------------------------------------------------------
# Fallback: empty candidates returns empty list
# ---------------------------------------------------------------------------

def test_fallback_empty_candidates_returns_empty():
    # Feature: behavioral-twin-state (Jaccard fallback)
    # Validates: Requirements 1.6

    async def _run_test():
        mem = _make_fallback_memory([])
        results = await mem.query_similar("click the button", n=5)
        assert results == [], f"Expected [], got {results}"

    asyncio.run(_run_test())


# ---------------------------------------------------------------------------
# Fallback: no agent_db returns empty list
# ---------------------------------------------------------------------------

def test_fallback_no_agent_db_returns_empty():
    # Feature: behavioral-twin-state (Jaccard fallback)
    # Validates: Requirements 1.4, 1.5

    async def _run_test():
        mem = SemanticMemory(agent_db=None)
        assert not mem.available
        results = await mem.query_similar("click the button", n=5)
        assert results == [], f"Expected [], got {results}"

    asyncio.run(_run_test())


# ===========================================================================
# Task 14 — Unit tests (14.1 – 14.4)
# ===========================================================================

# ---------------------------------------------------------------------------
# 14.1  ChromaDB unavailable at startup: start() returns False, add() is no-op,
#        query_similar falls back to AgentDB
# ---------------------------------------------------------------------------

def test_14_1_chroma_unavailable_at_startup():
    """When chromadb import fails, start() returns False, _available=False,
    add() is a no-op, and query_similar uses the AgentDB fallback."""

    candidates = [
        {"text": "click the button", "action": "CLICK", "source": "voice", "ts": 1.0}
    ]
    mock_db = MagicMock()
    mock_db.get_recent_successful_commands = AsyncMock(return_value=candidates)

    async def _run_test():
        mem = SemanticMemory(chroma_dir="/nonexistent_chroma_dir_123", agent_db=mock_db)

        # Patch chromadb import inside start() to raise ImportError
        import sys
        orig_modules = sys.modules.copy()
        sys.modules["chromadb"] = None  # makes `import chromadb` raise ImportError

        try:
            result = await mem.start()
        finally:
            # Restore original modules
            for k in list(sys.modules.keys()):
                if k not in orig_modules:
                    del sys.modules[k]
            sys.modules.update(orig_modules)

        assert result is False, f"start() should return False when chromadb unavailable, got {result}"
        assert not mem.available, "_available must be False when ChromaDB init fails"

        # add() must be a no-op (no exception, no collection write)
        await mem.add(text="test", action="CLICK", source="voice", ts=1.0, success=True)
        # No error raised → no-op confirmed

        # query_similar must fall back to AgentDB
        results = await mem.query_similar("click the button", n=5)
        assert isinstance(results, list), f"query_similar must return list, got {type(results)}"
        # AgentDB has one candidate — should be returned
        assert len(results) > 0, "Expected at least one result from AgentDB fallback"
        assert results[0]["text"] == "click the button"

    asyncio.run(_run_test())


# ---------------------------------------------------------------------------
# 14.2  ChromaDB fails mid-session: _available becomes False, fallback used
# ---------------------------------------------------------------------------

def test_14_2_chroma_fails_mid_session_fallback_activated():
    """When ChromaDB raises during query, _available is set to False and
    query_similar falls through to the AgentDB fallback without raising."""

    candidates = [
        {"text": "open the app", "action": "OPEN", "source": "voice", "ts": 2.0}
    ]
    mock_db = MagicMock()
    mock_db.get_recent_successful_commands = AsyncMock(return_value=candidates)

    async def _run_test():
        mem = SemanticMemory(agent_db=mock_db)

        # Simulate that ChromaDB is "available" but fails on query
        mem._available = True
        mem._collection = MagicMock()

        # Make _collection.query raise RuntimeError
        def _raise(*args, **kwargs):
            raise RuntimeError("ChromaDB mid-session failure")

        mem._collection.query = _raise

        # query_similar should NOT raise — it should fall back to AgentDB
        results = await mem.query_similar("open the app", n=5)

        # _available must now be False (mid-session failure detected)
        assert not mem.available, (
            "_available must be False after mid-session ChromaDB failure"
        )

        # Results from AgentDB fallback should be returned
        assert isinstance(results, list)
        assert len(results) > 0, "Expected AgentDB fallback results after mid-session failure"
        assert results[0]["text"] == "open the app"

    asyncio.run(_run_test())


# ---------------------------------------------------------------------------
# 14.3  ./chroma_db/ directory created automatically if absent
# ---------------------------------------------------------------------------

def test_14_3_chroma_dir_created_automatically():
    """SemanticMemory.start() creates the chroma_dir if it does not exist."""
    chromadb = pytest.importorskip("chromadb")

    async def _run_test():
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as base_dir:
            chroma_dir = os.path.join(base_dir, "nested", "chroma_db")
            assert not os.path.exists(chroma_dir), "Directory should not exist yet"

            mem = SemanticMemory(chroma_dir=chroma_dir)
            try:
                await mem.start()
                assert os.path.isdir(chroma_dir), (
                    f"SemanticMemory.start() must create chroma_dir='{chroma_dir}' "
                    f"if it does not exist."
                )
            finally:
                await mem.stop()

    asyncio.run(_run_test())


# ---------------------------------------------------------------------------
# 14.4  Eviction: after CAP insertions, oldest EVICT_COUNT documents removed
#        (complementary deterministic test alongside Property 17 PBT)
# ---------------------------------------------------------------------------

def test_14_4_eviction_removes_oldest_documents_deterministically():
    """After exactly CAP + 1 insertions, the EVICT_COUNT oldest (by ts) are removed
    and the remaining count = CAP - EVICT_COUNT + 1.

    Uses a SmallCapMemory subclass (CAP=8, EVICT_COUNT=2) for fast runtime.
    Verifies doc_0 and doc_1 (lowest ts) are gone after eviction.
    """
    chromadb = pytest.importorskip("chromadb")

    SMALL_CAP = 8
    SMALL_EVICT = 2

    class TinyCapMemory(SemanticMemory):
        CAP = SMALL_CAP
        EVICT_COUNT = SMALL_EVICT

    async def _run_test():
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
            mem = TinyCapMemory(chroma_dir=tmp_dir)
            available = await mem.start()
            if not available:
                pytest.skip("ChromaDB unavailable at runtime")
            try:
                base_ts = 1_000_000.0

                for i in range(SMALL_CAP):
                    await mem.add(
                        text=f"cmd {i}",
                        action="CLICK",
                        source="voice",
                        ts=base_ts + i,
                        success=True,
                        doc_id=f"doc_{i}",
                    )

                assert await mem.count() == SMALL_CAP

                await mem.add(
                    text="cmd extra",
                    action="CLICK",
                    source="voice",
                    ts=base_ts + SMALL_CAP,
                    success=True,
                    doc_id="doc_extra",
                )

                await asyncio.sleep(0)
                await asyncio.sleep(0)

                final_count = await mem.count()
                expected_count = SMALL_CAP - SMALL_EVICT + 1
                assert final_count == expected_count, (
                    f"Expected {expected_count} docs after eviction, got {final_count}"
                )

                for i in range(SMALL_EVICT):
                    results = await mem.query_similar(f"cmd {i}", n=1)
                    if results:
                        assert results[0]["text"] != f"cmd {i}", (
                            f"doc_{i} (oldest) should have been evicted but was returned"
                        )
            finally:
                await mem.stop()

    asyncio.run(_run_test())


# ---------------------------------------------------------------------------
# Fallback: distance field equals 1 - jaccard_similarity
# ---------------------------------------------------------------------------

def test_fallback_distance_equals_one_minus_jaccard():
    # Feature: behavioral-twin-state (Jaccard fallback)
    # Validates: Requirements 1.6

    query = "click the button"
    candidates = [
        {"text": "click the button", "action": "CLICK", "source": "voice", "ts": 1.0},
        {"text": "open the window", "action": "OPEN", "source": "voice", "ts": 2.0},
    ]

    async def _run_test():
        mem = _make_fallback_memory(candidates)
        results = await mem.query_similar(query, n=5)

        for result in results:
            expected_sim = _jaccard(query, result["text"])
            expected_dist = 1.0 - expected_sim
            assert abs(result["distance"] - expected_dist) < 1e-9, (
                f"distance mismatch for {result['text']!r}: "
                f"expected {expected_dist}, got {result['distance']}"
            )

    asyncio.run(_run_test())
