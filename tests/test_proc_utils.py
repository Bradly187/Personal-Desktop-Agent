"""Tests for core/proc_utils — run_capped (timeout → tree-kill) and
kill_process_tree.

Validates the hardening that a runaway command which forked grandchildren is
fully reaped on timeout, not left orphaned (the real gap on the Windows-native
RUN_TERMINAL path and for npm / code-eval).
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.proc_utils import run_capped, kill_process_tree

psutil = pytest.importorskip("psutil")


def test_run_capped_fast_success_captures_output():
    r = run_capped(
        [sys.executable, "-c", "print('hello')"],
        timeout=10, capture_output=True, text=True,
    )
    assert r.returncode == 0
    assert "hello" in (r.stdout or "")


def test_run_capped_nonzero_returncode():
    r = run_capped(
        [sys.executable, "-c", "import sys; sys.exit(3)"],
        timeout=10, capture_output=True, text=True,
    )
    assert r.returncode == 3


def test_run_capped_timeout_raises_quickly():
    t0 = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_capped(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout=1.0, capture_output=True, text=True,
        )
    elapsed = time.monotonic() - t0
    # Must return shortly after the 1s timeout — NOT wait the full 30s sleep.
    assert elapsed < 15.0


def test_run_capped_timeout_kills_grandchild():
    """A timed-out parent that forked a long-lived grandchild must have the WHOLE
    tree reaped — the grandchild pid must be gone afterward."""
    parent_src = (
        "import subprocess, sys, time\n"
        "gc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print(gc.pid, flush=True)\n"
        "time.sleep(60)\n"
    )
    with pytest.raises(subprocess.TimeoutExpired) as ei:
        run_capped(
            [sys.executable, "-c", parent_src],
            timeout=2.0, capture_output=True, text=True,
        )
    out = ei.value.output or ""
    gc_pid = int(out.strip().splitlines()[0])
    # Give the OS a beat to finish reaping after the tree-kill.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and psutil.pid_exists(gc_pid):
        time.sleep(0.1)
    assert not psutil.pid_exists(gc_pid), "grandchild survived the tree-kill"


def test_kill_process_tree_terminates_live_process():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    assert proc.poll() is None        # alive
    kill_process_tree(proc.pid)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and proc.poll() is None:
        time.sleep(0.1)
    assert proc.poll() is not None, "process was not killed"


def test_kill_process_tree_no_such_pid_is_noop():
    # A pid that does not exist must not raise.
    kill_process_tree(2_000_000_000)


def test_run_capped_closes_stdin_by_default():
    # stdin defaults to DEVNULL: a child reading stdin sees immediate EOF and
    # exits fast instead of blocking until the timeout. read() → '' proves it.
    r = run_capped(
        [sys.executable, "-c", "import sys; print(repr(sys.stdin.read()))"],
        timeout=10, capture_output=True, text=True,
    )
    assert r.returncode == 0
    assert "''" in (r.stdout or "")


def test_run_capped_child_cannot_inherit_proxy_auth(monkeypatch):
    monkeypatch.setenv("WSL_ACTION_PROXY_TOKEN", "secret-token")
    monkeypatch.setenv("WSL_ACTION_PROXY_INSTANCE", "instance-id")
    monkeypatch.setenv("CUSTOM_VALUE", "keep-me")
    code = (
        "import json,os; print(json.dumps({"
        "'token': os.environ.get('WSL_ACTION_PROXY_TOKEN'), "
        "'instance': os.environ.get('WSL_ACTION_PROXY_INSTANCE'), "
        "'custom': os.environ.get('CUSTOM_VALUE')}))"
    )

    result = run_capped(
        [sys.executable, "-c", code],
        timeout=10,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == '{"token": null, "instance": null, "custom": "keep-me"}'


@pytest.mark.parametrize("verified", [True, False])
def test_run_capped_timeout_exposes_termination_evidence(monkeypatch, verified):
    import core.proc_utils as proc_utils
    from core.proc_utils import ProcessTimeoutError, TerminationEvidence

    class FakeProcess:
        pid = 12345
        returncode = None

        def __init__(self):
            self.calls = 0

        def communicate(self, timeout):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired("cmd", timeout)
            return "out", "err"

    fake = FakeProcess()
    monkeypatch.setattr(proc_utils.subprocess, "Popen", lambda *a, **k: fake)
    monkeypatch.setattr(
        proc_utils,
        "kill_process_tree",
        lambda pid: TerminationEvidence(verified, "test", () if verified else (pid,)),
    )

    with pytest.raises(ProcessTimeoutError) as captured:
        run_capped("cmd", timeout=0.01, capture_output=True, text=True, shell=True)

    assert captured.value.termination_verified is verified
    assert ("verified" if verified else "unverified") in captured.value.termination_detail
    assert fake.calls == 2


def test_kill_process_tree_returns_verified_evidence_for_missing_pid():
    evidence = kill_process_tree(2_000_000_000)
    assert evidence.verified is True


def test_posix_fallback_signal_delivery_is_not_claimed_verified(monkeypatch):
    import core.proc_utils as proc_utils

    monkeypatch.setattr(proc_utils, "_IS_WINDOWS", False)
    monkeypatch.setattr(proc_utils.os, "getpgid", lambda pid: pid, raising=False)
    monkeypatch.setattr(proc_utils.os, "killpg", lambda pgid, signal: None, raising=False)
    evidence = proc_utils._kill_tree_no_psutil(123)
    assert evidence.verified is False
    assert evidence.alive_pids == (123,)


def test_windows_fallback_taskkill_success_is_still_conservative(monkeypatch):
    import core.proc_utils as proc_utils

    monkeypatch.setattr(proc_utils, "_IS_WINDOWS", True)
    monkeypatch.setattr(
        proc_utils.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0),
    )
    evidence = proc_utils._kill_tree_no_psutil(456)
    assert evidence.verified is False
    assert evidence.alive_pids == (456,)
