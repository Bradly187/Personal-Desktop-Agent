"""Regression test for the 2026-07-21 duplicate-chunk-id bug in CodebaseIndexer.

`_make_id(file, name, mtime)` hashed only file/name/mtime, so two chunks in the
same file that share a `name` at different locations — most commonly same-named
methods across multiple classes in one test file (e.g. two TestCase classes
each defining `setUp`) — collided into one chroma id. ChromaDB's collection.add()
rejects the whole batch on a duplicate id ("Expected IDs to be unique, found
duplicates... in add."), so the entire file silently dropped out of the index.
77 files (mostly under tests/) were affected on the 2026-07-21 full reindex.

Fix: `_make_id` also hashes `start_line`, which AST guarantees is distinct for
every top-level definition in a file.

Run with:
    pytest tests/test_codebase_indexer_duplicate_ids.py -v
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from inference.codebase_indexer import CodebaseIndexer, _chunk_python, _make_id


class TestMakeIdDisambiguation:
    def test_same_file_name_mtime_different_line_yields_distinct_ids(self):
        a = _make_id("tests/test_x.py", "setUp", 1720000000.0, start_line=5)
        b = _make_id("tests/test_x.py", "setUp", 1720000000.0, start_line=42)
        assert a != b

    def test_start_line_defaults_to_zero_backward_compatible(self):
        # Callers that don't pass start_line still get a stable id.
        assert _make_id("f.py", "foo", 1.0) == _make_id("f.py", "foo", 1.0, 0)


class TestSameNameAcrossClasses:
    """The real-world shape that triggered the bug: multiple classes in one
    file each defining a method with the same name (setUp is the canonical
    pytest/unittest example, and is exactly what tripped 58 of the 77
    affected files under tests/)."""

    SOURCE = textwrap.dedent('''\
        class TestFoo:
            def setUp(self):
                pass

            def test_one(self):
                pass


        class TestBar:
            def setUp(self):
                pass

            def test_two(self):
                pass
    ''')

    def test_chunker_emits_one_chunk_per_method_with_distinct_start_lines(self, tmp_path):
        f = tmp_path / "test_dup.py"
        f.write_text(self.SOURCE)
        chunks = _chunk_python(f, tmp_path)

        setup_chunks = [c for c in chunks if c.name == "setUp"]
        assert len(setup_chunks) == 2
        assert setup_chunks[0].start_line != setup_chunks[1].start_line

    def test_generated_ids_are_unique_across_same_named_methods(self, tmp_path):
        f = tmp_path / "test_dup.py"
        f.write_text(self.SOURCE)
        chunks = _chunk_python(f, tmp_path)
        mtime = f.stat().st_mtime

        ids = [_make_id(c.file, c.name, mtime, c.start_line) for c in chunks]
        assert len(ids) == len(set(ids)), "duplicate chunk ids would make ChromaDB add() fail"


@pytest.fixture
def _chroma():
    return pytest.importorskip("chromadb")


class TestIndexDoesNotDropDuplicateNamedFile:
    async def test_index_succeeds_with_zero_errors_for_same_named_methods(self, tmp_path, _chroma):
        f = tmp_path / "test_dup.py"
        f.write_text(TestSameNameAcrossClasses.SOURCE)

        idx = CodebaseIndexer(project_root=str(tmp_path), chroma_dir=str(tmp_path / "chroma"))
        try:
            assert await idx.start() is True
            stats = await idx.index(force=True)
            # Before the fix this file contributed to stats["errors"] via
            # "Expected IDs to be unique" and its chunks never reached chroma.
            assert stats["errors"] == 0
            assert stats["indexed_files"] == 1

            hits = await idx.query_codebase("setUp", n=10)
            names = [h.get("name") for h in hits]
            assert names.count("setUp") == 2
        finally:
            await idx.stop()
