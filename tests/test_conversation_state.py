"""Tests for conversational continuity (core/conversation_state.py) and its
wiring into HybridCoordinator (anaphora rewrite before inference + turn recording).

Run: pytest tests/test_conversation_state.py
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.conversation_state import ConversationState, Turn
from core.command_executor import Command


# --------------------------------------------------------------------------- #
# ConversationState unit tests
# --------------------------------------------------------------------------- #

def _click(cs: ConversationState, target="Save button", success=True, text=None):
    cs.record(
        command_text=text or f"click the {target.lower()}",
        verb="CLICK",
        target=target,
        coords=(812, 440),
        success=success,
    )


def test_record_and_last():
    cs = ConversationState()
    assert cs.last is None
    _click(cs)
    assert cs.last is not None
    assert cs.last.verb == "CLICK"
    assert cs.last.target == "Save button"
    assert cs.last.success is True


def test_max_turns_cap():
    cs = ConversationState(max_turns=3)
    for i in range(5):
        cs.record(command_text=f"cmd {i}", verb="TYPE", target="", success=True)
    assert len(cs._turns) == 3
    assert cs._turns[0].command_text == "cmd 2"  # oldest two evicted


def test_verb_is_uppercased():
    cs = ConversationState()
    cs.record(command_text="x", verb="click", success=True)
    assert cs.last.verb == "CLICK"


def test_last_actionable_skips_clarify_and_failures():
    cs = ConversationState()
    cs.record(command_text="click save", verb="CLICK", target="Save", success=True)
    cs.record(command_text="what?", verb="CLARIFY", target="huh", success=True)
    cs.record(command_text="click cancel", verb="CLICK", target="Cancel", success=False)
    # CLARIFY (not re-issuable) and the failed CLICK are both skipped.
    actionable = cs.last_actionable()
    assert actionable is not None
    assert actionable.target == "Save"


# -- anaphora resolution ---------------------------------------------------- #

@pytest.mark.parametrize("phrase", [
    "do that again", "do it again", "again", "again please",
    "repeat", "repeat that", "same again", "do the same", "once more",
])
def test_resolve_again_reissues_previous(phrase):
    cs = ConversationState()
    _click(cs, text="click the save button")
    out, changed = cs.resolve_anaphora(phrase)
    assert changed is True
    assert out == "click the save button"


@pytest.mark.parametrize("phrase,expected", [
    ("click it", "click Save button"),
    ("close that", "close Save button"),
    ("open this", "open Save button"),
    ("click that one", "click Save button"),
])
def test_resolve_pronoun_substitutes_target(phrase, expected):
    cs = ConversationState()
    _click(cs, target="Save button")
    out, changed = cs.resolve_anaphora(phrase)
    assert changed is True
    assert out == expected


def test_pronoun_preserves_verb_casing():
    cs = ConversationState()
    _click(cs, target="Save button")
    out, changed = cs.resolve_anaphora("Click it")
    assert changed is True
    assert out == "Click Save button"


def test_no_antecedent_leaves_text_untouched():
    cs = ConversationState()
    out, changed = cs.resolve_anaphora("do that again")
    assert changed is False
    assert out == "do that again"


def test_pronoun_without_target_unchanged():
    cs = ConversationState()
    cs.record(command_text="close", verb="CLOSE", target="", success=True)
    out, changed = cs.resolve_anaphora("close it")
    assert changed is False
    assert out == "close it"


@pytest.mark.parametrize("phrase", [
    "type that's great",          # dictation that merely contains "that"
    "scroll down again to find it",  # "again" mid-sentence, not a bare repeat
    "click the submit button",    # ordinary command
    "open notepad",
    "what did i just do",
])
def test_dictation_and_commands_are_not_hijacked(phrase):
    cs = ConversationState()
    _click(cs, target="Save button")
    out, changed = cs.resolve_anaphora(phrase)
    assert changed is False
    assert out == phrase


def test_again_ignores_clarify_antecedent():
    cs = ConversationState()
    cs.record(command_text="what?", verb="CLARIFY", target="huh", success=True)
    out, changed = cs.resolve_anaphora("again")
    assert changed is False  # CLARIFY is not a re-issuable antecedent
    assert out == "again"


# -- prompt hint ------------------------------------------------------------ #

def test_prompt_hint_empty_on_fresh_state():
    assert ConversationState().prompt_hint() == ""


def test_prompt_hint_success_and_failure():
    cs = ConversationState()
    _click(cs, target="Save button", success=True)
    assert cs.prompt_hint() == "Last action: CLICK Save button (done)"
    cs.record(command_text="close x", verb="CLOSE", target="", success=False)
    assert cs.prompt_hint() == "Last action: CLOSE (failed)"


def test_clear():
    cs = ConversationState()
    _click(cs)
    cs.clear()
    assert cs.last is None
    assert cs.prompt_hint() == ""


# --------------------------------------------------------------------------- #
# HybridCoordinator wiring
# --------------------------------------------------------------------------- #

def _make_coord():
    from core.hybrid_coordinator import HybridCoordinator, CoordinatorConfig

    coord = HybridCoordinator(config=CoordinatorConfig())
    # Force the local path deterministically (Gate 3 does a real VRAM probe).
    coord._gates.gate3 = AsyncMock(return_value=True)
    coord._ground_target = AsyncMock(return_value=None)
    coord._executor.execute = AsyncMock(
        return_value={"status": "ok", "action": "CLICK"}
    )
    return coord


@pytest.mark.asyncio
async def test_execute_action_records_turn():
    coord = _make_coord()
    cmd = Command(text="click the save button", action="", source="voice")
    await coord._execute_action("CLICK Save button", cmd, route_label="local")
    last = coord._conversation.last
    assert last is not None
    assert last.verb == "CLICK"
    assert last.target == "Save button"
    assert last.success is True


@pytest.mark.asyncio
async def test_route_rewrites_anaphora_before_inference():
    coord = _make_coord()
    seen: list[str] = []

    async def fake_local(cmd):
        seen.append(cmd.text)
        return "CLICK Save button"

    coord._run_local = fake_local

    # First turn establishes the antecedent.
    await coord.route(Command(text="click the save button", action="", source="voice"))
    assert coord._conversation.last.target == "Save button"

    # Second turn: "do that again" must be rewritten to the previous command
    # text BEFORE it reaches inference.
    await coord.route(Command(text="do that again", action="", source="voice"))
    assert seen[-1] == "click the save button"


@pytest.mark.asyncio
async def test_route_appends_last_action_hint_to_context():
    coord = _make_coord()
    seen_ctx: list[list[str]] = []

    async def fake_local(cmd):
        seen_ctx.append(list(cmd.session_context or []))
        return "TYPE hello"

    coord._run_local = fake_local

    await coord.route(Command(text="type hello", action="", source="voice"))
    # No hint yet on the first command.
    assert all("Last action:" not in c for c in seen_ctx[-1])

    await coord.route(Command(text="type world", action="", source="voice"))
    # The second command's prompt context carries the structured last-action hint.
    assert any(c.startswith("Last action: TYPE") for c in seen_ctx[-1])
