"""GAP-5 — intent_satisfaction judge suite loads and runs through the harness.

Model-free: a fake producer + fake judge exercise the wiring (the live model path
is `python -m evals.run --suite intent_satisfaction --mode judge`). Verifies every
case carries a session prefix (context) and acceptance criteria, and that the
suite scores through run_judge_suite exactly like explain_quality.

Run:
    python -m pytest tests/test_intent_satisfaction_suite.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from evals.judge import load_judge_suite, Verdict
from evals.runner import run_judge_suite


def _cases():
    return load_judge_suite("intent_satisfaction")


def test_suite_loads_with_enough_cases():
    cases = _cases()
    assert len(cases) >= 10
    assert all(c.suite == "intent_satisfaction" for c in cases)


def test_every_case_has_prefix_and_criteria():
    for c in _cases():
        assert c.context, f"{c.id} missing session-prefix context"
        assert 1 <= len(c.criteria) <= 5, f"{c.id} criteria count off"
        # a no_scope_creep / scope-discipline criterion appears across the suite
    names = {cr.name for c in _cases() for cr in c.criteria}
    assert {"intent_match"} & names


def test_runs_through_judge_harness_with_fakes():
    cases = _cases()

    def fake_producer(case):
        return f"answer to {case.id}"

    def fake_judge(case, output):
        # pass everything: a flat 1.0 on each criterion
        return Verdict(scores={cr.name: 1.0 for cr in case.criteria},
                       overall=1.0, passed=True, rationale="ok")

    report = run_judge_suite(cases, fake_judge, produce_fn=fake_producer)
    assert report.n == len(cases)
    assert report.errors == 0
    assert report.pass_rate == 1.0
