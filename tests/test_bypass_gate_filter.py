"""Bypass-gate sanity filter — the wake-armed and awaiting-clarification paths
accept an utterance WITHOUT a wake phrase, so a Whisper misrecognition could pose
as a command there. These tests pin the filter that rejects interrogatives and
text sharing no token with the command vocabulary.

Pure/stdlib (no model, no audio): exercises bypass_reject_reason directly.

Run: python -m pytest tests/test_bypass_gate_filter.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from sensors.whisper_stream import bypass_reject_reason


# --- the reported failure must now be dropped ------------------------------- #

def test_reported_misrecognition_is_rejected():
    # "agent compose note" was misheard as this question and executed.
    assert bypass_reject_reason("Are you taking us now?") is not None


@pytest.mark.parametrize("text", [
    "Are you taking us now?",          # trailing ? + question opener
    "are you taking us now",           # opener, no punctuation (Whisper omits it)
    "what time is it",                 # question word
    "how was your day",
    "is this thing on",
])
def test_questions_rejected(text):
    assert bypass_reject_reason(text) is not None


@pytest.mark.parametrize("text", [
    "taking us somewhere nice",        # no question opener, no command/function token
    "raindrops keep falling everywhere",
    "nobody knows anymore",
])
def test_no_vocab_overlap_rejected(text):
    # The coarse secondary filter: text sharing no token with the command surface.
    # (Statements containing function words like "the" intentionally pass — the
    # interrogative check above is the precise guard.)
    assert bypass_reject_reason(text) == "no overlap with command vocabulary"


def test_empty_rejected():
    assert bypass_reject_reason("") == "empty"
    assert bypass_reject_reason("   ") == "empty"


# --- real commands / answers must still pass -------------------------------- #

@pytest.mark.parametrize("text", [
    "compose a note buy milk tomorrow",   # the real phrase
    "compose note call the doctor",
    "scroll down",
    "click the save button",
    "open kiro",
    "take a note about the meeting",
    "what's my next meeting",             # question word but 'meeting' is a command noun
    "log my pain it's an eight",
])
def test_real_commands_pass(text):
    assert bypass_reject_reason(text) is None, text


@pytest.mark.parametrize("text", ["yes", "no", "cancel", "the first one", "second", "approve"])
def test_short_clarification_answers_pass(text):
    assert bypass_reject_reason(text) is None, text


# --- live hotwords extend the vocabulary ------------------------------------ #

def test_extra_vocab_admits_otherwise_unknown_token():
    # A brand/app name with no base-vocab word is rejected by default...
    assert bypass_reject_reason("zotero") is not None
    # ...but accepted once it's a live hotword.
    assert bypass_reject_reason("zotero", {"zotero"}) is None


def test_question_opener_beats_vocab_overlap():
    # Even with a command noun present, a clear interrogative is still dropped.
    assert bypass_reject_reason("are you going to open the file?") is not None


# --- the instance method folds in the stream's own hotwords ----------------- #

def test_instance_method_uses_live_hotwords():
    from sensors.whisper_stream import WhisperStream
    ws = WhisperStream.__new__(WhisperStream)        # skip heavy __init__
    ws._static_hotwords = ["Kiro", "compose a note"]
    ws._hotwords = ["zotero"]
    assert ws._bypass_reject_reason("zotero please") is None      # live hotword
    assert ws._bypass_reject_reason("are you taking us now?") is not None
