"""Regression tests for EXCLUDE_DIRS matching in CodebaseIndexer.

Guards the 2026-07-03 fix: exclusion checked `any(part in EXCLUDE_DIRS for
part in path.parts)` on the *absolute* path, so directory names in ancestors
OUTSIDE the project root also matched. A project rooted under an
excluded-named ancestor — e.g. a git worktree at `.claude/worktrees/<name>` —
indexed zero source files. Exclusion must be evaluated on components
*relative to the project root* (CodebaseIndexer._is_excluded), at all three
sites: index() source loop, index() PDF loop, and _on_file_changed().

Run with:
    pytest tests/test_codebase_indexer_exclude.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from inference.codebase_indexer import CodebaseIndexer


@pytest.fixture
def nested_root(tmp_path):
    """A project root nested under a directory named like an excluded dir —
    the git-worktree layout that triggered the bug."""
    root = tmp_path / ".claude" / "worktrees" / "proj"
    root.mkdir(parents=True)
    return root


# ---------------------------------------------------------------------------
# _is_excluded — pure path logic
# ---------------------------------------------------------------------------

class TestIsExcluded:
    def test_project_file_under_excluded_ancestor_not_excluded(self, nested_root):
        # The core regression: ".claude" is an ancestor of the ROOT, not a
        # directory inside the project — must not exclude project files.
        idx = CodebaseIndexer(project_root=str(nested_root))
        assert idx._is_excluded(nested_root / "core" / "main.py") is False

    def test_excluded_dir_inside_project_still_excluded(self, nested_root):
        idx = CodebaseIndexer(project_root=str(nested_root))
        assert idx._is_excluded(nested_root / ".venv" / "lib" / "x.py") is True
        assert idx._is_excluded(nested_root / "__pycache__" / "m.py") is True
        assert idx._is_excluded(nested_root / "sub" / "node_modules" / "j.py") is True

    def test_path_outside_root_is_excluded(self, nested_root, tmp_path):
        idx = CodebaseIndexer(project_root=str(nested_root))
        assert idx._is_excluded(tmp_path / "elsewhere" / "a.py") is True

    def test_plain_root_behavior_unchanged(self, tmp_path):
        # For a root NOT under an excluded-named ancestor, semantics are
        # identical to the old absolute-parts check.
        root = tmp_path / "proj"
        root.mkdir()
        idx = CodebaseIndexer(project_root=str(root))
        assert idx._is_excluded(root / "core" / "main.py") is False
        assert idx._is_excluded(root / ".git" / "hooks" / "h.py") is True


# ---------------------------------------------------------------------------
# index() — discovery loops use the relative check (no ChromaDB needed:
# _available is forced on and the per-file indexers are stubbed out)
# ---------------------------------------------------------------------------

class TestIndexDiscovery:
    async def test_index_finds_sources_under_excluded_ancestor(self, nested_root):
        (nested_root / "core").mkdir()
        (nested_root / "core" / "main.py").write_text("x = 1\n")
        (nested_root / ".venv" / "lib").mkdir(parents=True)
        (nested_root / ".venv" / "lib" / "site.py").write_text("y = 2\n")

        idx = CodebaseIndexer(project_root=str(nested_root))
        idx._available = True
        indexed: list[Path] = []

        async def _fake_index(path: Path) -> int:
            indexed.append(path)
            return 1

        idx._index_source_file_async = _fake_index
        stats = await idx.index(force=True)

        # Before the fix this indexed ZERO files (".claude" ancestor matched).
        assert stats["indexed_files"] == 1
        assert [p.name for p in indexed] == ["main.py"]

    async def test_pdf_loop_skips_excluded_dirs_inside_docs(self, nested_root):
        docs = nested_root / "docs"
        (docs / "node_modules").mkdir(parents=True)
        (docs / "guide.pdf").write_bytes(b"%PDF-1.4")
        (docs / "node_modules" / "vendored.pdf").write_bytes(b"%PDF-1.4")

        idx = CodebaseIndexer(project_root=str(nested_root))
        idx._available = True
        pdfs: list[Path] = []
        idx._index_pdf_file = pdfs.append
        stats = await idx.index(force=True)

        assert stats["indexed_files"] == 1
        assert [p.name for p in pdfs] == ["guide.pdf"]


# ---------------------------------------------------------------------------
# _on_file_changed — watcher path uses the same relative check
# ---------------------------------------------------------------------------

class TestOnFileChanged:
    async def test_reindexes_project_file_under_excluded_ancestor(self, nested_root):
        src = nested_root / "core"
        src.mkdir()
        f = src / "main.py"
        f.write_text("x = 1\n")

        idx = CodebaseIndexer(project_root=str(nested_root))
        idx._available = True
        indexed: list[Path] = []

        async def _fake_index(path: Path) -> int:
            indexed.append(path)
            return 1

        idx._index_source_file_async = _fake_index
        await idx._on_file_changed(f)
        assert indexed == [f]

    async def test_ignores_file_in_excluded_dir_inside_project(self, nested_root):
        venv_file = nested_root / ".venv" / "bad.py"
        venv_file.parent.mkdir(parents=True)
        venv_file.write_text("z = 3\n")

        idx = CodebaseIndexer(project_root=str(nested_root))
        idx._available = True

        async def _fake_index(path: Path) -> int:  # pragma: no cover — must not run
            raise AssertionError("excluded file must not be re-indexed")

        idx._index_source_file_async = _fake_index
        await idx._on_file_changed(venv_file)
