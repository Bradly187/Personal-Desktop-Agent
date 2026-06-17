"""Skill-trigger eval tests — the skill router eval is MODEL-FREE, so this exercises
the real predictor (over the real SkillRegistry.match_intent, populated from the
shipped manifests) end to end. Runs in CI instantly.

Run: python -m pytest tests/test_evals_skill_trigger.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from evals.corpus import load_suite, EvalCase
from evals.runner import run_suite, skill_trigger_predictor


def _c(u: str) -> EvalCase:
    return EvalCase(id="x", suite="t", utterance=u, expected_verb="")


def test_predictor_fires_right_skill_and_stays_quiet():
    predict = skill_trigger_predictor()
    assert predict(_c("what's the weather right now?")).verb == "weather"
    assert predict(_c("find papers about diffusion models")).verb == "arxiv"
    assert predict(_c("add a note: buy milk")).verb == "notes"
    # negative — no keyword should match anything
    assert predict(_c("click the save button")).verb == "none"
    assert predict(_c("scroll down a little")).verb == "none"


def test_adjacent_summarize_skills_do_not_cross():
    predict = skill_trigger_predictor()
    assert predict(_c("summarize my pain")).verb == "pain_journal"
    assert predict(_c("summarize my inbox")).verb == "google_pim"


def test_shipped_skill_trigger_suite_runs_at_high_accuracy():
    cases = load_suite("skill_triggers")
    assert len(cases) >= 20
    # every expected_verb is a real skill_id or the "none" sentinel
    mdir = Path(__file__).parent.parent / "skills" / "manifests"
    ids = {p.stem for p in mdir.glob("*.json")} | {"none"}
    for c in cases:
        assert c.expected_verb in ids, f"{c.id}: bad expected_verb {c.expected_verb}"
    rep = run_suite(cases, skill_trigger_predictor())
    # The whitepaper's trigger gate: >=90% accuracy on positive AND negative cases.
    assert rep.exact_acc >= 0.90, rep.summary()


def test_token_budget_under_bound():
    from evals.token_budget import measure, DEFAULT_MAX_TOKENS
    rep = measure()
    assert rep["n_skills"] >= 5
    assert rep["total_tokens"] <= DEFAULT_MAX_TOKENS, rep
