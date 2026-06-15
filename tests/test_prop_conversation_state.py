"""Property-based invariants for the deterministic anaphora layer.

The example tests in test_conversation_state.py sample specific phrases; these
assert the safety invariants hold for arbitrary input — the properties that make
rewriting-before-inference trustworthy:

  * with no antecedent, NOTHING is ever rewritten;
  * a "no change" result returns the input verbatim;
  * a change is only ever made when the utterance matches one of the two narrow
    anaphora patterns (so free-form dictation can never be silently rewritten);
  * the rolling buffer never exceeds max_turns.

Run: python -m pytest tests/test_prop_conversation_state.py -q
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from hypothesis import given, strategies as st

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.conversation_state import ConversationState, _REPEAT_RE, _PRONOUN_RE


def _norm(text: str) -> str:
    n = re.sub(r"[^\w\s]", " ", text.lower())
    return re.sub(r"\s+", " ", n).strip()


# Mix of free text and phrasings that exercise both the trigger and non-trigger
# branches, so the properties aren't vacuously true.
_PHRASES = st.one_of(
    st.text(),
    st.sampled_from([
        "again", "do that again", "do it again", "repeat", "once more",
        "click it", "close that", "open this", "click that one",
        "type that's great", "scroll down", "open notepad",
        "what did i just do", "again and again forever",
    ]),
)


def _seeded() -> ConversationState:
    cs = ConversationState()
    cs.record(command_text="click the save button", verb="CLICK",
              target="Save button", success=True)
    return cs


@given(text=_PHRASES)
def test_no_antecedent_never_rewrites(text):
    out, changed = ConversationState().resolve_anaphora(text)
    assert changed is False
    assert out == text


@given(text=_PHRASES)
def test_unchanged_implies_identical(text):
    out, changed = _seeded().resolve_anaphora(text)
    if not changed:
        assert out == text


@given(text=_PHRASES)
def test_any_change_is_pattern_justified(text):
    out, changed = _seeded().resolve_anaphora(text)
    if changed:
        norm = _norm(text)
        assert _REPEAT_RE.match(norm) or _PRONOUN_RE.match(norm)
        assert isinstance(out, str) and out


@given(
    verbs=st.lists(
        st.sampled_from(["CLICK", "OPEN", "CLOSE", "TYPE", "SCROLL"]),
        min_size=0, max_size=40,
    )
)
def test_buffer_never_exceeds_cap(verbs):
    cs = ConversationState(max_turns=10)
    for i, v in enumerate(verbs):
        cs.record(command_text=f"cmd {i}", verb=v, success=True)
    assert len(cs._turns) <= 10
    if verbs:
        assert cs.last.command_text == f"cmd {len(verbs) - 1}"


@given(
    verb=st.sampled_from(["CLICK", "OPEN", "CLOSE", "TYPE", "SCROLL"]),
    target=st.text(max_size=20),
    success=st.booleans(),
)
def test_prompt_hint_is_wellformed(verb, target, success):
    assert ConversationState().prompt_hint() == ""        # empty until a turn
    cs = ConversationState()
    cs.record(command_text="x", verb=verb, target=target, success=success)
    hint = cs.prompt_hint()
    assert isinstance(hint, str) and hint.startswith("Last action:")
    assert ("done" if success else "failed") in hint
