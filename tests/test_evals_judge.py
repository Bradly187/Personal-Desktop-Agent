"""LM-judge eval logic tests — verdict parsing, scoring, aggregation, runner.

Model-free (the judge and producer are fakes), so these run in CI and never hang.
The model-backed path lives in evals/run.py --mode judge.

Run: python -m pytest tests/test_evals_judge.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from evals.judge import (
    Criterion,
    JudgeCase,
    Verdict,
    build_judge_prompt,
    parse_verdict,
    score_judge,
    aggregate_judge,
    load_judge_suite,
)
from evals.runner import run_judge_suite


def _case(**kw) -> JudgeCase:
    base = dict(
        id="j1", suite="t", prompt="what is 2+2?",
        criteria=[{"name": "correctness", "description": "says 4"}],
        reference="4", pass_threshold=0.7,
    )
    base.update(kw)
    return JudgeCase.from_dict(base)


# --------------------------------------------------------------------------- #
# from_dict / criteria coercion
# --------------------------------------------------------------------------- #

def test_case_criteria_coerced_to_objects():
    c = _case()
    assert isinstance(c.criteria[0], Criterion)
    assert c.criteria[0].name == "correctness"


# --------------------------------------------------------------------------- #
# parse_verdict
# --------------------------------------------------------------------------- #

def test_parse_clean_json():
    v = parse_verdict('{"scores": {"correctness": 1.0}, "overall": 0.9, "rationale": "good"}',
                      _case())
    assert v.overall == pytest.approx(0.9) and v.passed and not v.error
    assert v.rationale == "good"


def test_parse_json_embedded_in_prose():
    raw = 'Sure, here is my verdict:\n{"scores": {"correctness": 0.8}, "overall": 0.8}\nThanks!'
    v = parse_verdict(raw, _case())
    assert v.overall == pytest.approx(0.8) and v.passed


def test_parse_derives_overall_as_weighted_mean():
    c = _case(criteria=[{"name": "a", "weight": 3.0}, {"name": "b", "weight": 1.0}])
    v = parse_verdict('{"scores": {"a": 1.0, "b": 0.0}}', c)   # no overall
    assert v.overall == pytest.approx(0.75)                    # (3*1 + 1*0)/4


def test_parse_normalizes_1_to_5_scale():
    v = parse_verdict('{"scores": {"correctness": 5}, "overall": 4}', _case())
    assert v.overall == pytest.approx(0.8)                     # 4/5
    assert v.scores["correctness"] == pytest.approx(1.0)       # 5/5


def test_parse_garbage_is_error_not_pass():
    v = parse_verdict("the answer was pretty good i think", _case())
    assert not v.passed and v.error


def test_parse_empty_scores_is_error():
    v = parse_verdict('{"rationale": "meh"}', _case())
    assert not v.passed and v.error


def test_parse_below_threshold_fails():
    v = parse_verdict('{"overall": 0.5}', _case(pass_threshold=0.7))
    assert not v.passed and not v.error


# --------------------------------------------------------------------------- #
# scoring + aggregation
# --------------------------------------------------------------------------- #

def test_score_error_verdict_never_passes():
    r = score_judge(_case(), Verdict(overall=0.9, passed=True, error="boom"))
    assert not r.passed and r.error == "boom"


def test_aggregate_pass_rate_and_by_criterion():
    c = _case()
    results = [
        score_judge(c, Verdict(scores={"correctness": 1.0}, overall=0.9, passed=True)),
        score_judge(c, Verdict(scores={"correctness": 0.4}, overall=0.4, passed=False)),
    ]
    rep = aggregate_judge(results)
    assert rep.n == 2
    assert rep.pass_rate == pytest.approx(0.5)
    assert rep.exact_acc == pytest.approx(0.5)            # alias for the shared gate
    assert rep.by_criterion["correctness"] == pytest.approx(0.7)
    assert "exact_acc" in rep.metrics()


def test_aggregate_empty():
    rep = aggregate_judge([])
    assert rep.n == 0 and rep.pass_rate == 0.0


# --------------------------------------------------------------------------- #
# build_judge_prompt
# --------------------------------------------------------------------------- #

def test_build_prompt_includes_rubric_output_and_reference():
    c = _case(criteria=[{"name": "grounding", "description": "no made-up facts"}],
              reference="must say 4")
    system, user = build_judge_prompt(c, "the answer is 4")
    assert "JSON" in system
    assert "grounding" in user and "no made-up facts" in user
    assert "the answer is 4" in user
    assert "must say 4" in user


# --------------------------------------------------------------------------- #
# run_judge_suite (fake producer + fake judge)
# --------------------------------------------------------------------------- #

def test_run_judge_suite_with_fakes():
    cases = [_case(id="1"), _case(id="2")]

    def producer(case):
        return "4"

    def judge(case, output):
        return Verdict(scores={"correctness": 1.0}, overall=1.0, passed=True)

    rep = run_judge_suite(cases, judge, produce_fn=producer)
    assert rep.pass_rate == 1.0 and rep.errors == 0


def test_run_judge_uses_prerecorded_output_fixture():
    # A case carrying `output` is a fixture for the judge — no producer call.
    c = _case(id="fx", output="4")
    seen = {}

    def judge(case, output):
        seen["output"] = output
        return Verdict(overall=0.9, passed=True)

    rep = run_judge_suite([c], judge, produce_fn=None)
    assert seen["output"] == "4" and rep.pass_rate == 1.0


def test_run_judge_producer_exception_is_error():
    def producer(case):
        raise RuntimeError("model down")

    def judge(case, output):  # never reached
        return Verdict(overall=1.0, passed=True)

    rep = run_judge_suite([_case()], judge, produce_fn=producer)
    assert rep.errors == 1 and rep.pass_rate == 0.0


def test_run_judge_no_output_is_error():
    rep = run_judge_suite([_case(output="")], lambda c, o: Verdict(passed=True),
                          produce_fn=None)
    assert rep.errors == 1


# --------------------------------------------------------------------------- #
# shipped suite is well-formed
# --------------------------------------------------------------------------- #

def test_shipped_judge_suite_valid():
    cases = load_judge_suite("explain_quality")
    assert len(cases) >= 5
    for c in cases:
        assert c.prompt and c.criteria
        assert all(isinstance(cr, Criterion) and cr.name for cr in c.criteria)
        assert 0.0 < c.pass_threshold <= 1.0
    # the hallucination probe must be present — it's the headline judge case
    assert any(any("halluc" in t for t in c.tags) for c in cases)
