"""Tests for the model-free self-skilling (macros) eval harness.

Mirrors tests/test_evals_plan_contract.py: verifies the suite loads, every
scripted case passes against the real MacroDetector/MacroStore, the locked
baseline holds, and the --check gate is wired.
"""
from evals import self_skilling as ss


def test_suite_loads_nonempty():
    cases = ss.load_suite()
    assert len(cases) >= 8
    assert all(c.id for c in cases)


def test_suite_is_green_exact_acc_1():
    report = ss.run_suite()
    assert report.exact_acc == 1.0, [
        (r.id, r.note) for r in report.failures
    ]


def test_baseline_locked_and_matches():
    base = ss.load_baseline()
    assert base is not None, "run `python -m evals.self_skilling --update-baseline`"
    assert base["exact_acc"] == 1.0
    assert base["n"] == ss.run_suite().n


def test_check_gate_passes_at_baseline():
    assert ss._main(["--check"]) == 0


def test_individual_cases_pass():
    for case in ss.load_suite():
        res = ss.run_case(case)
        assert res.correct, f"{case.id}: {res.note}"
