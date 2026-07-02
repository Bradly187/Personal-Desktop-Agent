"""GAP-10 — denial-of-wallet tripwire.

GateEvaluator.note_cloud_call counts cloud API calls per session and speaks a
one-time warning once the count exceeds cloud_call_budget. It is advisory — it
never blocks, and warns only once (latched).

Run:
    python -m pytest tests/test_dow_alert.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.gate_evaluator import GateEvaluator


def _gates(budget: int):
    return GateEvaluator(
        cfg=None,
        run_local=None,
        run_cloud=None,
        approval_config=lambda: {"cloud_call_budget": budget},
        audit=None,
    )


def test_warns_once_past_budget(monkeypatch):
    gates = _gates(2)
    spoken: list[str] = []
    monkeypatch.setattr(gates, "_tts_speak", lambda m: spoken.append(m) or None)
    monkeypatch.setattr("core.async_utils.fire_and_log", lambda coro, *a, **k: None)

    gates.note_cloud_call()      # 1 — under budget
    gates.note_cloud_call()      # 2 — at budget, no warn
    assert not gates._cloud_budget_warned
    gates.note_cloud_call()      # 3 — over budget → warn
    assert gates._cloud_budget_warned

    # Latched: a 4th call does not warn again, but still counts.
    gates.note_cloud_call()
    assert gates._session_cloud_calls == 4 and gates._cloud_budget_warned


def test_default_budget_when_config_missing(monkeypatch):
    gates = GateEvaluator(
        cfg=None, run_local=None, run_cloud=None,
        approval_config=lambda: {})   # no key → default 20
    assert gates.cloud_call_budget() == 20
