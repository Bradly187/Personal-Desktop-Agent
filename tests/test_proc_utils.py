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
