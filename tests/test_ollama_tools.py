"""Tests for OllamaInference native tool-calling path (Ollama 0.30+).

The opt-in `use_tools=True` path calls /api/chat with a single constrained
`desktop_action` tool whose `verb` enum guarantees a valid action verb — the
same format guarantee the vLLM path gets from grammar-constrained decoding.
The structured tool_call is reconstructed into the exact "VERB argument" string
the default generate path returns, so ActionExecutor.parse_action is
unchanged.

These tests monkeypatch OllamaInference._chat (the only network boundary) so
they run without a live Ollama server or aiohttp mocking.

Covers:
- A well-formed tool_call → reconstructed "VERB argument" action string
- Stringified JSON arguments (some Ollama builds) are parsed
- SCREENSHOT (no argument) yields the bare verb, no trailing space
- An invalid/unknown verb in the tool_call is rejected → content fallback
- No tool_call but free-text content → first-line fallback (graceful)
- Empty response → CLARIFY (never a silent drop)
- A transport error → CLARIFY and available flips False
- use_tools defaults off; status reports the flag
- _action_from_tool_call unit behaviour (wrong tool name, junk args)
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.command_executor import Command
from inference.local_inference import (
    OllamaInference,
    _action_from_tool_call,
    _recover_action_from_content,
    _DESKTOP_ACTION_TOOL,
    _ACTION_VERBS,
)


def _cmd(text: str = "click the save button") -> Command:
    return Command(text=text, action="", source="voice", whisper_logprob=0.0)


def _chat_returning(message: dict):
    """Build an async _chat replacement that returns {'message': message}."""
    async def _fake_chat(messages, tools=None):
        _fake_chat.calls.append({"messages": messages, "tools": tools})
        return {"message": message}
    _fake_chat.calls = []
    return _fake_chat


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

async def test_tool_call_reconstructs_action():
    backend = OllamaInference(use_tools=True)
    backend._chat = _chat_returning({
        "tool_calls": [
            {"function": {"name": "desktop_action",
                          "arguments": {"verb": "CLICK", "argument": "save button"}}}
        ]
    })
    assert await backend.infer(_cmd()) == "CLICK save button"
    # The constrained tool was actually passed to the server.
    assert backend._chat.calls[0]["tools"] == [_DESKTOP_ACTION_TOOL]


async def test_tool_call_stringified_arguments():
    """Some Ollama builds/models return arguments as a JSON string, not a dict."""
    backend = OllamaInference(use_tools=True)
    backend._chat = _chat_returning({
        "tool_calls": [
            {"function": {"name": "desktop_action",
                          "arguments": '{"verb": "OPEN", "argument": "Chrome"}'}}
        ]
    })
    assert await backend.infer(_cmd("open chrome")) == "OPEN Chrome"


async def test_screenshot_has_no_trailing_space():
    backend = OllamaInference(use_tools=True)
    backend._chat = _chat_returning({
        "tool_calls": [
            {"function": {"name": "desktop_action",
                          "arguments": {"verb": "SCREENSHOT", "argument": ""}}}
        ]
    })
    assert await backend.infer(_cmd("take a screenshot")) == "SCREENSHOT"


async def test_missing_argument_key_yields_bare_verb():
    backend = OllamaInference(use_tools=True)
    backend._chat = _chat_returning({
        "tool_calls": [
            {"function": {"name": "desktop_action", "arguments": {"verb": "SCREENSHOT"}}}
        ]
    })
    assert await backend.infer(_cmd()) == "SCREENSHOT"


# ---------------------------------------------------------------------------
# Fallbacks / robustness
# ---------------------------------------------------------------------------

async def test_invalid_verb_falls_back_to_content():
    """An out-of-vocab verb is rejected; the model's content is used instead."""
    backend = OllamaInference(use_tools=True)
    backend._chat = _chat_returning({
        "tool_calls": [
            {"function": {"name": "desktop_action",
                          "arguments": {"verb": "TELEPORT", "argument": "x"}}}
        ],
        "content": "CLARIFY I did not understand",
    })
    assert await backend.infer(_cmd()) == "CLARIFY I did not understand"


async def test_no_tool_call_uses_content_first_line():
    backend = OllamaInference(use_tools=True)
    backend._chat = _chat_returning({"content": "SCROLL down 3\nnoise second line"})
    assert await backend.infer(_cmd("scroll down")) == "SCROLL down 3"


async def test_content_serialised_tool_call_recovered():
    """llama3.1 sometimes emits the call as JSON text in content, not tool_calls.

    Observed live on Ollama 0.30.6: {"name":"CLARIFY","parameters":{...}}.
    """
    backend = OllamaInference(use_tools=True)
    backend._chat = _chat_returning({
        "content": '{"name": "CLARIFY", "parameters": '
                   '{"argument": "What time is it?", "verb": "CLARIFY"}}'
    })
    assert await backend.infer(_cmd("what time is it")) == "CLARIFY What time is it?"


async def test_empty_response_clarifies():
    backend = OllamaInference(use_tools=True)
    backend._chat = _chat_returning({})
    assert await backend.infer(_cmd()) == "CLARIFY no action produced"


async def test_transport_error_clarifies_and_marks_unavailable():
    backend = OllamaInference(use_tools=True)

    async def _boom(messages, tools=None):
        raise RuntimeError("Ollama HTTP 500")

    backend._chat = _boom
    result = await backend.infer(_cmd())
    assert result.startswith("CLARIFY inference error:")
    assert backend._available is False


# ---------------------------------------------------------------------------
# Defaults / status
# ---------------------------------------------------------------------------

def test_use_tools_defaults_off():
    assert OllamaInference().use_tools is False


def test_status_reports_use_tools():
    assert OllamaInference(use_tools=True).get_status()["use_tools"] is True


# ---------------------------------------------------------------------------
# _action_from_tool_call unit behaviour
# ---------------------------------------------------------------------------

def test_action_helper_rejects_wrong_tool_name():
    assert _action_from_tool_call(
        {"function": {"name": "other", "arguments": {"verb": "CLICK"}}}
    ) is None


def test_action_helper_rejects_non_dict_args():
    assert _action_from_tool_call(
        {"function": {"name": "desktop_action", "arguments": "not json"}}
    ) is None


def test_action_helper_rejects_unknown_verb():
    assert _action_from_tool_call(
        {"function": {"name": "desktop_action", "arguments": {"verb": "NOPE"}}}
    ) is None


def test_every_action_verb_round_trips():
    for verb in _ACTION_VERBS:
        out = _action_from_tool_call(
            {"function": {"name": "desktop_action",
                          "arguments": {"verb": verb.lower(), "argument": "x"}}}
        )
        assert out == f"{verb} x"


# ---------------------------------------------------------------------------
# _recover_action_from_content unit behaviour
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("content,expected", [
    # name-as-verb + nested parameters (the observed llama3.1 shape)
    ('{"name": "CLICK", "parameters": {"argument": "Save"}}', "CLICK Save"),
    # flat verb/argument object
    ('{"verb": "OPEN", "argument": "Chrome"}', "OPEN Chrome"),
    # desktop_action name with nested arguments
    ('{"name": "desktop_action", "arguments": {"verb": "SCROLL", "argument": "down 3"}}', "SCROLL down 3"),
    # JSON embedded in surrounding prose
    ('Sure: {"verb": "SCREENSHOT", "argument": ""} done', "SCREENSHOT"),
])
def test_recover_action_from_content(content, expected):
    assert _recover_action_from_content(content) == expected


@pytest.mark.parametrize("content", [
    "",
    "just plain text, no json",
    '{"name": "TELEPORT", "parameters": {}}',   # unknown verb
    '{"not": "an action"}',
])
def test_recover_action_from_content_returns_none(content):
    assert _recover_action_from_content(content) is None
