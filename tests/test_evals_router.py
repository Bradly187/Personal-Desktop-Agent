"""Router-eval tests — the DomainClassifier eval is MODEL-FREE, so this exercises
the real predictor end to end (no fakes needed) and runs in CI instantly.

Run: python -m pytest tests/test_evals_router.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from evals.corpus import load_suite
from evals.runner import run_suite, router_predictor, _register_skill_keywords_from_manifests


def test_router_predictor_classifies_core_domains():
    predict = router_predictor(register_skills=False)
    from evals.corpus import EvalCase

    def c(u):
        return EvalCase(id="x", suite="t", utterance=u, expected_verb="")

    assert predict(c("click the save button")).verb == "command"
    assert predict(c("write a pytorch training loop for a CNN")).verb == "code"


def test_router_skill_registration_enables_skill_domain():
    # Without skill keywords, a skill utterance won't classify as 'skill'.
    from core.domain_classifier import DomainClassifier
    DomainClassifier.register_skill_keywords(set())              # reset
    from evals.corpus import EvalCase
    case = EvalCase(id="x", suite="t", utterance="what's on my calendar tomorrow",
                    expected_verb="skill")
    assert router_predictor(register_skills=False)(case).verb != "skill"
    # With manifest keywords registered, it should.
    assert router_predictor(register_skills=True)(case).verb == "skill"


def test_register_skill_keywords_from_manifests_populates():
    from core.domain_classifier import DomainClassifier
    DomainClassifier.register_skill_keywords(set())
    _register_skill_keywords_from_manifests(DomainClassifier)
    assert len(DomainClassifier._SKILL_KEYWORDS) > 0


def test_shipped_router_suite_runs_at_high_accuracy():
    cases = load_suite("router_domains")
    assert len(cases) >= 12
    valid = set(__import__("core.domain_classifier", fromlist=["DomainClassifier"])
                .DomainClassifier.DOMAINS)
    for c in cases:
        assert c.expected_verb in valid, f"{c.id}: bad domain {c.expected_verb}"
    rep = run_suite(cases, router_predictor())
    # Deterministic classifier — should clear a high bar on a curated suite.
    assert rep.exact_acc >= 0.85, rep.summary()
