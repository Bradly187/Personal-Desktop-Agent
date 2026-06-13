"""Tests for scripts/agent_watchdog.ps1 — the REAL launch path.

The watchdog's -TestChild hook substitutes the child script but keeps the
production command-line construction: real venv python, real cmd quoting
(/d /s /c), real log redirection, real exit-code propagation. This is the
path that was broken at review (cmd's strip-first-and-last-quote rule put the
redirect inside a quoted span and the child never started), so these tests
deliberately do NOT use -TestCommand, which bypasses it.

Covers:
  - clean exit (code 0): watchdog exits 0, child stdout reaches the log
  - crash then clean: one restart, previous log preserved as .prev.log
  - crash loop: gives up after MaxRestarts within the window, exits 0

Windows-only (PowerShell + cmd semantics are the subject under test).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="exercises Windows cmd/PowerShell semantics"
)

_REPO = Path(__file__).resolve().parent.parent
_WATCHDOG = _REPO / "scripts" / "agent_watchdog.ps1"


def _run_watchdog(child: Path, logdir: Path, *extra: str) -> subprocess.CompletedProcess:
    cmd = [
        "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", str(_WATCHDOG),
        "-Target", "agent",
        "-TestChild", str(child),
        "-TestLogDir", str(logdir),
        *extra,
    ]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=90)


def _write_child(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_clean_exit_passes_through(tmp_path: Path):
    # Space in the path on purpose — the quoting bug class this guards against
    # is exactly "the command line stopped surviving cmd's quote handling".
    child = _write_child(
        tmp_path / "with space" / "child.py",
        "print('CHILD-OK')\nraise SystemExit(0)\n",
    )
    logdir = tmp_path / "logs"

    result = _run_watchdog(child, logdir)

    assert result.returncode == 0, result.stdout + result.stderr
    out_log = (logdir / "agent_startup.log").read_text()
    assert "CHILD-OK" in out_log  # redirection actually reached the log file
    watch_log = (logdir / "watchdog_agent.log").read_text()
    assert "exited cleanly" in watch_log
    assert "restarting" not in watch_log


def test_crash_then_clean_restarts_once(tmp_path: Path):
    # First run: no state file -> create it, exit 7. Second run: exit 0.
    state = tmp_path / "ran_once"
    child = _write_child(
        tmp_path / "child.py",
        (
            "import pathlib, sys\n"
            f"state = pathlib.Path({str(state)!r})\n"
            "if state.exists():\n"
            "    print('SECOND-RUN')\n"
            "    sys.exit(0)\n"
            "state.write_text('x')\n"
            "print('FIRST-RUN')\n"
            "sys.exit(7)\n"
        ),
    )
    logdir = tmp_path / "logs"

    result = _run_watchdog(child, logdir)

    assert result.returncode == 0, result.stdout + result.stderr
    watch_log = (logdir / "watchdog_agent.log").read_text()
    assert "exited with code 7" in watch_log
    assert "restarting" in watch_log
    assert "exited cleanly" in watch_log
    # The crashed run's output trail survives the restart as .prev.log
    assert "FIRST-RUN" in (logdir / "agent_startup.prev.log").read_text()
    assert "SECOND-RUN" in (logdir / "agent_startup.log").read_text()


def test_crash_loop_gives_up_after_bound(tmp_path: Path):
    child = _write_child(tmp_path / "child.py", "raise SystemExit(9)\n")
    logdir = tmp_path / "logs"

    result = _run_watchdog(child, logdir, "-MaxRestarts", "2")

    # exit 0 on purpose: Task Scheduler's RestartCount is reserved for the
    # watchdog itself dying, not for re-running a crash-looping child.
    assert result.returncode == 0, result.stdout + result.stderr
    watch_log = (logdir / "watchdog_agent.log").read_text()
    assert "CRASH LOOP" in watch_log
    assert watch_log.count("exited with code 9") == 2
