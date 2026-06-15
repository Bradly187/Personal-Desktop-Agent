"""RAG ablation eval — model-free logic tests (grounding score, scoring, runner).

The real --mode ablation runs the live DevAgent with vs without the indexer; here
we stub the agents so the harness logic is covered in CI without a GPU.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.ablation import (
    AblationCase,
    grounding_score,
    score_ablation,
    aggregate_ablation,
    load_ablation_suite,
)
from evals.runner import run_ablation_suite


# --------------------------------------------------------------------------- #
# grounding_score
# --------------------------------------------------------------------------- #

def test_grounding_score_recall_and_case_insensitive():
    score, hits = grounding_score("The AccessibilityScheduler uses a Semaphore.",
                                  ["AccessibilityScheduler", "semaphore", "fan_out"])
    assert hits == 2 and score == pytest.approx(2 / 3)


def test_grounding_score_empty_anchors():
    assert grounding_score("anything", []) == (0.0, 0)


def test_grounding_score_no_hits():
    assert grounding_score("generic prose about scheduling", ["bubblewrap"]) == (0.0, 1 * 0)


# --------------------------------------------------------------------------- #
# score_ablation
# --------------------------------------------------------------------------- #

def _case(anchors):
    return AblationCase(id="c", suite="s", goal="g", anchor_terms=anchors)


def test_score_ablation_positive_delta_when_rag_grounds():
    c = _case(["bubblewrap", "firejail", "unshare"])
    r = score_ablation(c, with_text="uses bubblewrap and firejail with unshare",
                       no_text="it sandboxes commands somehow")
    assert r.with_score == 1.0 and r.no_score == 0.0 and r.delta == 1.0


def test_score_ablation_flags_error_sentinel():
    c = _case(["x"])
    r = score_ablation(c, with_text="__ERROR__ TimeoutError: ", no_text="x present")
    assert r.error.startswith("with:")


# --------------------------------------------------------------------------- #
# aggregate_ablation
# --------------------------------------------------------------------------- #

def test_aggregate_means_and_counts():
    c1, c2 = _case(["a", "b"]), _case(["c", "d"])
    results = [
        score_ablation(c1, "a b", "a"),      # with=1.0 no=0.5 -> +0.5 improved
        score_ablation(c2, "c", "c d"),      # with=0.5 no=1.0 -> -0.5 regressed
    ]
    rep = aggregate_ablation(results)
    assert rep.n == 2
    assert rep.mean_with == pytest.approx(0.75)
    assert rep.mean_no == pytest.approx(0.75)
    assert rep.mean_delta == pytest.approx(0.0)
    assert rep.improved == 1 and rep.regressed == 1
    assert "exact_acc" in rep.metrics()


def test_aggregate_excludes_errored_from_means():
    c = _case(["a"])
    results = [score_ablation(c, "a", "a"),                       # delta 0, valid
               score_ablation(c, "__ERROR__ boom", "a")]          # errored
    rep = aggregate_ablation(results)
    assert rep.errors == 1 and rep.n == 2
    assert rep.mean_with == pytest.approx(1.0)   # errored case excluded


# --------------------------------------------------------------------------- #
# run_ablation_suite (stub DevAgents)
# --------------------------------------------------------------------------- #

class _Step:
    def __init__(self, result):
        self.action = "EXPLAIN"
        self.result = result


class _Res:
    def __init__(self, text):
        self.steps = [_Step(text)]
        self.response_text = ""
        self.error = None
        self.success = True


class _FakeAgent:
    """Returns scripted text per goal (with-RAG = grounded, no-RAG = generic)."""
    def __init__(self, scripted):
        self._scripted = scripted
        self._context: list = []
        self._approve_plan_upfront = None

    async def plan_and_run(self, goal):
        return _Res(self._scripted.get(goal, ""))


def test_run_ablation_suite_measures_positive_delta():
    cases = [AblationCase(id="ab1", suite="s", goal="explain sandbox",
                          anchor_terms=["bubblewrap", "firejail", "unshare"])]
    grounded = {"explain sandbox": "uses bubblewrap, firejail, unshare jail"}
    generic = {"explain sandbox": "it runs commands in some isolated way"}
    rep = run_ablation_suite(
        cases,
        build_no_rag=lambda: _FakeAgent(generic),
        build_with_rag=lambda: _FakeAgent(grounded),
        timeout_s=5)
    assert rep.n == 1 and rep.errors == 0
    assert rep.mean_with == 1.0 and rep.mean_no == 0.0
    assert rep.mean_delta == 1.0 and rep.improved == 1


def test_run_ablation_installs_auto_approve():
    agent = _FakeAgent({"g": "x"})
    run_ablation_suite([AblationCase(id="c", suite="s", goal="g", anchor_terms=["x"])],
                       build_no_rag=lambda: _FakeAgent({"g": "x"}),
                       build_with_rag=lambda: agent, timeout_s=5)
    assert agent._approve_plan_upfront is not None


# --------------------------------------------------------------------------- #
# shipped suite
# --------------------------------------------------------------------------- #

def test_shipped_ablation_suite_valid():
    cases = load_ablation_suite("rag_ablation")
    assert len(cases) >= 4
    for c in cases:
        assert c.goal and len(c.anchor_terms) >= 3   # need real discriminators
