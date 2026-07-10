import logging

log = logging.getLogger(__name__)

def run_terminal(cmd: str) -> str:
    cmd = cmd.strip()
    log.info("DevAgent: running terminal command: %s", cmd)
    # Sandbox (mistake-containment): cwd-jail + resource/output caps. Network
    # is granted only for curated package/VCS/fetch ops (pip install, git
    # push, …); everything else stays offline. Those ops are themselves
    # approval-gated by the goal-session allowlist upstream.
    from inference.sandbox import run_sandboxed, command_needs_network
    # Slopsquatting guard (GAP-7): block a `pip install` of a package that
    # doesn't exist on PyPI (a hallucinated name is the supply-chain threat).
    # Fails open on a network error so offline dev isn't blocked.
    from core.goal_session import verify_pip_install
    ok, reason = verify_pip_install(cmd)
    if not ok:
        log.warning("DevAgent: blocked pip install — %s", reason)
        raise RuntimeError(reason)
    net = command_needs_network(cmd)
    result = run_sandboxed(cmd, timeout=60, allow_network=net)
    output = (result.stdout + result.stderr).strip()
    status = "ok" if result.returncode == 0 else f"exit {result.returncode}"
    log.info("DevAgent: terminal %s%s%s → %s",
             status, "" if result.sandboxed else " [unsandboxed]",
             " [net]" if net else "", output[:120])
    if result.returncode != 0:
        raise RuntimeError(f"Command failed ({status}): {output[:200]}")
    return output or status
