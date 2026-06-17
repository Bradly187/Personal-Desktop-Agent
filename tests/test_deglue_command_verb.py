"""Regression: ASR-concatenated launch verbs must still route to the command
domain.

Bug (2026-06-17): "Hey agent, open VS Code" was transcribed as the single token
"OpenVSCode". With the verb hidden inside one word the DomainClassifier saw no
leading command verb, fell through to dev-domain=general, and the dev agent
replied "Opening Visual Studio Code." as free-form text without ever emitting an
OPEN action — so VS Code never launched.

deglue_command_verb restores the missing space on a camelCase boundary so the
query classifies as `command` again.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.domain_classifier import DomainClassifier, deglue_command_verb


@pytest.mark.parametrize(
    "glued, expected",
    [
        ("OpenVSCode", "Open VSCode"),
        ("CloseSlack", "Close Slack"),
        ("FocusChrome", "Focus Chrome"),
        ("OpenVSCode now", "Open VSCode now"),  # trailing words preserved
        ("  OpenNotepad", "  Open Notepad"),    # leading whitespace preserved
    ],
)
def test_deglue_splits_leading_launch_verb(glued, expected):
    assert deglue_command_verb(glued) == expected


@pytest.mark.parametrize(
    "text",
    [
        "open VS Code",      # already spaced
        "open",              # bare standalone verb
        "opener",            # verb is a prefix but no camelCase boundary
        "screenshot",        # standalone verb, no target
        "what's on my screen",  # not a command at all
        "OpenAPI spec",      # already spaced; only first word inspected
        "",                  # empty
    ],
)
def test_deglue_is_noop_when_not_glued(text):
    assert deglue_command_verb(text) == text


def test_deglue_only_touches_first_word():
    # A glued verb later in the sentence is left alone (only word[0] is a verb).
    assert deglue_command_verb("please OpenVSCode") == "please OpenVSCode"


def test_glued_verb_misroutes_without_deglue():
    """Documents the original bug: the raw glued token is NOT command-domain."""
    clf = DomainClassifier()
    assert clf.classify("OpenVSCode") != "command"


def test_deglued_verb_routes_to_command_domain():
    """After de-gluing, the same utterance classifies as command."""
    clf = DomainClassifier()
    assert clf.classify(deglue_command_verb("OpenVSCode")) == "command"
    assert clf.classify(deglue_command_verb("CloseSlack")) == "command"
