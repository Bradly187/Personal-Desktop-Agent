"""B4 — undo phrases must route as voice system-control.

Spec: specs/bugfix-b4-undo-phrases/

``_SYSTEM_CONTROL_PHRASES`` is the frozenset ``_is_system_control_voice`` checks
to short-circuit the dev-agent pre-gate in the EventDispatcher. The undo/revert
phrases handled by ``maybe_handle`` were missing from it, so "undo that run"
could be classified as a ``dev``/``general`` command and misrouted to DevAgent
or the LLM instead of the rewind handler — an accessibility-critical failure for
a user whose primary undo mechanism is voice.

The fix hoists the undo phrases into a named ``_UNDO_PHRASES`` constant that both
``_SYSTEM_CONTROL_PHRASES`` and the ``maybe_handle`` branch derive from, so they
cannot drift out of sync again. The unit tests below lock that structural
guarantee; ``TestUndoRoutingIntegration`` drives the real
``EventDispatcher.route_impl`` (via ``HybridCoordinator.route``) end to end to
prove an "undo that run" voice command reaches the rewind handler (``REVERT_RUN``)
instead of the dev-agent pre-gate / domain classifier (R2.2).
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.command_executor import Command
from core.hybrid_coordinator import HybridCoordinator
from core.voice_system_control import (
    _SYSTEM_CONTROL_PHRASES,
    _UNDO_PHRASES,
    _is_system_control_voice,
)


def _voice(text: str, source: str = "voice") -> Command:
    return Command(text=text, action="", source=source)


@pytest.mark.parametrize("phrase", sorted(_UNDO_PHRASES))
def test_each_undo_phrase_is_system_control(phrase):
    assert _is_system_control_voice(_voice(phrase)) is True


@pytest.mark.parametrize("phrase", ["pain day on", "stop agent", "agent status"])
def test_existing_phrases_still_system_control(phrase):
    # Merging _UNDO_PHRASES must not shadow or drop any pre-existing phrase (R2.1).
    assert _is_system_control_voice(_voice(phrase)) is True


def test_undo_phrases_are_subset_of_system_control():
    # Structural enforcement: the "KEEP IN SYNC" comment is replaced by this
    # invariant (R1.4). If the two ever diverge, this fails.
    assert _UNDO_PHRASES.issubset(_SYSTEM_CONTROL_PHRASES)


def test_undo_phrase_normalized_for_case_and_punctuation():
    # _is_system_control_voice lowercases and strips surrounding punctuation.
    assert _is_system_control_voice(_voice("Undo that run.")) is True


def test_touch_source_is_never_system_control():
    # Only voice / voice_local sources may match — a touch command that happens
    # to carry the same text must not be intercepted as a voice keyword.
    assert _is_system_control_voice(_voice("undo that run", source="touch")) is False


def test_voice_local_source_matches():
    assert _is_system_control_voice(_voice("undo that run", source="voice_local")) is True


class TestUndoRoutingIntegration:
    """R2.2 — drive the REAL EventDispatcher.route_impl end to end.

    The dev-agent pre-gate (event_dispatcher.py:57-61) is skipped when
    ``_is_system_control_voice`` is True, so an undo phrase must fall through to
    ``_voice_control.maybe_handle`` (the VoiceRewindHandler) and return
    ``REVERT_RUN`` — never the domain-classifier / dev-agent path. Uses the real
    coordinator-wired ``_voice_control`` (dev_agent=lambda: self._dev_agent), so
    only the DevAgent leaf is mocked. Mirrors test_domain_classifier_accessor.py.
    """

    @staticmethod
    def _coord() -> HybridCoordinator:
        coord = HybridCoordinator()
        coord._dev_agent = MagicMock()
        # dev-agent pre-gate leaf: if the undo were misrouted here, handle() runs.
        coord._dev_agent.handle = AsyncMock(return_value=MagicMock(
            domain="code", model_used="qwen3-coder:30b", response_text="ok", steps=[],
        ))
        # rewind leaf reached by the undo branch of maybe_handle.
        coord._dev_agent.revert_last_run = AsyncMock()
        return coord

    def test_undo_voice_command_routes_to_rewind_not_dev_agent(self):
        coord = self._coord()
        result = asyncio.run(coord.route(
            Command(text="undo that run", action="CLARIFY", source="voice")
        ))
        # Reached the VoiceRewindHandler: REVERT_RUN with a live agent to undo.
        assert result["action"] == "REVERT_RUN"
        assert result.get("offered") is True
        # The dev-agent pre-gate / domain classifier was NOT consulted.
        coord._dev_agent.handle.assert_not_awaited()

    def test_non_undo_dev_command_still_reaches_dev_pregate(self):
        # Contrast: a genuine dev phrase is NOT system-control, so it must still
        # flow through the pre-gate to the DevAgent — proving the undo routing is
        # a targeted interception, not a blanket short-circuit.
        coord = self._coord()
        result = asyncio.run(coord.route(
            Command(text="refactor the auth module", action="CLARIFY", source="voice")
        ))
        assert result["action"] == "dev_agent"
        coord._dev_agent.handle.assert_awaited_once()
