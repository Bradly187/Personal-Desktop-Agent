"""Regression: voice system-control keywords must tolerate trailing punctuation.

Whisper routinely renders "hey agent pain day on" as "Pain day on." — the
trailing period made `_lower in (...)` exact-match miss, so the manual-pain-day
(and lecture/condition/calibration) keywords silently failed. Surfaced by a live
device run. _lower now strips surrounding punctuation as well as whitespace.
"""

import asyncio
from unittest.mock import MagicMock

from core.command_executor import Command
from core.hybrid_coordinator import HybridCoordinator


def _coord():
    c = HybridCoordinator()
    c._twin = MagicMock()       # set_manual_pain_day is sync
    c._whisper = MagicMock()    # apply_pain_day is sync
    return c


def _route(c, text):
    return asyncio.run(c.route(Command(text=text, action="DICTATE", source="voice")))


def test_pain_day_on_with_trailing_period():
    c = _coord()
    res = _route(c, "Pain day on.")          # exactly what Whisper produced live
    c._twin.set_manual_pain_day.assert_called_once_with(True)
    assert res.get("action") == "PAIN_DAY"


def test_pain_day_on_clean_still_matches():
    c = _coord()
    _route(c, "pain day on")
    c._twin.set_manual_pain_day.assert_called_once_with(True)


def test_pain_day_on_with_question_mark_and_caps():
    c = _coord()
    _route(c, "Pain Day On?")
    c._twin.set_manual_pain_day.assert_called_once_with(True)


def test_feeling_better_with_bang_turns_off():
    c = _coord()
    res = _route(c, "Feeling better!")
    c._twin.set_manual_pain_day.assert_called_once_with(False)
    assert res.get("action") == "PAIN_DAY"


# --- bug #3: dev-agent pre-gate must not shadow system-control keywords -------

def test_is_system_control_voice_classifier():
    from core.voice_system_control import _is_system_control_voice
    assert _is_system_control_voice(Command(text="Pain day on.", action="X", source="voice"))
    assert _is_system_control_voice(Command(text="calibrate allergy day", action="X", source="voice"))
    # a genuine dev query is NOT system-control
    assert not _is_system_control_voice(
        Command(text="explain this function", action="X", source="voice"))
    # only voice sources qualify
    assert not _is_system_control_voice(
        Command(text="pain day on", action="X", source="touch"))


def test_pain_day_not_shadowed_when_dev_agent_present():
    # With a DevAgent wired, "pain day on" classifies as a dev domain — without
    # the guard it was intercepted and sent to an LLM (the live 404 bug).
    c = _coord()
    c._dev_agent = MagicMock()
    res = _route(c, "Pain day on.")
    c._dev_agent.handle.assert_not_called()          # not misrouted to DevAgent
    c._twin.set_manual_pain_day.assert_called_once_with(True)
    assert res.get("action") == "PAIN_DAY"
