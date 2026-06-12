"""skill domain routing — DomainClassifier scores skill intents without
regressing command/dev domains.

Run:
    python -m pytest tests/test_skill_routing.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from core.domain_classifier import DomainClassifier


@pytest.fixture(autouse=True)
def _skill_kw():
    DomainClassifier.register_skill_keywords({
        "next meeting", "unread email", "what's on my calendar", "reply to",
    })
    yield
    DomainClassifier.register_skill_keywords(set())


def test_skill_utterance_classifies_skill():
    c = DomainClassifier()
    assert c.classify("read my next meeting") == "skill"
    assert c.classify("summarize my unread email") == "skill"
    assert c.classify("what's on my calendar today") == "skill"


def test_command_not_misrouted():
    c = DomainClassifier()
    assert c.classify("scroll down") == "command"
    assert c.classify("click the button") == "command"


def test_dev_domains_not_regressed():
    c = DomainClassifier()
    assert c.classify("prove that the matrix is positive definite using eigenvalues") == "math"
    assert c.classify("write a python function to train a transformer model") == "code"


def test_no_skill_keywords_no_skill_domain():
    DomainClassifier.register_skill_keywords(set())
    c = DomainClassifier()
    assert c.classify("read my next meeting") != "skill"
