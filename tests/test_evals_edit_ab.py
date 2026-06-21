"""Pure-logic tests for the edit-format A/B harness (evals/edit_ab.py).

These never touch a model — they verify the deterministic scorer that turns an
``ArmOutcome`` (built by the model-backed runner) into per-arm correct/elision
signal, plus the suite loader and fixture integrity. The model-backed run lives
in ``evals.run --mode edit_ab`` and is exercised by hand / Tier-2, never in CI.
"""

from __future__ import annotations

import ast

import pytest

from evals.edit_ab import (
    ArmOutcome,
    EditABCase,
    aggregate_edit_ab,
    load_edit_ab_suite,
    score_arm,
    score_edit_ab,
)
from inference.edit_format import HASHLINE, WHOLE_FILE


def _case(**over) -> EditABCase:
    base = dict(
        id="t", suite="edit_format", fixture="calculator.py",
        instruction="add divide",
        expect_contains=["def divide"], expect_absent=["BUG"],
        preserve=["def add", "def subtract"], tags=[],
    )
    base.update(over)
    return EditABCase.from_dict(base)


# --------------------------- score_arm --------------------------------------- #

def test_score_arm_correct_when_all_checks_pass():
    case = _case()
    text = "def add():\n    pass\ndef subtract():\n    pass\ndef divide():\n    pass\n"
    s = score_arm(case, ArmOutcome(arm=WHOLE_FILE, ok=True, text=text))
    assert s.applied and s.lint_ok and s.intent_ok and s.preserved and s.correct


def test_score_arm_elision_when_preserve_dropped():
    case = _case()
    # divide added but subtract dropped -> intent ok, preserved False -> elision
    text = "def add():\n    pass\ndef divide():\n    pass\n"
    s = score_arm(case, ArmOutcome(arm=WHOLE_FILE, ok=True, text=text))
    assert s.intent_ok and not s.preserved and not s.correct
    assert "ELISION" in s.note


def test_score_arm_intent_miss_when_forbidden_present():
    case = _case(expect_absent=["def add"])
    text = "def add():\n    pass\ndef subtract():\n    pass\ndef divide():\n    pass\n"
    s = score_arm(case, ArmOutcome(arm=WHOLE_FILE, ok=True, text=text))
    assert not s.intent_ok and not s.correct


def test_score_arm_lint_rejected_is_applied_but_not_correct():
    case = _case()
    s = score_arm(case, ArmOutcome(arm=WHOLE_FILE, ok=False, reason="syntax",
                                   message="SyntaxError"))
    assert s.applied and not s.lint_ok and not s.correct


def test_score_arm_apply_error_not_applied():
    case = _case()
    s = score_arm(case, ArmOutcome(arm=HASHLINE, ok=False, reason="mismatch",
                                   message="stale anchor"))
    assert not s.applied and not s.lint_ok and not s.correct


# --------------------------- score_edit_ab / aggregate ----------------------- #

def _good_text():
    return ("def add():\n    pass\ndef subtract():\n    pass\n"
            "def divide():\n    pass\n")


def test_delta_positive_when_hashline_beats_whole_file():
    case = _case()
    outcomes = {
        WHOLE_FILE: ArmOutcome(arm=WHOLE_FILE, ok=True,
                               text="def add():\n    pass\ndef divide():\n    pass\n"),  # dropped subtract
        HASHLINE: ArmOutcome(arm=HASHLINE, ok=True, text=_good_text()),
    }
    r = score_edit_ab(case, outcomes)
    assert r.arms[HASHLINE].correct and not r.arms[WHOLE_FILE].correct
    assert r.delta == 1.0


def test_all_backend_errors_marks_case_errored():
    case = _case()
    outcomes = {
        WHOLE_FILE: ArmOutcome(arm=WHOLE_FILE, ok=False, reason="io", message="down"),
        HASHLINE: ArmOutcome(arm=HASHLINE, ok=False, reason="io", message="down"),
    }
    r = score_edit_ab(case, outcomes)
    assert r.error


def test_aggregate_rates_counts_and_elision():
    case = _case()
    r1 = score_edit_ab(case, {
        WHOLE_FILE: ArmOutcome(arm=WHOLE_FILE, ok=True, text=_good_text()),
        HASHLINE: ArmOutcome(arm=HASHLINE, ok=True, text=_good_text()),
    })
    r2 = score_edit_ab(case, {
        WHOLE_FILE: ArmOutcome(arm=WHOLE_FILE, ok=True,
                               text="def add():\n    pass\ndef divide():\n    pass\n"),  # elision
        HASHLINE: ArmOutcome(arm=HASHLINE, ok=True, text=_good_text()),
    })
    rep = aggregate_edit_ab([r1, r2])
    assert rep.n == 2
    assert rep.rates[HASHLINE] == 1.0
    assert rep.rates[WHOLE_FILE] == 0.5
    assert rep.mean_delta == 0.5
    assert rep.counts[WHOLE_FILE]["elision"] == 1
    m = rep.metrics()
    assert m["whole_file_rate"] == 0.5 and m["hashline_rate"] == 1.0
    assert m["exact_acc"] == 0.5  # gated metric = legacy whole_file rate


def test_empty_aggregate_is_safe():
    rep = aggregate_edit_ab([])
    assert rep.n == 0 and rep.exact_acc == 0.0


# --------------------------- suite + fixtures -------------------------------- #

@pytest.mark.parametrize("suite", ["edit_format", "edit_format_hard"])
def test_suite_loads_and_fixtures_exist_and_are_valid_python(suite):
    cases = load_edit_ab_suite(suite)
    assert cases, f"{suite} suite should have cases"
    seen = set()
    for c in cases:
        assert c.id not in seen, f"duplicate case id {c.id}"
        seen.add(c.id)
        assert c.instruction and c.expect_contains and c.preserve
        text = c.base_text()                 # raises if fixture missing
        ast.parse(text)                       # every fixture is valid Python
        # the seeded checks must be coherent with the base file:
        for snip in c.preserve:
            assert snip in text, f"{c.id}: preserve {snip!r} not in base fixture"
        for snip in c.expect_absent:
            assert snip in text, f"{c.id}: expect_absent {snip!r} should start present"
