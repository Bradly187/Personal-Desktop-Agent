"""Run children under a wall timeout and retain process-tree kill evidence.

Children run in their own process group.  A timeout terminates the complete tree
and raises :class:`ProcessTimeoutError`, which preserves whether termination was
actually verified so callers can distinguish failure from an unknown outcome.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from typing import Optional, Sequence, Union

from core.proxy_auth import sanitized_child_env

log = logging.getLogger(__name__)
_IS_WINDOWS = os.name == "nt"


@dataclass(frozen=True)
class TerminationEvidence:
    verified: bool
    method: str
    alive_pids: tuple[int, ...] = ()

    @property
    def detail(self) -> str:
        if self.verified:
            return f"process tree termination verified ({self.method})"
        alive = f"; possibly alive={list(self.alive_pids)}" if self.alive_pids else ""
        return f"process tree termination unverified ({self.method}){alive}"


class ProcessTimeoutError(subprocess.TimeoutExpired):
    """Timeout carrying explicit process-tree termination evidence."""

    def __init__(self, *args, termination: TerminationEvidence, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.termination = termination
        self.termination_verified = termination.verified
        self.termination_detail = termination.detail


def kill_process_tree(pid: int, *, grace: float = 2.0) -> TerminationEvidence:
    """Terminate ``pid`` and descendants, returning conservative evidence."""
    try:
        import psutil
    except Exception:
        return _kill_tree_no_psutil(pid)
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return TerminationEvidence(True, "psutil:not-found")
    except Exception as exc:
        log.debug("kill_process_tree(%s): psutil lookup failed: %s", pid, exc)
        return _kill_tree_no_psutil(pid)

    try:
        procs = parent.children(recursive=True)
    except Exception:
        procs = []
    procs.append(parent)

    for proc in procs:
        try:
            proc.terminate()
        except psutil.NoSuchProcess:
            pass
        except Exception:
            pass
    try:
        _gone, alive = psutil.wait_procs(procs, timeout=grace)
    except Exception:
        alive = procs
    for proc in alive:
        try:
            proc.kill()
        except psutil.NoSuchProcess:
            pass
        except Exception:
            pass
    try:
        _gone, alive = psutil.wait_procs(alive, timeout=grace)
    except Exception:
        # If verification itself fails, preserve the known candidate pids.
        return TerminationEvidence(
            False, "psutil:verification-error",
            tuple(sorted({getattr(proc, "pid", pid) for proc in alive})),
        )
    alive_pids = tuple(sorted({proc.pid for proc in alive if proc.is_running()}))
    return TerminationEvidence(not alive_pids, "psutil", alive_pids)


def _kill_tree_no_psutil(pid: int) -> TerminationEvidence:
    if _IS_WINDOWS:
        try:
            result = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                timeout=10,
                env=sanitized_child_env(),
            )
            # taskkill success proves request acceptance, not that every child
            # has exited. Without an independent process enumerator this remains
            # conservatively unknown.
            return TerminationEvidence(
                False,
                "taskkill:signal-sent" if result.returncode == 0 else "taskkill:failed",
                (pid,),
            )
        except Exception as exc:
            log.debug("taskkill /T %s failed: %s", pid, exc)
            return TerminationEvidence(False, "taskkill:error", (pid,))

    import signal
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)  # type: ignore[attr-defined]
    except ProcessLookupError:
        return TerminationEvidence(True, "killpg:not-found")
    except PermissionError:
        try:
            os.kill(pid, signal.SIGKILL)  # type: ignore[attr-defined]
        except ProcessLookupError:
            return TerminationEvidence(True, "kill:not-found")
        except Exception as exc:
            log.debug("kill %s failed: %s", pid, exc)
            return TerminationEvidence(False, "kill:error", (pid,))
        return TerminationEvidence(False, "kill:signal-sent", (pid,))
    except Exception as exc:
        log.debug("killpg %s failed: %s", pid, exc)
        return TerminationEvidence(False, "killpg:error", (pid,))
    return TerminationEvidence(False, "killpg:signal-sent", (pid,))


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
    """Like ``subprocess.run`` but kill and verify the whole tree on timeout."""
    creationflags = 0
    start_new_session = False
    if _IS_WINDOWS:
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        start_new_session = True

    stdout = subprocess.PIPE if capture_output else None
    stderr = subprocess.PIPE if capture_output else None
    proc = subprocess.Popen(
        args,
        shell=shell,
        cwd=cwd,
        env=sanitized_child_env(env),
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
        termination = kill_process_tree(proc.pid)
        try:
            out, err = proc.communicate(timeout=5)
        except Exception:
            out, err = None, None
        raise ProcessTimeoutError(
            args, timeout, output=out, stderr=err, termination=termination
        )
    except BaseException:
        kill_process_tree(proc.pid)
        raise
    return subprocess.CompletedProcess(args, proc.returncode, out, err)
