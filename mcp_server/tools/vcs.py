"""Version Control System (VCS) MCP Tools.

Provides structured wrappers around git operations to avoid terminal parsing errors.
"""

import subprocess
from typing import Dict, Any

from core.goal_session import GoalSessionStore, _path_in_scope

def _get_repo_root(cwd: str = ".") -> str:
    """Find the root of the git repository."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd, capture_output=True, text=True, check=True
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        return ""

def _is_safe_repo(path: str) -> bool:
    """Check if the path is within the allowed writable_roots."""
    session = GoalSessionStore.get_active()
    scopes = session.cwd_scope if session and session.cwd_scope else []
    # If there are no active scopes, we allow it (meaning the boundary isn't active).
    # But if there ARE scopes, it must be within one of them.
    if scopes:
        return _path_in_scope(path, scopes)
    return True

def _run_git(args: list[str], cwd: str = ".") -> tuple[bool, str]:
    """Helper to run a git command and return (success, output_or_error)."""
    repo = _get_repo_root(cwd)
    if not repo:
        return False, "Not inside a git repository."
        
    if not _is_safe_repo(repo):
        return False, f"Git repository at {repo} is outside the allowed writable_roots."
        
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace'
        )
        if result.returncode == 0:
            return True, result.stdout.strip()
        else:
            return False, result.stderr.strip() or result.stdout.strip()
    except Exception as e:
        return False, str(e)

def git_create_branch(branch_name: str, cwd: str = ".") -> Dict[str, Any]:
    """Create and switch to a new branch."""
    success, out = _run_git(["checkout", "-b", branch_name], cwd=cwd)
    if success:
        return {"ok": True, "branch": branch_name}
    return {"ok": False, "error": out}

def git_checkout(branch_name: str, cwd: str = ".") -> Dict[str, Any]:
    """Switch to an existing branch."""
    success, out = _run_git(["checkout", branch_name], cwd=cwd)
    if success:
        return {"ok": True, "branch": branch_name}
    return {"ok": False, "error": out}

def git_commit(message: str, add_all: bool = True, cwd: str = ".") -> Dict[str, Any]:
    """Stage changes and commit them with structured metadata."""
    if add_all:
        success, out = _run_git(["add", "--all"], cwd=cwd)
        if not success:
            return {"ok": False, "error": f"git add failed: {out}"}
            
    # Check if there is anything to commit
    st_success, st_out = _run_git(["status", "--porcelain"], cwd=cwd)
    if st_success and not st_out.strip():
        return {"ok": False, "error": "Nothing to commit."}

    success, out = _run_git(["commit", "-m", message], cwd=cwd)
    if success:
        # Get the new commit hash
        hash_success, hash_out = _run_git(["rev-parse", "HEAD"], cwd=cwd)
        commit_hash = hash_out if hash_success else "unknown"
        return {"ok": True, "commit": commit_hash}
    return {"ok": False, "error": out}

def git_diff(target: str = "", cwd: str = ".") -> Dict[str, Any]:
    """Read differences between branches or commits in a structured format."""
    args = ["diff", "--no-color"]
    if target:
        args.append(target)
        
    success, out = _run_git(args, cwd=cwd)
    if success:
        return {"ok": True, "diff": out}
    return {"ok": False, "error": out}
