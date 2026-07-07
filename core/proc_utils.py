"""core/proc_utils.py — run a child process under a wall-clock timeout and, on
overrun, kill the WHOLE process tree (not just the direct child).

`subprocess.run(timeout=...)` kills only the immediate child when the timeout
fires; any grandchildren it spawned (npm → node → git, a shell → a runaway loop,
LLM-generated code → its own subprocess) are orphaned and keep burning CPU/VRAM.
This wrapper launches the child in its OWN process group — POSIX `start_new_session`
(setsid) / Windows `CREATE_NEW_PROCESS_GROUP` — and on timeout reaps the entire
tree: psutil when available (recursive, cross-platform), else `os.killpg` (POSIX)
or `taskkill /T` (Windows).

This is mistake-containment for the agent's own hallucinated/looping commands —
layered behind the goal-session allowlist and the sandbox — not adversarial
isolation. `run_capped` mirrors `subprocess.run`'s contract closely enough to be a
drop-in for the timeout-sensitive spawn sites (RUN_TERMINAL sandbox, npm install,
code-eval): it returns a `CompletedProcess` and re-raises `TimeoutExpired` after
the tree is killed.
"""

from __future__ import annotations

import logging
import os
import subprocess
from typing import Optional, Sequence, Union

log = logging.getLogger(__name__)

_IS_WINDOWS = os.name == "nt"


def kill_process_tree(pid: int, *, grace: float = 2.0) -> None:
    """Best-effort terminate→kill of `pid` and all its descendants.

    Tries psutil first (recursive, cross-platform). Falls back to a process-group
    kill on POSIX and `taskkill /F /T` on Windows. Never raises.
    """
    try:
        import psutil
    except Exception:
        _kill_tree_no_psutil(pid)
        return
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    except Exception as exc:
        log.debug("kill_process_tree(%s): psutil lookup failed: %s", pid, exc)
        _kill_tree_no_psutil(pid)
        return

    try:
        procs = parent.children(recursive=True)
    except Exception:
        procs = []
    procs.append(parent)

    for p in procs:
        try:
            p.terminate()
        except Exception:
            pass
    try:
        _gone, alive = psutil.wait_procs(procs, timeout=grace)
    except Exception:
        alive = procs
    for p in alive:
        try:
            p.kill()
        except Exception:
            pass


def _kill_tree_no_psutil(pid: int) -> None:
    if _IS_WINDOWS:
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, timeout=10)
        except Exception as exc:
            log.debug("taskkill /T %s failed: %s", pid, exc)
        return
    import signal
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)  # type: ignore[attr-defined]
    except (ProcessLookupError, PermissionError):
        try:
            os.kill(pid, signal.SIGKILL)  # type: ignore[attr-defined]
        except ProcessLookupError:
            pass
    except Exception as exc:
        log.debug("killpg %s failed: %s", pid, exc)


def run_capped(
    args: Union[str, Sequence[str]],
    *,
    timeout: float,
    capture_output: bool = False,
    text: bool = False,
    shell: bool = False,
    cwd: Optional[str] = None,
    env: Optional[dict] = None,
    preexec_fn=None,
    stdin: Optional[int] = subprocess.DEVNULL,
) -> subprocess.CompletedProcess:
    """Like `subprocess.run(..., timeout=timeout)` but kills the whole process
    tree (not just the direct child) when the timeout fires, then re-raises
    `subprocess.TimeoutExpired` — same contract as `subprocess.run`.

    The child runs in its own process group so the tree-kill can reach every
    descendant. `preexec_fn` (POSIX rlimits etc.) is honored on POSIX only.

    `stdin` defaults to `DEVNULL` (not inherited): these are non-interactive
    agent spawn sites, so a command that reads stdin gets immediate EOF and
    fails fast instead of blocking on a TTY until the wall timeout. Pass
    `stdin=None` to restore inheritance for the rare caller that needs it.
    """
    creationflags = 0
    start_new_session = False
    if _IS_WINDOWS:
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        start_new_session = True   # setsid → own group, so killpg reaps the tree

    stdout = subprocess.PIPE if capture_output else None
    stderr = subprocess.PIPE if capture_output else None

    proc = subprocess.Popen(
        args,
        shell=shell,
        cwd=cwd,
        env=env,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        text=text,
        preexec_fn=(preexec_fn if not _IS_WINDOWS else None),
        creationflags=creationflags,
        start_new_session=start_new_session,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_process_tree(proc.pid)
        # Drain the pipes after the kill so handles close and we don't leak.
        try:
            out, err = proc.communicate(timeout=5)
        except Exception:
            out, err = None, None
        raise subprocess.TimeoutExpired(args, timeout, output=out, stderr=err)
    except BaseException:
        # Any other interruption (e.g. KeyboardInterrupt) — don't orphan the tree.
        kill_process_tree(proc.pid)
        raise
    return subprocess.CompletedProcess(args, proc.returncode, out, err)
