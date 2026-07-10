import logging
import subprocess

log = logging.getLogger(__name__)

def git_status() -> str:
    result = subprocess.run(
        ["git", "status", "--short", "--branch"],
        capture_output=True, text=True, timeout=10,
    )
    out = result.stdout.strip()
    if result.returncode != 0:
        raise RuntimeError(f"git status failed: {result.stderr.strip()[:200]}")
    return out or "(nothing to commit, working tree clean)"

def git_diff(flags: str = "", max_chars: int = 8000) -> str:
    cmd = ["git", "diff"]
    if flags:
        cmd.extend(flags.split())
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git diff failed: {result.stderr.strip()[:200]}")
    out = result.stdout.strip()
    if len(out) > max_chars:
        out = out[:max_chars] + f"\n… [truncated at {max_chars} chars]"
    return out or "(no diff)"

def git_commit(message: str) -> str:
    # Stage all tracked changes then commit. Capture output and raise a
    # RuntimeError with stderr (not a raw CalledProcessError) so a staging
    # failure surfaces consistently with the commit path / saga (#30).
    add = subprocess.run(
        ["git", "add", "-u"], capture_output=True, text=True, timeout=10,
    )
    if add.returncode != 0:
        raise RuntimeError(f"git add failed: {add.stderr.strip()[:200]}")
    result = subprocess.run(
        ["git", "commit", "-m", message],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git commit failed: {result.stderr.strip()[:200]}")
    out = result.stdout.strip()
    log.info("DevAgent: git commit — %s", out[:100])
    return out

def git_checkout(branch_args: str) -> str:
    cmd = ["git", "checkout"] + branch_args.split()
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git checkout failed: {result.stderr.strip()[:200]}")
    return result.stdout.strip() or result.stderr.strip() or "ok"

def github_pr(title: str, body: str) -> str:
    """Create a GitHub PR using the gh CLI and return the PR URL."""
    cmd = ["gh", "pr", "create", "--title", title]
    if body:
        cmd.extend(["--body", body])
    else:
        cmd.extend(["--body", "Created by Personal Desktop Agent via voice command."])
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh pr create failed: {result.stderr.strip()[:200]}")
    url = result.stdout.strip()
    log.info("DevAgent: PR created — %s", url)
    return url
