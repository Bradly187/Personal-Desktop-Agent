"""Active-directory switching + attachment wiring (specs/chat-context-attachments).

Covers the non-UI surfaces: CommandExecutor active-root setters (R1.2/R1.3),
HybridCoordinator.set_active_directory browse+confirm logic (R1.2–R1.4) and
list_writable_roots (R1.5), and DevAgent.set_repo_root (R1.2). The pure extractor
is in test_attachments.py.
"""

from __future__ import annotations

import os

import pytest

from core.command_executor import CommandExecutor


# --- CommandExecutor active-root (R1.2/R1.3) --------------------------------

def test_add_writable_root_valid_dir(tmp_path):
    ex = CommandExecutor()
    before = len(ex.writable_roots)
    assert ex.add_writable_root(str(tmp_path)) is True
    assert os.path.realpath(str(tmp_path)) in ex.writable_roots
    # Idempotent — re-adding doesn't duplicate.
    ex.add_writable_root(str(tmp_path))
    assert len(ex.writable_roots) == before + 1


def test_add_writable_root_rejects_nonexistent():
    ex = CommandExecutor()
    before = list(ex.writable_roots)
    assert ex.add_writable_root("Z:/no/such/dir/ever") is False
    assert ex.writable_roots == before          # scope unchanged


def test_set_active_root_activates_and_appends(tmp_path):
    ex = CommandExecutor()
    assert ex.active_root is None
    assert ex.set_active_root(str(tmp_path)) is True
    assert ex.active_root == os.path.realpath(str(tmp_path))
    assert os.path.realpath(str(tmp_path)) in ex.writable_roots


def test_set_active_root_invalid_changes_nothing():
    ex = CommandExecutor()
    assert ex.set_active_root("Z:/nope") is False
    assert ex.active_root is None


# --- HybridCoordinator browse + confirm (R1.2–R1.5) -------------------------

@pytest.fixture
def coord():
    from core.hybrid_coordinator import HybridCoordinator
    c = HybridCoordinator()

    class _FakeDev:
        def __init__(self): self.repo_root = None
        def set_repo_root(self, p): self.repo_root = p; return True
    c.set_dev_agent(_FakeDev())
    return c


def test_set_active_directory_invalid(coord):
    res = coord.set_active_directory("Z:/missing")
    assert res["status"] == "invalid"


def test_new_root_requires_confirm_then_activates(coord, tmp_path):
    # Pin the allowlist to `allowed/` so `proj/` is genuinely outside it (the
    # default allowlist includes the system temp dir, which tmp_path lives under).
    allowed = tmp_path / "allowed"; allowed.mkdir()
    proj = tmp_path / "proj"; proj.mkdir()
    coord._executor._writable_roots = [str(allowed)]

    # First touch: outside the allowlist → must confirm, nothing changes.
    res = coord.set_active_directory(str(proj))
    assert res["status"] == "confirm_required"
    assert os.path.realpath(str(proj)) not in coord._executor.writable_roots
    # Confirm: appended + activated + DevAgent re-pointed.
    res2 = coord.set_active_directory(str(proj), confirm=True)
    assert res2["status"] == "activated"
    rp = os.path.realpath(str(proj))
    assert rp in coord._executor.writable_roots
    assert coord._executor.active_root == rp
    assert coord._dev_agent.repo_root == rp


def test_in_scope_subdir_activates_without_confirm(coord, tmp_path):
    base = tmp_path / "base"; base.mkdir()
    coord._executor._writable_roots = [str(base)]
    sub = base / "child"; sub.mkdir()
    # A subdir of an allowed root is already in scope → no confirm needed (R1.2).
    res = coord.set_active_directory(str(sub))
    assert res["status"] == "activated"


def test_list_writable_roots_shape(coord, tmp_path):
    base = tmp_path / "base"; base.mkdir()
    coord._executor._writable_roots = [str(base)]
    coord.set_active_directory(str(base))
    info = coord.list_writable_roots()
    assert "active_root" in info and "writable_roots" in info
    assert info["active_root"] == os.path.realpath(str(base))


# --- DevAgent.set_repo_root (R1.2) ------------------------------------------

def test_dev_agent_set_repo_root(tmp_path):
    from inference.dev_agent import DevAgent
    dev = DevAgent(None, None, None)
    assert dev.set_repo_root(str(tmp_path)) is True
    assert dev._repo_root == os.path.realpath(str(tmp_path))
    assert dev.set_repo_root("Z:/nope") is False
    assert dev._repo_root == os.path.realpath(str(tmp_path))   # unchanged
