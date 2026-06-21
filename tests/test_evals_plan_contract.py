"""CI-safe tests for the model-free plan-contract eval (evals/plan_contract.py).

The eval drives the REAL DevAgent._acquire_plan_steps with scripted planner
responses — no GPU — so it runs in the normal pytest collection (the harness's
model-free gate, like test_evals_edit_ab).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from evals.plan_contract import (
    PlanContractCase,
    load_suite,
    run_case,
    run_suite,
    load_baseline,
)


def test_suite_loads_and_every_case_passes():
    cases = load_suite()
    assert len(cases) >= 6
    report = run_suite()
    assert report.n == len(cases)
    assert report.exact_acc == 1.0, [f"{r.id}: {r.note}" for r in report.failures]


def test_recover_unknown_verb_fires_one_reprompt():
    case = PlanContractCase(
        id="t",
        responses=[
            '{"steps":[{"action":"WRITE_FILE","args":"a.py"},{"action":"NOPE"}]}',
            '{"steps":[{"action":"WRITE_FILE","args":"a.py"},{"action":"RUN_TERMINAL","args":"pytest"}]}',
        ],
        repair=True, max_repairs=1,
        expect_actions=["WRITE_FILE", "RUN_TERMINAL"], expect_calls=1,
    )
    r = run_case(case)
    assert r.correct
    assert r.actions == ["WRITE_FILE", "RUN_TERMINAL"]
    assert r.calls == 1


def test_unrecoverable_fails_safe_to_empty_and_is_bounded():
    bad = '{"steps":[{"action":"NOPE"}]}'
    case = PlanContractCase(
        id="t", responses=[bad, bad, bad], repair=True, max_repairs=2,
        expect_actions=[], expect_calls=2,
    )
    r = run_case(case)
    assert r.correct
    assert r.actions == []        # fail-safe: nothing executes
    assert r.calls == 2           # exactly max re-prompts, then stop


def test_disabled_does_not_reprompt():
    case = PlanContractCase(
        id="t",
        responses=['{"steps":[{"action":"WRITE_FILE","args":"a.py"},{"action":"NOPE"}]}'],
        repair=False, max_repairs=1,
        expect_actions=["WRITE_FILE"], expect_calls=0,
    )
    r = run_case(case)
    assert r.correct and r.calls == 0


def test_scorer_flags_a_mismatch():
    # Wrong expectation → the case scores incorrect with a diagnostic note.
    case = PlanContractCase(
        id="t", responses=['{"steps":[{"action":"WRITE_FILE","args":"a.py"}]}'],
        repair=True, max_repairs=1,
        expect_actions=["RUN_TERMINAL"], expect_calls=0,   # deliberately wrong
    )
    r = run_case(case)
    assert r.correct is False
    assert "actions" in r.note


def test_baseline_is_locked_at_full_accuracy():
    base = load_baseline()
    assert base is not None, "run `python -m evals.plan_contract --update-baseline`"
    assert base["exact_acc"] == 1.0
    assert base["mode"] == "plan_contract"
