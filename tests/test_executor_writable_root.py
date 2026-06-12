"""M2 — CommandExecutor writable-root allowlist for WRITE_FILE / RUN_TERMINAL cwd.

A replanned or injected dev step must not be able to write outside an allowed
root or run a terminal with a cwd outside it. The guard lives in the executor
itself, independent of goal_session (whose cwd_scope only governs Claude Code's
Write/Edit tools, not the executor's WRITE_FILE/RUN_TERMINAL verbs).

Run:
    python -m pytest tests/test_executor_writable_root.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.command_executor import Command, CommandExecutor


def _executor(tmp_path) -> CommandExecutor:
    ex = CommandExecutor()
    ex._writable_roots = [str(tmp_path)]  # scope writes to the test tmpdir only
    return ex


def _write_cmd(path, content="hi") -> Command:
    return Command(text=content, action="WRITE_FILE", source="test",
                   params={"path": str(path), "content": content})


def test_write_inside_scope_succeeds(tmp_path):
    ex = _executor(tmp_path)
    target = tmp_path / "sub" / "note.txt"
    res = ex._dispatch("WRITE_FILE", _write_cmd(target))
    assert res.get("written") == str(target)
    assert target.read_text(encoding="utf-8") == "hi"


def test_write_traversal_outside_scope_denied(tmp_path):
    ex = _executor(tmp_path)
    escape = tmp_path / ".." / "escape.txt"
    res = ex._dispatch("WRITE_FILE", _write_cmd(escape))
    assert res.get("status") == "error"
    assert "writable-root" in res.get("error", "")
    assert not (tmp_path.parent / "escape.txt").exists()


def test_write_absolute_outside_scope_denied(tmp_path):
    ex = _executor(tmp_path)
    outside = tmp_path.parent / "outside_root.txt"
    res = ex._dispatch("WRITE_FILE", _write_cmd(outside))
    assert res.get("status") == "error"
    assert not outside.exists()


def test_run_terminal_cwd_outside_scope_denied(tmp_path):
    ex = _executor(tmp_path)
    cmd = Command(text="echo hi", action="RUN_TERMINAL", source="test",
                  params={"command": "echo hi", "cwd": str(tmp_path.parent)})
    res = ex._dispatch("RUN_TERMINAL", cmd)
    assert res.get("status") == "error"
    assert "writable-root" in res.get("error", "")


def test_run_terminal_cwd_inside_scope_not_denied(tmp_path):
    ex = _executor(tmp_path)
    cmd = Command(text="echo hi", action="RUN_TERMINAL", source="test",
                  params={"command": "echo hi", "cwd": str(tmp_path)})
    res = ex._dispatch("RUN_TERMINAL", cmd)
    # The allowlist must not block an in-scope cwd; the command runs normally.
    assert "writable-root" not in res.get("error", "")


def test_default_roots_are_not_everything():
    # A fresh executor's default allowlist must be bounded — never the FS root.
    from core.command_executor import _REPO_ROOT
    ex = CommandExecutor()
    assert ex._writable_roots
    assert all(r not in ("/", "\\") for r in ex._writable_roots)
    # A path at the drive/filesystem root is outside repo-root and system-temp.
    outside = Path(_REPO_ROOT).anchor + "da_m2_should_not_exist.txt"
    res = ex._dispatch("WRITE_FILE", _write_cmd(outside))
    assert res.get("status") == "error"
    assert not Path(outside).exists()
