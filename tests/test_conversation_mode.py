"""Unit tests for voice conversation mode (core/conversation_mode.py).

Covers deterministic wake/sleep detection (including fail-safe non-matches that
must NOT toggle the mode), history accumulation/trimming, context rendering, and
config parsing. The model inference, TTS, and mic suppression live in
HybridCoordinator and are out of scope here — this module is pure/synchronous.

Spec: specs/conversation-mode/.
"""

import pytest

from core.conversation_mode import (
    ConversationMode,
    ConversationTurn,
    conversation_mode_config,
)


# --- wake detection ------------------------------------------------------- #

@pytest.mark.parametrize("utterance", [
    "let's talk",
    "Let's talk.",
    "lets talk",
    "let's chat",
    "Hey agent, let's talk",          # leading filler stripped
    "okay let's have a conversation",
    "start a conversation",
    "conversation mode",
    "let's talk please",              # trailing politeness stripped
])
def test_detect_wake_positive(utterance):
    cm = ConversationMode(enabled=True)
    assert cm.detect_wake(utterance) is True


@pytest.mark.parametrize("utterance", [
    "talk to the team about it",
    "let's open the talk document",
    "I want to start a new file",
    "click the chat button",
    "what should we talk about",      # contains 'talk' but isn't the phrase
    "",
    "   ",
])
def test_detect_wake_negative(utterance):
    """Ordinary commands/dictation must never trip the wake phrase."""
    cm = ConversationMode(enabled=True)
    assert cm.detect_wake(utterance) is False


# --- combined wake + first turn (one breath) ------------------------------ #

@pytest.mark.parametrize("utterance,expected_remainder", [
    ("let's talk", ""),
    ("conversation mode", ""),
    ("conversation mode, what is the weather like",
     "what is the weather like"),
    ("let's talk about my schedule for tomorrow",
     "about my schedule for tomorrow"),
    ("hey agent, let's chat — how are you",
     "how are you"),
])
def test_match_wake_returns_remainder(utterance, expected_remainder):
    cm = ConversationMode(enabled=True)
    matched, remainder = cm.match_wake(utterance)
    assert matched is True
    assert remainder == expected_remainder


@pytest.mark.parametrize("utterance", [
    "what should we talk about",
    "open the talk document",
    "click the chat button",
])
def test_match_wake_no_false_prefix(utterance):
    cm = ConversationMode(enabled=True)
    matched, remainder = cm.match_wake(utterance)
    assert matched is False
    assert remainder == ""


# --- sleep detection ------------------------------------------------------ #

@pytest.mark.parametrize("utterance", [
    "that's all",
    "That's all.",
    "thats all",
    "that's all for now",
    "goodbye",
    "we're done",
    "stop talking",
    "end conversation",
    "okay that's all please",
])
def test_detect_sleep_positive(utterance):
    cm = ConversationMode(enabled=True)
    assert cm.detect_sleep(utterance) is True


@pytest.mark.parametrize("utterance", [
    "how do you say goodbye in French",   # the classic false-positive trap
    "tell me when we are done with that",
    "that's all the information I have on the topic",
    "I want to stop talking to my landlord about the lease",
    "",
])
def test_detect_sleep_negative(utterance):
    """A sleep word embedded in a real sentence must NOT end the conversation."""
    cm = ConversationMode(enabled=True)
    assert cm.detect_sleep(utterance) is False


# --- lifecycle ------------------------------------------------------------ #

def test_enter_exit_toggles_active_and_clears_history():
    cm = ConversationMode(enabled=True)
    assert cm.active is False
    cm.enter()
    assert cm.active is True
    cm.record("user", "hi")
    cm.record("assistant", "hello")
    assert len(cm.history) == 2
    cm.exit()
    assert cm.active is False
    assert cm.history == []


def test_enter_clears_prior_history():
    cm = ConversationMode(enabled=True)
    cm.enter()
    cm.record("user", "stale")
    cm.enter()  # re-entering starts fresh
    assert cm.history == []


# --- history accumulation / trimming ------------------------------------- #

def test_history_trims_to_max_turns():
    cm = ConversationMode(enabled=True, max_history_turns=4)
    for i in range(10):
        cm.record("user" if i % 2 == 0 else "assistant", f"msg{i}")
    hist = cm.history
    assert len(hist) == 4
    # The most recent entries are retained.
    assert hist[-1].content == "msg9"
    assert hist[0].content == "msg6"


def test_history_property_returns_copy():
    cm = ConversationMode(enabled=True)
    cm.record("user", "a")
    snapshot = cm.history
    snapshot.append(ConversationTurn(role="user", content="tamper"))
    assert len(cm.history) == 1  # internal state unaffected


# --- context rendering ---------------------------------------------------- #

def test_build_context_directive_only_when_empty():
    cm = ConversationMode(enabled=True)
    ctx = cm.build_context()
    assert cm.directive in ctx
    assert "Conversation so far" not in ctx


def test_build_context_includes_transcript():
    cm = ConversationMode(enabled=True)
    cm.record("user", "what's the capital of France")
    cm.record("assistant", "Paris.")
    ctx = cm.build_context()
    assert "Conversation so far:" in ctx
    assert "User: what's the capital of France" in ctx
    assert "Assistant: Paris." in ctx


# --- config parsing ------------------------------------------------------- #

def test_from_config_default_disabled():
    cm = ConversationMode.from_config({})
    assert cm.enabled is False
    # default phrase sets are populated
    assert cm.detect_wake("let's talk")
    assert cm.detect_sleep("goodbye")


def test_from_config_enabled_and_custom_phrases():
    cm = ConversationMode.from_config({
        "enabled": True,
        "wake_phrases": ["wake up"],
        "sleep_phrases": ["go to sleep"],
        "max_history_turns": 6,
    })
    assert cm.enabled is True
    assert cm.detect_wake("wake up")
    assert cm.detect_sleep("go to sleep")
    # custom sets REPLACE the defaults
    assert cm.detect_wake("let's talk") is False
    assert cm.max_history_turns == 6


def test_from_config_ignores_malformed_values():
    cm = ConversationMode.from_config({
        "enabled": "yes",            # truthy → True
        "wake_phrases": "not a list",
        "max_history_turns": "lots",
    })
    assert cm.enabled is True
    assert cm.detect_wake("let's talk")  # falls back to defaults
    assert cm.max_history_turns == 12


def test_conversation_mode_config_missing_file(monkeypatch, tmp_path):
    """No config file → disabled default, never raises."""
    monkeypatch.setattr("os.path.expanduser",
                        lambda p: str(tmp_path / "nope.json"))
    cfg = conversation_mode_config()
    assert cfg == {"enabled": False}
