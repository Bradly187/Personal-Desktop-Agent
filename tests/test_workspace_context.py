"""Tests for live repo-context ingestion (specs/repo-context-ingestion, Gap A).

One assertion group per numbered acceptance criterion (cited in the test name).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from inference.workspace_context import build_workspace_context
from inference.dev_agent import DevAgent


def _init_git_repo(root: Path) -> None:
    """Make `root` a real git repo with one commit (best-effort; skip if no git)."""
    def run(*args):
        subprocess.run(args, cwd=str(root), capture_output=True, text=True, check=True)
    run("git", "init", "-q")
    run("git", "config", "user.email", "t@t.t")
    run("git", "config", "user.name", "T")
    (root / "seed.txt").write_text("x", encoding="utf-8")
    run("git", "add", "-A")
    run("git", "commit", "-q", "-m", "initial commit")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "AGENTS.md").write_text(
        "# Rules\n- schema changes need a user_version bump\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("# Project\nDesktop agent.\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("readme body", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[tool]\n", encoding="utf-8")
    (tmp_path / "inference").mkdir()
    return tmp_path


# -- R1: collect stable repo facts ------------------------------------------- #

def test_r1_1_collects_git_and_docs(repo: Path):
    try:
        _init_git_repo(repo)
        has_git_expected = True
    except Exception:
        has_git_expected = False  # git missing in CI — still must collect docs
    block, stats = build_workspace_context(repo)
    assert "AGENTS.md" in block
    assert "user_version bump" in block        # AGENTS.md house rule ingested
    assert "CLAUDE.md" in block
    assert "Repo layout" in block and "inference/" in block
    assert "pyproject.toml" in block
    assert stats["files_read"] >= 3
    if has_git_expected:
        assert stats["has_git"] is True
        assert "branch:" in block and "recent commits:" in block


def test_r1_2_clips_per_file_and_total(repo: Path):
    (repo / "AGENTS.md").write_text("A" * 5000, encoding="utf-8")
    block, _ = build_workspace_context(repo, per_file_chars=100, max_chars=400)
    assert "truncated at 100 chars" in block      # per-file clip
    assert len(block) <= 400 + 80                  # total cap (+ marker slack)


def test_r1_3_skips_paths_outside_root(repo: Path, tmp_path: Path):
    secret = tmp_path.parent / "outside_secret.md"
    secret.write_text("SECRET", encoding="utf-8")
    try:
        os.symlink(secret, repo / "AGENTS.md")  # symlink escaping the root
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform/permset")
    block, _ = build_workspace_context(repo)
    assert "SECRET" not in block   # escaping symlink refused, not followed


# -- R4: safe degradation ---------------------------------------------------- #

def test_r4_1_non_git_dir_degrades(repo: Path):
    # repo fixture is NOT a git repo (no _init_git_repo call)
    block, stats = build_workspace_context(repo)
    assert stats["has_git"] is False
    assert "branch:" not in block
    assert "AGENTS.md" in block            # file facts still collected


def test_r4_2_missing_files_skipped(tmp_path: Path):
    # Empty dir: no docs, no git — empty block, no raise.
    block, stats = build_workspace_context(tmp_path)
    assert stats["files_read"] == 0
    # only layout (empty) — block may be "" when nothing collected
    assert isinstance(block, str)


# -- R2 / R4.4: DevAgent memoization + disabled byte-identical ---------------- #

def test_r2_1_built_once_memoized(repo: Path, monkeypatch):
    agent = DevAgent(router=MagicMock())
    agent._repo_context_enabled = True
    agent._repo_root = str(repo)
    calls = {"n": 0}
    import inference.dev_agent as da

    real = build_workspace_context

    def counting(root, **kw):
        calls["n"] += 1
        return real(root, **kw)

    monkeypatch.setattr("inference.workspace_context.build_workspace_context", counting)
    first = agent._workspace_context()
    second = agent._workspace_context()
    assert first == second
    assert calls["n"] == 1                 # built once, then memoized


def test_r2_2_invalidate_rebuilds(repo: Path):
    agent = DevAgent(router=MagicMock())
    agent._repo_context_enabled = True
    agent._repo_root = str(repo)
    agent._workspace_context()
    assert agent._workspace_built is True
    agent.invalidate_workspace_context()
    assert agent._workspace_built is False
    assert agent._workspace_block is None


def test_r4_4_disabled_returns_none(repo: Path):
    agent = DevAgent(router=MagicMock())
    agent._repo_context_enabled = False    # default
    agent._repo_root = str(repo)
    assert agent._workspace_context() is None   # off → no injection (byte-identical)


def test_r4_3_build_failure_degrades(monkeypatch):
    agent = DevAgent(router=MagicMock())
    agent._repo_context_enabled = True

    def boom(root, **kw):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr("inference.workspace_context.build_workspace_context", boom)
    assert agent._workspace_context() is None   # never raises into the plan path
