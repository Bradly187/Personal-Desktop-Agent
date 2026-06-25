"""Tests for mcp_server/tools/search.py — shared grep + glob primitives.

Spec: specs/first-class-search-tools/requirements.md. Pure-function, no model.
Covers the MCP-tool contract (dict shape, scope refusal) and parity with the
DevAgent GREP verb that delegates to the same implementation.
"""

from __future__ import annotations

import pytest

from mcp_server.tools import search
from inference.dev_agent import DevAgent


@pytest.fixture
def tree(tmp_path):
    """A small file tree with a vendored dir that must be skipped."""
    (tmp_path / "a.py").write_text("import os\nx = 1\nimport sys\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("hello\nimport nothing\n", encoding="utf-8")
    (tmp_path / "notes.md").write_text("# title\nimport markdown\n", encoding="utf-8")
    skip = tmp_path / "node_modules"
    skip.mkdir()
    (skip / "vendor.py").write_text("import secret\n", encoding="utf-8")
    sub = tmp_path / "pkg"
    sub.mkdir()
    (sub / "c.py").write_text("import json\n", encoding="utf-8")
    return tmp_path


# --- grep (search_text) -------------------------------------------------------

def test_grep_hit_returns_matches(tree):
    r = search.search_text(r"^import", str(tree))
    assert r["ok"] is True
    # a.py (2: import os/import sys) + b.txt (import nothing) + notes.md
    # (import markdown) + pkg/c.py (import json) — all start with "import".
    assert r["count"] == 5
    assert any("a.py:1: import os" in m for m in r["matches"])


def test_grep_skips_vendored_dirs(tree):
    r = search.search_text(r"import secret", str(tree))
    assert r["ok"] is True and r["count"] == 0  # node_modules pruned


def test_grep_miss_is_ok_with_zero(tree):
    r = search.search_text("zzzznope", str(tree))
    assert r["ok"] is True and r["count"] == 0 and r["matches"] == []


def test_grep_bad_regex_fails_closed(tree):
    r = search.search_text("(unclosed", str(tree))
    assert r["ok"] is False and "invalid regex" in r["error"]


def test_grep_missing_path(tree):
    r = search.search_text("x", str(tree / "nope"))
    assert r["ok"] is False and "does not exist" in r["error"]


def test_grep_max_lines_truncates(tree):
    r = search.search_text("import", str(tree), max_lines=2)
    assert r["ok"] is True and r["count"] == 2 and r["truncated"] is True


# --- glob (glob_paths) --------------------------------------------------------

def test_glob_single_level(tree):
    r = search.glob_paths("*.py", str(tree))
    assert r["ok"] is True
    names = [p.rsplit("\\", 1)[-1].rsplit("/", 1)[-1] for p in r["paths"]]
    assert "a.py" in names and "c.py" not in names  # non-recursive


def test_glob_recursive_and_skips_vendored(tree):
    r = search.glob_paths("**/*.py", str(tree))
    assert r["ok"] is True
    joined = " ".join(r["paths"])
    assert "a.py" in joined and "c.py" in joined       # recursion reaches pkg/
    assert "vendor.py" not in joined                   # node_modules pruned


def test_glob_sorted_deterministic(tree):
    r = search.glob_paths("**/*.py", str(tree))
    assert r["paths"] == sorted(r["paths"])


def test_glob_missing_path(tree):
    r = search.glob_paths("*.py", str(tree / "nope"))
    assert r["ok"] is False and "does not exist" in r["error"]


# --- scope enforcement (deny-by-default for the MCP surface) ------------------

def test_grep_scope_refusal_outside_allowlist(tree):
    # Allowlist is a sibling dir; searching `tree` must be refused.
    other = tree.parent / "elsewhere"
    other.mkdir()
    r = search.search_text("import", str(tree), scopes=[str(other)])
    assert r["ok"] is False and "outside the allowed search roots" in r["error"]


def test_glob_scope_refusal_outside_allowlist(tree):
    other = tree.parent / "elsewhere2"
    other.mkdir()
    r = search.glob_paths("*.py", str(tree), scopes=[str(other)])
    assert r["ok"] is False and "outside the allowed search roots" in r["error"]


def test_grep_scope_allows_inside_allowlist(tree):
    r = search.search_text(r"^import", str(tree), scopes=[str(tree)])
    assert r["ok"] is True and r["count"] == 5


def test_scope_none_is_unrestricted(tree):
    # scopes=None (the in-process DevAgent path) never refuses on scope.
    r = search.search_text("import", str(tree), scopes=None)
    assert r["ok"] is True


# --- parity: DevAgent._grep delegates to the shared module --------------------

def test_devagent_grep_parity_with_shared_module(tree):
    legacy = DevAgent._grep(r"^import", str(tree))
    result = search.search_text(r"^import", str(tree), scopes=None)
    expected = search.format_grep_result(result, r"^import", str(tree), 100)
    assert legacy == expected
    assert legacy.startswith("Found 5 match(es)")


def test_devagent_grep_missing_path_string(tree):
    out = DevAgent._grep("x", str(tree / "nope"))
    assert out == f"Path does not exist: {tree / 'nope'}"
