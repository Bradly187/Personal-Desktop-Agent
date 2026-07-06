"""Tests for LLM output schema validation in ActionExecutor.

The command-path LLM is prompted to answer verb-first with one of 11
accessibility verbs. Previously execute_action() split the response on
whitespace and dispatched the first token as a verb — a malformed response
(prose, a hallucinated verb, a refusal) became a bad/unknown verb that
surfaced as a raw "Unknown action" error.

Now execute_action() validates the parsed verb against _VALID_COMMAND_VERBS
and degrades anything else to CLARIFY so the user is re-prompted.

Covers:
- parse_action: basic split, leading-label tolerance, empty input, trailing colon
- A valid verb dispatches unchanged
- A prose / hallucinated / dev verb degrades to CLARIFY
- The degraded CLARIFY reaches the executor and sets pending clarification
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.action_executor import ActionExecutor
from core.command_executor import Command
from core.hybrid_coordinator import (
    CoordinatorConfig,
    HybridCoordinator,
    _VALID_COMMAND_VERBS,
)


def _coord() -> HybridCoordinator:
    return HybridCoordinator(config=CoordinatorConfig())


def _cmd(text: str = "scroll down", source: str = "voice") -> Command:
    return Command(text=text, action="", source=source)


# ---------------------------------------------------------------------------
# parse_action
# ---------------------------------------------------------------------------

def test_parse_action_basic():
    assert ActionExecutor.parse_action("CLICK the button") == ("CLICK", "the button")


def test_parse_action_verb_only():
    assert ActionExecutor.parse_action("SCREENSHOT") == ("SCREENSHOT", "")


def test_parse_action_lowercases_to_upper():
    verb, target = ActionExecutor.parse_action("scroll down")
    assert verb == "SCROLL"
    assert target == "down"


def test_parse_action_strips_leading_label():
    assert ActionExecutor.parse_action("Action: SCROLL down") == ("SCROLL", "down")
    assert ActionExecutor.parse_action("command: CLOSE") == ("CLOSE", "")


def test_parse_action_strips_trailing_colon_on_verb():
    # Some models emit "CLICK:" — the colon must not defeat verb matching.
    verb, _ = ActionExecutor.parse_action("CLICK: ok")
    assert verb == "CLICK"
    assert verb in _VALID_COMMAND_VERBS


def test_parse_action_empty():
    assert ActionExecutor.parse_action("   ") == ("", "")


# ---------------------------------------------------------------------------
# Schema validation in execute_action
# ---------------------------------------------------------------------------

async def test_valid_verb_dispatched_unchanged():
    """A well-formed SCROLL response reaches the executor as SCROLL."""
    coord = _coord()
    captured = {}

    async def _exec(exec_cmd):
        captured["action"] = exec_cmd.action
        captured["params"] = exec_cmd.params
        return {"status": "ok", "action": exec_cmd.action}

    coord._executor.execute = _exec

    result = await coord._action_executor.execute_action("SCROLL down", _cmd(), route_label="local")
    assert captured["action"] == "SCROLL"
    assert captured["params"]["direction"] == "down"
    assert result["status"] == "ok"


@pytest.mark.parametrize("bad", [
    "I think you want to close the window",   # prose
    "Sure, closing it now",                   # refusal-style prose
    "FROBNICATE the widget",                  # hallucinated verb
    "WRITE_FILE /etc/passwd",                 # dev verb — not a command-path verb
    "SNAP_WINDOW right_half",                 # gesture verb — never an LLM output
])
async def test_malformed_verb_degrades_to_clarify(bad):
    """Anything outside the 11-verb vocabulary becomes CLARIFY at the executor."""
    coord = _coord()
    captured = {}

    async def _exec(exec_cmd):
        captured["action"] = exec_cmd.action
        captured["params"] = exec_cmd.params
        return {"status": "ok", "clarify": True}

    coord._executor.execute = _exec

    await coord._action_executor.execute_action(bad, _cmd(), route_label="local")
    assert captured["action"] == "CLARIFY"
    # A non-empty re-prompt message is passed through for TTS.
    assert captured["params"].get("message")


async def test_malformed_verb_sets_pending_clarification():
    """After a degraded CLARIFY, pending clarification is recorded for context."""
    coord = _coord()
    coord._executor.execute = AsyncMock(return_value={"status": "ok", "clarify": True})

    await coord._action_executor.execute_action("blah blah nonsense", _cmd(), route_label="local")
    assert coord.state.pending_clarification is not None


async def test_real_clarify_still_works():
    """A genuine CLARIFY from the LLM is not double-handled — it passes through."""
    coord = _coord()
    captured = {}

    async def _exec(exec_cmd):
        captured["action"] = exec_cmd.action
        captured["params"] = exec_cmd.params
        return {"status": "ok", "clarify": True}

    coord._executor.execute = _exec

    await coord._action_executor.execute_action("CLARIFY which window?", _cmd(), route_label="local")
    assert captured["action"] == "CLARIFY"
    assert captured["params"]["message"] == "which window?"
