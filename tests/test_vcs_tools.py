import os
import subprocess
import pytest
from pathlib import Path

from mcp_server.tools import vcs

@pytest.fixture
def temp_git_repo(tmp_path):
    # Initialize a temporary git repo
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    # Configure git so commits work in CI
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True, capture_output=True)
    
    # Create an initial commit
    (tmp_path / "README.md").write_text("Hello World")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=tmp_path, check=True, capture_output=True)
    
    return str(tmp_path)

def test_git_create_and_checkout_branch(temp_git_repo):
    # Create branch
    res = vcs.git_create_branch("feature/test", cwd=temp_git_repo)
    assert res["ok"] is True
    assert res["branch"] == "feature/test"
    
    # Check current branch
    proc = subprocess.run(["git", "branch", "--show-current"], cwd=temp_git_repo, capture_output=True, text=True)
    assert proc.stdout.strip() == "feature/test"
    
    # Checkout master
    res2 = vcs.git_checkout("master", cwd=temp_git_repo)
    if not res2["ok"]: # depending on git version default branch might be main
        res2 = vcs.git_checkout("main", cwd=temp_git_repo)
    assert res2["ok"] is True

def test_git_commit(temp_git_repo):
    # Create a new file
    repo_path = Path(temp_git_repo)
    (repo_path / "new_file.txt").write_text("Some changes")
    
    # Commit
    res = vcs.git_commit("Add new_file.txt", add_all=True, cwd=temp_git_repo)
    assert res["ok"] is True
    assert "commit" in res
    
    # Try empty commit
    res2 = vcs.git_commit("Empty commit", add_all=True, cwd=temp_git_repo)
    assert res2["ok"] is False
    assert "Nothing to commit" in res2["error"]

def test_git_diff(temp_git_repo):
    repo_path = Path(temp_git_repo)
    (repo_path / "README.md").write_text("Hello Universe")
    
    # Diff should show the change
    res = vcs.git_diff(cwd=temp_git_repo)
    assert res["ok"] is True
    assert "Hello Universe" in res["diff"]
    assert "Hello World" in res["diff"]

def test_outside_repo(tmp_path):
    # Running git tools in a non-git directory
    res = vcs.git_diff(cwd=str(tmp_path))
    assert res["ok"] is False
    assert "Not inside a git repository" in res["error"]
