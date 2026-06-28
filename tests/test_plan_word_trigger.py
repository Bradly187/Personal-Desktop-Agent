"""Explicit "plan" word trigger for the DomainClassifier (specs/cloud-plan-routing R5).

When DA_PLAN_WORD_TRIGGER is on, a literal "plan"/"plans"/"planning" token forces
the plan domain so the agentic plan_and_run loop (→ CloudPlanRouter → Sonnet 4.6)
fires reliably. Default OFF → byte-identical to the static classifier.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.domain_classifier as dc
from core.domain_classifier import DomainClassifier


# --------------------------------------------------------------------------- #
# R5: flag OFF is byte-identical (the trigger never fires)
# --------------------------------------------------------------------------- #

def test_r5_trigger_off_is_noop(monkeypatch):
    monkeypatch.setattr(dc, "_PLAN_WORD_TRIGGER", False)
    clf = DomainClassifier()
    # "plan" present but a code-heavy query still classifies as code (legacy).
    assert clf.classify("implement a plan cache in python with pytest") == "code"
    assert clf.classify("what is the deployment plan for the model") == "code"


# --------------------------------------------------------------------------- #
# R5: flag ON forces plan on any literal plan token
# --------------------------------------------------------------------------- #

def test_r5_trigger_on_forces_plan_over_code(monkeypatch):
    monkeypatch.setattr(dc, "_PLAN_WORD_TRIGGER", True)
    clf = DomainClassifier()
    assert clf.classify("implement a plan cache in python with pytest") == "plan"
    assert clf.classify("search the codebase and read the router, plan it out") == "plan"


def test_r5_trigger_on_bypasses_min_words_gate(monkeypatch):
    # Short query (< _MIN_WORDS_FOR_DEV) — dev domains normally aren't scored, but
    # the trigger appends a winning plan entry anyway.
    monkeypatch.setattr(dc, "_PLAN_WORD_TRIGGER", True)
    clf = DomainClassifier()
    assert clf.classify("plan X") == "plan"


def test_r5_trigger_matches_plan_inflections(monkeypatch):
    monkeypatch.setattr(dc, "_PLAN_WORD_TRIGGER", True)
    clf = DomainClassifier()
    assert clf.classify("planning the migration of the training loop") == "plan"
    assert clf.classify("compare the two plans for the dataset build") == "plan"


def test_r5_trigger_does_not_hijack_command_or_plain_queries(monkeypatch):
    monkeypatch.setattr(dc, "_PLAN_WORD_TRIGGER", True)
    clf = DomainClassifier()
    # No "plan" token → unchanged: command stays command, code stays code.
    assert clf.classify("click the save button") == "command"
    assert clf.classify("explain how attention works in transformers") == "code"
    # "explain" must NOT substring-match "plan" (token equality, not substring).
    assert clf.classify("explain the encoder decoder attention layer") != "plan"
