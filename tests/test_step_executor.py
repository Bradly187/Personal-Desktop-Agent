"""B5 — approval-card diff cap restored to the spec'd 400 lines.

Spec: specs/bugfix-b5-diff-cap/ (chat-workbench-parity R5.1)

After PR #164 split DevAgent into StepExecutor, ``_CONFIRM_DIFF_MAX_LINES`` was
forked: the live copy in ``step_executor.py`` truncated approval diffs at 100
lines (4x harder than the spec'd 400), while a dead 400-line copy lingered as an
unused class attribute on ``DevAgent``. A 300-line refactor diff was shown to the
user as only 100 lines, making an informed voice approval impossible.

These tests lock the live constant at 400, verify truncation behavior at the
boundary, and guard against the dead copy reappearing.
"""
from pathlib import Path

import inference.step_executor as step_executor
from inference.step_executor import _CONFIRM_DIFF_MAX_LINES, diff_for_confirm


def test_diff_cap_is_400():
    assert _CONFIRM_DIFF_MAX_LINES == 400


def test_small_diff_is_not_truncated(tmp_path):
    f = tmp_path / "small.txt"
    f.write_text("", encoding="utf-8")
    new_text = "".join(f"line {i}\n" for i in range(50))  # ~53-line diff, under cap
    out = diff_for_confirm(str(f), new_text)
    assert "more lines" not in out
    assert len(out.splitlines()) <= _CONFIRM_DIFF_MAX_LINES


def test_large_diff_is_truncated_with_marker(tmp_path):
    f = tmp_path / "big.txt"
    f.write_text("", encoding="utf-8")
    new_text = "".join(f"line {i}\n" for i in range(600))  # >400 diff lines
    out = diff_for_confirm(str(f), new_text)
    lines = out.splitlines()
    # Exactly the cap plus one appended truncation marker.
    assert len(lines) == _CONFIRM_DIFF_MAX_LINES + 1
    assert "more lines" in lines[-1]


def test_diff_for_confirm_is_presentation_safe(tmp_path):
    # Presentation-only: a nonexistent path degrades to a from-empty diff and
    # never raises (an unreadable file yields "" via the except guard).
    out = diff_for_confirm(str(tmp_path / "does-not-exist.txt"), "x\n")
    assert isinstance(out, str)
    assert "+x" in out  # diffed against empty current content, no crash


def test_dead_copy_removed_from_dev_agent():
    # R1.4/R1.5: the constant must live in exactly one module. The dead
    # DevAgent class attribute (dev_agent.py:879) must be gone.
    dev_agent_src = (
        Path(step_executor.__file__).resolve().parent / "dev_agent.py"
    ).read_text(encoding="utf-8")
    assert "_CONFIRM_DIFF_MAX_LINES" not in dev_agent_src
