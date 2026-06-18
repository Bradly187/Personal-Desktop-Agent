"""GAP-10 — denial-of-wallet tripwire.

_note_cloud_call counts cloud API calls per session and speaks a one-time warning
once the count exceeds cloud_call_budget. It is advisory — it never blocks, and
warns only once (latched).

Run:
    python -m pytest tests/test_dow_alert.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.hybrid_coordinator import HybridCoordinator


def _coord(budget: int):
    c = HybridCoordinator.__new__(HybridCoordinator)
    c._session_cloud_calls = 0
    c._cloud_budget_warned = False
    c._audit = None
    c._approval_config = lambda: {"cloud_call_budget": budget}
    return c


def test_warns_once_past_budget(monkeypatch):
    c = _coord(2)
    spoken: list[str] = []
    monkeypatch.setattr(c, "_tts_speak", lambda m: spoken.append(m) or None)
    monkeypatch.setattr("core.async_utils.fire_and_log", lambda coro, *a, **k: None)

    c._note_cloud_call()      # 1 — under budget
    c._note_cloud_call()      # 2 — at budget, no warn
    assert not c._cloud_budget_warned
    c._note_cloud_call()      # 3 — over budget → warn
    assert c._cloud_budget_warned

    # Latched: a 4th call does not warn again, but still counts.
    c._note_cloud_call()
    assert c._session_cloud_calls == 4 and c._cloud_budget_warned


def test_default_budget_when_config_missing(monkeypatch):
    c = HybridCoordinator.__new__(HybridCoordinator)
    c._session_cloud_calls = 0
    c._cloud_budget_warned = False
    c._audit = None
    c._approval_config = lambda: {}   # no key → default 20
    assert c._cloud_call_budget() == 20
