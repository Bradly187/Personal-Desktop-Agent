"""Tests for plan-contract auto-repair (specs/dev-agent-plan-contract).

Covers the structured parse-report (drops recorded, not swallowed), the bounded
corrective re-prompt loop, and the default-OFF / fail-safe behavior. The planner
model is stubbed so everything runs in CI.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from inference.dev_agent import (
    DevAgent,
    _parse_plan_json,
    _parse_plan_json_report,
    _build_plan_repair_prompt,
)


# --------------------------------------------------------------------------- #
# Stubs
# --------------------------------------------------------------------------- #

class _Result:
    def __init__(self, text, ok=True, model="plan-model", error=None):
        self.text = text
        self.ok = ok
        self.model = model
        self.error = error


class _FakeRouter:
    """Returns queued _Result objects on successive infer() calls."""
    def __init__(self, results=None):
        self._results = list(results or [])
        self.calls = 0

    async def infer(self, **kw):
        self.calls += 1
        return self._results.pop(0) if self._results else _Result("{}")


def _agent(router, *, enabled=True, max_repairs=1):
    a = DevAgent(router=router)
    a._plan_repair_enabled = enabled
    a._plan_repair_max = max_repairs
    return a


_WF = '{"action":"WRITE_FILE","args":"a.py"}'
_RT = '{"action":"RUN_TERMINAL","args":"pytest -q"}'
_BAD = '{"action":"NOPE"}'


# --------------------------------------------------------------------------- #
# R1.1 — report records drops instead of swallowing them
# --------------------------------------------------------------------------- #

def test_report_records_unknown_action_drop():
    r = _parse_plan_json_report('{"steps":[%s,%s]}' % (_WF, _BAD))
    assert r.parsed_ok is True
    assert [s.action for s in r.steps] == ["WRITE_FILE"]
    assert len(r.dropped) == 1
    assert r.dropped[0].index == 2
    assert r.dropped[0].raw_action == "NOPE"
    assert r.dropped[0].reason == "unknown action"


def test_report_records_non_object_item():
    r = _parse_plan_json_report('{"steps":["oops"]}')
    assert r.parsed_ok is True
    assert r.steps == []
    assert r.dropped[0].reason == "not an object"


def test_report_parsed_ok_false_on_garbage():
    r = _parse_plan_json_report("this is not json")
    assert r.parsed_ok is False
    assert r.steps == [] and r.dropped == []


def test_back_compat_parse_plan_json_still_raises_on_garbage():
    # The wrapper preserves the raising contract the caller's regex fallback uses.
    with pytest.raises((ValueError, Exception)):
        _parse_plan_json("not json")
    # ...and returns steps on a valid array (unknown verbs dropped).
    steps = _parse_plan_json('{"steps":[%s,%s]}' % (_WF, _BAD))
    assert [s.action for s in steps] == ["WRITE_FILE"]


# --------------------------------------------------------------------------- #
# R1.3 — corrective prompt names the failure + lists valid actions
# --------------------------------------------------------------------------- #

def test_repair_prompt_names_offender_and_lists_valid_actions():
    r = _parse_plan_json_report('{"steps":[{"action":"EDITT"}]}')
    msg = _build_plan_repair_prompt(r)
    assert "EDITT" in msg
    assert "WRITE_FILE" in msg            # valid actions enumerated
    assert "was NOT executed" in msg


def test_repair_prompt_handles_no_steps_array():
    r = _parse_plan_json_report("garbage")
    msg = _build_plan_repair_prompt(r)
    assert "no valid" in msg.lower()


# --------------------------------------------------------------------------- #
# R3.1 — default OFF: no re-prompt, dropped steps surface via WARNING not silence
# --------------------------------------------------------------------------- #

async def test_repair_disabled_does_not_reprompt():
    router = _FakeRouter()
    agent = _agent(router, enabled=False)
    plan = _Result('{"steps":[%s,%s]}' % (_WF, _BAD))
    steps, final = await agent._acquire_plan_steps("goal", plan, "")
    assert [s.action for s in steps] == ["WRITE_FILE"]
    assert router.calls == 0          # disabled → never re-prompts
    assert final is plan


def test_default_flag_off_when_env_unset(monkeypatch):
    monkeypatch.delenv("DA_PLAN_REPAIR", raising=False)
    agent = DevAgent(router=_FakeRouter())
    assert agent._plan_repair_enabled is False


# --------------------------------------------------------------------------- #
# R1.2 — a drop fires exactly one corrective re-prompt that can recover
# --------------------------------------------------------------------------- #

async def test_repair_reprompts_once_and_recovers_clean_plan():
    good = _Result('{"steps":[%s,%s]}' % (_WF, _RT))
    router = _FakeRouter([good])
    agent = _agent(router, enabled=True, max_repairs=1)
    bad = _Result('{"steps":[%s,%s]}' % (_WF, _BAD))
    steps, final = await agent._acquire_plan_steps("goal", bad, "ctx")
    assert router.calls == 1
    assert [s.action for s in steps] == ["WRITE_FILE", "RUN_TERMINAL"]
    assert final is good


async def test_repair_fires_on_empty_unparseable_plan():
    good = _Result('{"steps":[%s]}' % _WF)
    router = _FakeRouter([good])
    agent = _agent(router, enabled=True, max_repairs=1)
    steps, final = await agent._acquire_plan_steps("goal", _Result("totally unparseable"), "")
    assert router.calls == 1
    assert [s.action for s in steps] == ["WRITE_FILE"]


# --------------------------------------------------------------------------- #
# R1.4 / R1.5 — bounded, and fail-safe to empty (caller wraps EXPLAIN)
# --------------------------------------------------------------------------- #

async def test_repair_bounded_by_max():
    bad = '{"steps":[%s]}' % _BAD
    router = _FakeRouter([_Result(bad), _Result(bad), _Result(bad)])
    agent = _agent(router, enabled=True, max_repairs=2)
    steps, final = await agent._acquire_plan_steps("goal", _Result(bad), "")
    assert router.calls == 2          # exactly max re-prompts, then stop
    assert steps == []                # nothing recovered → caller falls to EXPLAIN


async def test_repair_inference_failure_degrades_gracefully():
    router = _FakeRouter([_Result("", ok=False, error="boom")])
    agent = _agent(router, enabled=True, max_repairs=1)
    bad = _Result('{"steps":[%s]}' % _BAD)
    steps, final = await agent._acquire_plan_steps("goal", bad, "")
    assert router.calls == 1
    assert steps == []
    assert final is bad               # degraded to the prior plan_result, no crash


# --------------------------------------------------------------------------- #
# Regex fallback rescues a free-text plan → no re-prompt even when enabled
# --------------------------------------------------------------------------- #

async def test_regex_fallback_rescues_without_repair():
    router = _FakeRouter()
    agent = _agent(router, enabled=True, max_repairs=1)
    text = "WRITE_FILE a.py\nprint('hi')\nRUN_TERMINAL pytest -q"
    steps, final = await agent._acquire_plan_steps("goal", _Result(text), "")
    assert router.calls == 0          # regex recovered a plan → no re-prompt
    assert [s.action for s in steps] == ["WRITE_FILE", "RUN_TERMINAL"]
