"""Context-compaction unit tests for DevAgent (Sprint C).

Covers the pure helpers (_summarize_result, _fit_history_to_budget) and the
_replan / _reflect prompt assembly: the executed-step history stays under a token
budget, every FAILED step survives, oldest successes are elided first, and
DA_COMPACT_CONTEXT=0 reproduces the prior fixed-char-cut behaviour.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from inference import dev_agent as da
from inference.dev_agent import (
    AgentStep,
    DevAgent,
    _summarize_result,
    _fit_history_to_budget,
    _est_tokens,
    _HISTORY_TOKEN_BUDGET,
)


# --------------------------------------------------------------------------- #
# _summarize_result
# --------------------------------------------------------------------------- #

def test_summarize_success_takes_first_nonempty_line():
    out = _summarize_result("\n\nfirst line\nsecond line\n", success=True)
    assert out.startswith("first line")
    assert "second line" not in out
    assert out.endswith("…")            # multiline -> ellipsis marker


def test_summarize_success_caps_long_line():
    out = _summarize_result("x" * 500, success=True, success_cap=160)
    assert len(out) <= 162 and out.endswith("…")


def test_summarize_failure_keeps_tail():
    text = "trace head\n" + ("m" * 1000) + "\nValueError: the real cause"
    out = _summarize_result(text, success=False, failure_cap=400)
    assert "ValueError: the real cause" in out      # cause is last -> kept
    assert out.startswith("… ")


def test_summarize_empty():
    assert _summarize_result("", success=True) == "(no output)"
    assert _summarize_result(None, success=False) == "(no output)"


# --------------------------------------------------------------------------- #
# _fit_history_to_budget
# --------------------------------------------------------------------------- #

def _entries(n_success: int, fail_at: list[int] | None = None):
    fail_at = fail_at or []
    out = []
    for i in range(n_success + len(fail_at)):
        is_fail = i in fail_at
        out.append((is_fail, f"  {i}. line {'X'*200}"))   # ~50 tokens each
    return out


def test_under_budget_returns_all():
    rendered = [(False, "  1. short"), (False, "  2. short")]
    assert _fit_history_to_budget(rendered, budget_tokens=1000) == \
        ["  1. short", "  2. short"]


def test_over_budget_elides_oldest_successes_and_marks():
    rendered = _entries(40)                       # ~2000 tokens, over a 300 budget
    out = _fit_history_to_budget(rendered, budget_tokens=300)
    assert any("elided" in ln for ln in out)
    # fewer lines than input, and the most-recent step survives
    assert len(out) < len(rendered)
    assert rendered[-1][1] in out


def test_failures_always_survive_budget():
    # 30 successes + a failure in the middle; tiny budget
    rendered = _entries(30, fail_at=[5])
    out = _fit_history_to_budget(rendered, budget_tokens=120)
    fail_line = rendered[5][1]
    assert fail_line in out                       # failure kept despite budget
    assert any("elided" in ln for ln in out)


def test_order_preserved():
    rendered = _entries(20)
    out = [ln for ln in _fit_history_to_budget(rendered, budget_tokens=200)
           if "elided" not in ln]
    kept_idx = [int(ln.strip().split(".")[0]) for ln in out]
    assert kept_idx == sorted(kept_idx)


def test_compaction_off_is_passthrough(monkeypatch):
    monkeypatch.setattr(da, "_COMPACT_CONTEXT", False)
    rendered = _entries(40)
    out = _fit_history_to_budget(rendered, budget_tokens=10)   # tiny budget ignored
    assert out == [line for _, line in rendered]


# --------------------------------------------------------------------------- #
# _replan / _reflect prompt assembly (stubbed router)
# --------------------------------------------------------------------------- #

class _CaptureRouter:
    def __init__(self):
        self.last_prompt = None

    async def infer(self, domain, user_text, context=None):
        self.last_prompt = user_text
        return SimpleNamespace(ok=False, text="", model="stub", error=None)


def _steps(n_ok: int, fail_last: bool = True):
    steps = [AgentStep(action="READ_FILE", args=f"f{i}.py", result="ok " + "y"*500,
                       success=True) for i in range(n_ok)]
    if fail_last:
        steps.append(AgentStep(action="RUN_TERMINAL", args="pytest",
                               result="boom\nAssertionError: expected 3 got 4",
                               success=False))
    return steps


@pytest.mark.asyncio
async def test_replan_history_within_budget_keeps_failure():
    router = _CaptureRouter()
    agent = DevAgent(router=router)
    await agent._replan("do the thing", _steps(15, fail_last=True), remaining=[])
    prompt = router.last_prompt
    assert prompt is not None
    # the failing step's error survives compaction (planner needs it to recover)
    assert "AssertionError: expected 3 got 4" in prompt
    # the executed-history portion stays within budget (+ slack for the static tail)
    assert _est_tokens(prompt) <= _HISTORY_TOKEN_BUDGET + 400


@pytest.mark.asyncio
async def test_replan_compaction_off_uses_old_format(monkeypatch):
    monkeypatch.setattr(da, "_COMPACT_CONTEXT", False)
    router = _CaptureRouter()
    agent = DevAgent(router=router)
    await agent._replan("do the thing", _steps(3, fail_last=False), remaining=[])
    # old format had no elision marker and used the raw [:300] cut
    assert "elided" not in router.last_prompt


@pytest.mark.asyncio
async def test_reflect_history_within_budget(monkeypatch):
    captured = {}

    class _R:
        async def infer(self, domain, user_text, context=None):
            captured["prompt"] = user_text
            return SimpleNamespace(ok=True, text="done", model="stub", error=None)

    agent = DevAgent(router=_R())
    out = await agent._reflect("goal", _steps(20, fail_last=True), model="stub")
    assert out == "done"
    assert "AssertionError: expected 3 got 4" in captured["prompt"]
    assert _est_tokens(captured["prompt"]) <= _HISTORY_TOKEN_BUDGET + 400
