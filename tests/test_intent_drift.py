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


def _coord():
    c = HybridCoordinator.__new__(HybridCoordinator)
    # Only wire the state _note_intent_drift touches.
    c._session_intent = None
    c._drift_streak = 0
    c._drift_warned = False
    c._agent_db = None
    c._audit = None
    c._session_id = 1
    return c


def _cmd(text: str) -> Command:
    return Command(source="voice", action="", text=text)


def test_first_command_anchors_intent():
    c = _coord()
    c._note_intent_drift(_cmd("fix the bug in the json parser"))
    assert c._session_intent == "fix the bug in the json parser"
    assert c._drift_streak == 0 and not c._drift_warned


def test_on_topic_keeps_streak_zero():
    c = _coord()
    c._note_intent_drift(_cmd("fix the bug in the json parser"))
    # high token overlap → no drift
    c._note_intent_drift(_cmd("fix the json parser bug for trailing commas"))
    assert c._drift_streak == 0 and not c._drift_warned


def test_sustained_divergence_warns_once(monkeypatch):
    c = _coord()
    spoken = []
    # _note_intent_drift fires _tts_speak via fire_and_log; stub both so no loop/db needed.
    monkeypatch.setattr(c, "_tts_speak", lambda text: spoken.append(text) or None)
    # _note_intent_drift imports fire_and_log lazily from core.async_utils.
    monkeypatch.setattr("core.async_utils.fire_and_log", lambda coro, *a, **k: None)

    c._note_intent_drift(_cmd("fix the bug in the json parser"))           # anchor
    c._note_intent_drift(_cmd("now refactor the entire authentication module"))  # 1
    assert c._drift_streak == 1 and not c._drift_warned
    c._note_intent_drift(_cmd("also redesign the database schema layer"))  # 2
    assert c._drift_streak == 2 and not c._drift_warned
    c._note_intent_drift(_cmd("rewrite the css for the settings page"))    # 3 → warn
    assert c._drift_warned is True

    # A subsequent divergent turn does NOT warn again (latched).
    before = c._drift_warned
    c._note_intent_drift(_cmd("update the readme deployment section"))
    assert c._drift_warned == before is True


def test_empty_text_is_noop():
    c = _coord()
    c._note_intent_drift(_cmd("   "))
    assert c._session_intent is None
