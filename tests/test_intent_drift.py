"""GAP-6 — intent drift / trust-decay tripwire.

The first dev command anchors the session intent; sustained divergence (3
consecutive low-overlap turns) logs a one-time DRIFT_WARNING and persists a row,
but never blocks the command. On-topic follow-ups keep the streak at zero.

Run:
    python -m pytest tests/test_intent_drift.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.hybrid_coordinator import HybridCoordinator
from core.command_executor import Command


from core.coordinator_state import CoordinatorState
from core.correction_handler import CorrectionHandler

def _coord():
    c = HybridCoordinator.__new__(HybridCoordinator)
    # Only wire the state _note_intent_drift touches.
    c.state = CoordinatorState()
    c.state.session_intent = None
    c.state.drift_streak = 0
    c.state.drift_warned = False
    c._correction_handler = CorrectionHandler(
        state=c.state,
        agent_db=lambda: getattr(c, "_agent_db", None),
        trainer=lambda: getattr(c, "_trainer", None),
        audit=lambda: getattr(c, "_audit", None),
        tts_speak=lambda text: c._tts_speak(text) if hasattr(c, "_tts_speak") and c._tts_speak else None,
        session_id=1
    )
    c._agent_db = None
    c._audit = None
    c._session_id = 1
    return c


def _cmd(text: str) -> Command:
    return Command(source="voice", action="", text=text)


def test_first_command_anchors_intent():
    c = _coord()
    c._correction_handler.note_intent_drift(_cmd("fix the bug in the json parser"))
    assert c.state.session_intent == "fix the bug in the json parser"
    assert c.state.drift_streak == 0 and not c.state.drift_warned


def test_on_topic_keeps_streak_zero():
    c = _coord()
    c._correction_handler.note_intent_drift(_cmd("fix the bug in the json parser"))
    # high token overlap → no drift
    c._correction_handler.note_intent_drift(_cmd("fix the json parser bug for trailing commas"))
    assert c.state.drift_streak == 0 and not c.state.drift_warned


def test_sustained_divergence_warns_once(monkeypatch):
    c = _coord()
    spoken = []
    # _note_intent_drift fires _tts_speak via fire_and_log; stub both so no loop/db needed.
    monkeypatch.setattr(c, "_tts_speak", lambda text: spoken.append(text) or None)
    monkeypatch.setattr("core.correction_handler.fire_and_log", lambda coro, *a, **k: None)

    c._correction_handler.note_intent_drift(_cmd("fix the bug in the json parser"))           # anchor
    c._correction_handler.note_intent_drift(_cmd("now refactor the entire authentication module"))  # 1
    assert c.state.drift_streak == 1 and not c.state.drift_warned
    c._correction_handler.note_intent_drift(_cmd("also redesign the database schema layer"))  # 2
    assert c.state.drift_streak == 2 and not c.state.drift_warned
    c._correction_handler.note_intent_drift(_cmd("rewrite the css for the settings page"))    # 3 → warn
    assert c.state.drift_warned is True

    # A subsequent divergent turn does NOT warn again (latched).
    before = c.state.drift_warned
    c._correction_handler.note_intent_drift(_cmd("update the readme deployment section"))
    assert c.state.drift_warned == before is True


def test_empty_text_is_noop():
    c = _coord()
    c._correction_handler.note_intent_drift(_cmd("   "))
    assert c.state.session_intent is None
