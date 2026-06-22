"""Tests for the autonomous Tester loop (specs/dev-agent-critic R3).

Model + sandbox are stubbed so these run in CI. Covers source-file selection,
generate/run outcomes, graceful degrade (never a false pass), and the DevAgent
safe-observation wiring (append outcome to the step result; never roll back).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import inference.sandbox as sandbox
from inference.tester import (
    Tester, TestOutcome, _extract_code_block, is_testable_source,
)
from inference.dev_agent import AgentStep, DevAgent


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("path,ok", [
    ("inference/foo.py", True),
    ("E:/proj/core/bar.py", True),
    ("tests/test_foo.py", False),       # a test file
    ("pkg/foo_test.py", False),
    ("tests/helpers/util.py", False),   # under a tests/ tree
    ("conftest.py", False),
    ("README.md", False),
    ("script.sh", False),
    ("", False),
])
def test_is_testable_source(path, ok):
    assert is_testable_source(path) is ok


def test_extract_code_block_prefers_fence():
    assert _extract_code_block("blah\n```python\nX=1\n```\ntrailing") == "X=1"
    assert _extract_code_block("no fence here") == "no fence here"


# --------------------------------------------------------------------------- #
# Tester.generate_and_run — stub router + sandbox
# --------------------------------------------------------------------------- #

class _Result:
    def __init__(self, text, ok=True):
        self.text, self.ok = text, ok


class _Router:
    def __init__(self, result):
        self._r = result

    async def infer(self, domain, user_text, context=None):
        return self._r


class _SB:
    def __init__(self, rc, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr, self.sandboxed = rc, stdout, stderr, True


def _tester(result, tmp_path):
    return Tester(_Router(result), code_domain="code",
                  scratch_root=str(tmp_path), project_dir=str(tmp_path))


async def test_generate_and_run_passing(monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox, "run_sandboxed", lambda *a, **k: _SB(0, "1 passed"))
    t = _tester(_Result("```python\ndef test_x():\n    assert True\n```"), tmp_path)
    out = await t.generate_and_run(goal="g", path="foo.py", code="def add(): ...")
    assert out.ran and out.passed is True
    assert "PASSED" in out.note
    assert Path(out.artifact.path).exists()        # artifact in scratch, not repo tests/


async def test_generate_and_run_failing_surfaces_traceback(monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox, "run_sandboxed",
                        lambda *a, **k: _SB(1, "E   assert add(2,2)==4"))
    t = _tester(_Result("```python\ndef test_x():\n    assert False\n```"), tmp_path)
    out = await t.generate_and_run(goal="g", path="foo.py", code="x")
    assert out.ran and out.passed is False
    assert "FAILED" in out.note and "assert add(2,2)==4" in out.note


async def test_empty_code_block_is_skipped_not_passed(tmp_path):
    t = _tester(_Result("```python\n```"), tmp_path)    # fence with no code
    out = await t.generate_and_run(goal="g", path="foo.py", code="x")
    assert out.skipped is True and out.passed is None   # never a false pass (R3.5)


async def test_generation_not_ok_is_skipped(tmp_path):
    t = _tester(_Result("", ok=False), tmp_path)
    out = await t.generate_and_run(goal="g", path="foo.py", code="x")
    assert out.skipped is True and out.ran is False


async def test_degrades_gracefully_on_exception(monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox, "run_sandboxed",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    t = _tester(_Result("```python\nassert True\n```"), tmp_path)
    out = await t.generate_and_run(goal="g", path="foo.py", code="x")
    assert out.skipped is True and out.passed is None   # never raises, never a pass


# --------------------------------------------------------------------------- #
# DevAgent safe-observation wiring (_maybe_run_tester)
# --------------------------------------------------------------------------- #

class _FakeTester:
    def __init__(self, outcome):
        self._outcome = outcome
        self.calls = 0

    async def generate_and_run(self, goal, path, code):
        self.calls += 1
        return self._outcome


def _agent_with_tester(outcome, *, skip_check=None):
    agent = DevAgent(router=MagicMock())
    agent._current_goal = "g"
    agent._read_current_for_critic = MagicMock(return_value="src code")
    agent.set_tester(_FakeTester(outcome), skip_check=skip_check)
    return agent


async def test_wiring_appends_failure_observation_without_undoing_write():
    agent = _agent_with_tester(TestOutcome(ran=True, passed=False, note="[tester] generated test FAILED:\nboom"))
    step = AgentStep(action="WRITE_FILE", args="foo.py", body="x")
    out = await agent._maybe_run_tester(step, "Written 5 bytes")
    assert out.startswith("Written 5 bytes")        # the write result is preserved
    assert "FAILED" in out                          # failure surfaced as observation


async def test_wiring_appends_pass_note():
    agent = _agent_with_tester(TestOutcome(ran=True, passed=True, note="[tester] generated test PASSED"))
    out = await agent._maybe_run_tester(AgentStep(action="WRITE_FILE", args="foo.py", body="x"), "Written")
    assert "PASSED" in out


async def test_wiring_skips_non_source_path():
    ft = _FakeTester(TestOutcome(note="should not appear"))
    agent = DevAgent(router=MagicMock())
    agent.set_tester(ft)
    out = await agent._maybe_run_tester(AgentStep(action="WRITE_FILE", args="tests/test_x.py", body="x"), "Written")
    assert out == "Written" and ft.calls == 0       # not a source file → not tested


async def test_wiring_skips_on_flare_check():
    agent = _agent_with_tester(TestOutcome(note="nope"), skip_check=lambda: True)
    out = await agent._maybe_run_tester(AgentStep(action="WRITE_FILE", args="foo.py", body="x"), "Written")
    assert out == "Written"                          # R3.6 — skipped on flare


async def test_wiring_disabled_by_default_is_noop():
    agent = DevAgent(router=MagicMock())
    assert agent._tester is None and agent._tester_enabled is False
    out = await agent._maybe_run_tester(AgentStep(action="WRITE_FILE", args="foo.py", body="x"), "Written")
    assert out == "Written"
