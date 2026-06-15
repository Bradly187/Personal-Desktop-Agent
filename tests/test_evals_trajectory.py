"""Trajectory-eval logic tests — verb extraction, scoring, aggregation, gate.

Model-free (plan_fn is a fake), so these run in CI and never hang. The model-backed
path lives in evals/run.py --mode trajectory and is exercised against the live model.

Run: python -m pytest tests/test_evals_trajectory.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from evals.trajectory import (
    TrajectoryCase,
    TrajPrediction,
    extract_plan_verbs,
    score_trajectory,
    aggregate_traj,
    load_trajectory_suite,
)
from evals.runner import run_trajectory_suite
from evals.scoring import check_regression


# --------------------------------------------------------------------------- #
# extract_plan_verbs (mirrors dev_agent._parse_plan / _parse_plan_json)
# --------------------------------------------------------------------------- #

def test_extract_from_json_steps():
    raw = '{"steps": [{"action": "READ_FILE", "args": "x.py"}, {"action": "WRITE_FILE"}]}'
    assert extract_plan_verbs(raw) == ["READ_FILE", "WRITE_FILE"]


def test_extract_from_bare_json_list():
    raw = '[{"action": "GREP"}, {"action": "EXPLAIN"}]'
    assert extract_plan_verbs(raw) == ["GREP", "EXPLAIN"]


def test_extract_json_drops_unknown_verbs():
    raw = '{"steps": [{"action": "WRITE_FILE"}, {"action": "FLY_TO_MOON"}]}'
    assert extract_plan_verbs(raw) == ["WRITE_FILE"]


def test_extract_from_free_text_numbered():
    raw = ("1. READ_FILE parser.py\n"
           "2. WRITE_FILE parser.py (after: 1)\n"
           "3. RUN_TERMINAL pytest -q (after: 2)")
    assert extract_plan_verbs(raw) == ["READ_FILE", "WRITE_FILE", "RUN_TERMINAL"]


def test_extract_free_text_bracket_style():
    raw = "[GIT_STATUS]\n[GIT_DIFF]\n[GIT_COMMIT] fix the bug"
    assert extract_plan_verbs(raw) == ["GIT_STATUS", "GIT_DIFF", "GIT_COMMIT"]


def test_extract_empty():
    assert extract_plan_verbs("") == []
    assert extract_plan_verbs("just some prose with no verbs") == []


# --------------------------------------------------------------------------- #
# case normalization
# --------------------------------------------------------------------------- #

def _case(**kw) -> TrajectoryCase:
    base = dict(id="t1", suite="t", goal="do x", expected_verbs=["WRITE_FILE"])
    base.update(kw)
    return TrajectoryCase.from_dict(base)


def test_case_uppercases_and_defaults_required():
    c = _case(expected_verbs=["write_file", "run_terminal"])
    assert c.expected_verbs == ["WRITE_FILE", "RUN_TERMINAL"]
    assert c.required == ["WRITE_FILE", "RUN_TERMINAL"]   # defaults to expected


def test_case_explicit_required_and_forbidden_uppercased():
    c = _case(expected_verbs=["read_file", "explain"],
              required=["explain"], forbidden=["write_file"])
    assert c.required == ["EXPLAIN"]
    assert c.forbidden == ["WRITE_FILE"]


# --------------------------------------------------------------------------- #
# scoring
# --------------------------------------------------------------------------- #

def test_score_exact_pass():
    c = _case(expected_verbs=["WRITE_FILE", "RUN_TERMINAL"],
              precedence=[["WRITE_FILE", "RUN_TERMINAL"]])
    r = score_trajectory(c, TrajPrediction(verbs=["READ_FILE", "WRITE_FILE", "RUN_TERMINAL"]))
    assert r.required_ok and r.order_ok and r.safe_ok and r.exact
    assert r.score == pytest.approx(1.0)


def test_score_missing_required_fails():
    c = _case(expected_verbs=["WRITE_FILE", "RUN_TERMINAL"])
    r = score_trajectory(c, TrajPrediction(verbs=["WRITE_FILE"]))  # no RUN_TERMINAL
    assert not r.required_ok and not r.exact
    assert "missing" in r.detail


def test_score_precedence_violation_fails():
    c = _case(expected_verbs=["WRITE_FILE", "GIT_COMMIT"],
              precedence=[["WRITE_FILE", "GIT_COMMIT"]])
    # commit BEFORE write — order violation
    r = score_trajectory(c, TrajPrediction(verbs=["GIT_COMMIT", "WRITE_FILE"]))
    assert r.required_ok and not r.order_ok and not r.exact
    assert "order" in r.detail


def test_score_forbidden_verb_fails_safety():
    c = _case(goal="explain x", expected_verbs=["EXPLAIN"], required=["EXPLAIN"],
              forbidden=["WRITE_FILE", "RUN_TERMINAL"])
    r = score_trajectory(c, TrajPrediction(verbs=["READ_FILE", "WRITE_FILE", "EXPLAIN"]))
    assert not r.safe_ok and not r.exact
    assert "forbidden" in r.detail


def test_score_min_coverage_relaxation():
    c = _case(expected_verbs=["GREP", "READ_FILE", "EXPLAIN"], min_coverage=0.5)
    r = score_trajectory(c, TrajPrediction(verbs=["READ_FILE", "EXPLAIN"]))  # 2/3
    assert r.coverage == pytest.approx(2 / 3)
    assert r.required_ok and r.exact          # 2/3 >= 0.5


def test_score_precedence_vacuous_when_endpoint_absent():
    c = _case(expected_verbs=["WRITE_FILE"], required=["WRITE_FILE"],
              precedence=[["WRITE_FILE", "GIT_COMMIT"]])  # commit never appears
    r = score_trajectory(c, TrajPrediction(verbs=["WRITE_FILE"]))
    assert r.order_ok and r.exact             # pair vacuously satisfied


def test_score_error_prediction():
    r = score_trajectory(_case(), TrajPrediction(error="backend down"))
    assert not r.exact and r.error == "backend down" and r.score == 0.0


# --------------------------------------------------------------------------- #
# aggregate + gate + runner
# --------------------------------------------------------------------------- #

def test_aggregate_and_safe_acc():
    c_safe = _case(id="a", goal="explain", expected_verbs=["EXPLAIN"],
                   required=["EXPLAIN"], forbidden=["WRITE_FILE"])
    c_req = _case(id="b", expected_verbs=["WRITE_FILE", "RUN_TERMINAL"])
    results = [
        score_trajectory(c_safe, TrajPrediction(verbs=["EXPLAIN"])),                 # exact
        score_trajectory(c_safe, TrajPrediction(verbs=["WRITE_FILE", "EXPLAIN"])),   # unsafe
        score_trajectory(c_req, TrajPrediction(verbs=["WRITE_FILE"])),              # missing
    ]
    rep = aggregate_traj(results)
    assert rep.n == 3
    assert rep.exact_acc == pytest.approx(1 / 3)
    assert rep.safe_acc == pytest.approx(2 / 3)   # only the 2nd is unsafe
    assert len(rep.failures) == 2
    assert "exact_acc" in rep.metrics()


def test_run_trajectory_suite_with_fake_and_gate():
    cases = [_case(id="1", expected_verbs=["WRITE_FILE"]),
             _case(id="2", expected_verbs=["GIT_COMMIT"])]

    def perfect(case):
        return TrajPrediction(verbs=list(case.expected_verbs))

    rep = run_trajectory_suite(cases, perfect)
    assert rep.exact_acc == 1.0
    ok, _ = check_regression(rep, {"exact_acc": 1.0, "tolerance": 0.05})
    assert ok


def test_run_trajectory_suite_captures_exception():
    def boom(case):
        raise RuntimeError("nope")

    rep = run_trajectory_suite([_case()], boom)
    assert rep.errors == 1 and rep.n == 1


# --------------------------------------------------------------------------- #
# shipped suite is well-formed
# --------------------------------------------------------------------------- #

def test_shipped_trajectory_suite_valid():
    from evals.trajectory import _PLAN_ACTIONS
    cases = load_trajectory_suite("dev_trajectory")
    assert len(cases) >= 10
    for c in cases:
        assert c.goal
        for v in c.required + c.forbidden + c.expected_verbs:
            assert v in _PLAN_ACTIONS, f"{c.id}: unknown verb {v}"
        for a, b in c.precedence:
            assert a in _PLAN_ACTIONS and b in _PLAN_ACTIONS
