"""Tests for structured-output plan parsing (audit fix 2026-06-09, Phase 1c).

The planner now emits JSON constrained by an Ollama `format` schema instead of
free text. _parse_plan_json maps it to AgentSteps; the regex _parse_plan stays
as a fallback for backends that don't honor `format`. This eliminates the
free-text body-collision (a body line beginning with a verb becoming a phantom
step) and the `]`-in-args truncation.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from inference.dev_agent import _parse_plan_json, DevAgent
from inference.model_router import _PLAN_JSON_SCHEMA, _PROFILES
from core.goal_session import GoalSessionStore


@pytest.fixture(autouse=True)
def _isolated_goal_session(tmp_path, monkeypatch):
    monkeypatch.setattr(GoalSessionStore, "PATH", tmp_path / "goal_session.json")
    GoalSessionStore.cancel()
    yield
    GoalSessionStore.cancel()


# ---------------------------------------------------------------------------
# _parse_plan_json
# ---------------------------------------------------------------------------

def test_parses_steps_with_deps():
    text = json.dumps({"steps": [
        {"action": "READ_FILE", "args": "a.py", "after": []},
        {"action": "WRITE_FILE", "args": "b.py", "body": "x = 1", "after": [1]},
    ]})
    steps = _parse_plan_json(text)
    assert [s.action for s in steps] == ["READ_FILE", "WRITE_FILE"]
    assert steps[1].args == "b.py"
    assert steps[1].body == "x = 1"
    assert steps[1].deps == [1]


def test_body_with_verb_prefixed_lines_does_not_spawn_phantom_steps():
    """The free-text parser turned a body line beginning with TYPE/OPEN into a
    new step. JSON keeps the body intact regardless of its content."""
    body = "type Alias = int\nOpen the settings file and edit it\nCLICK here  # prose"
    text = json.dumps({"steps": [
        {"action": "WRITE_FILE", "args": "mod.py", "body": body},
    ]})
    steps = _parse_plan_json(text)
    assert len(steps) == 1                      # exactly one step — no phantoms
    assert steps[0].action == "WRITE_FILE"
    assert steps[0].body == body                # body preserved verbatim


def test_args_with_bracket_not_truncated():
    """The regex `[^\\]\\n]+` truncated args at the first ']'. JSON does not."""
    text = json.dumps({"steps": [
        {"action": "GREP", "args": "interface\\[\\] foo  pattern with ] bracket"},
    ]})
    steps = _parse_plan_json(text)
    assert steps[0].args.endswith("] bracket")  # full args retained


def test_unknown_verb_is_skipped():
    text = json.dumps({"steps": [
        {"action": "WRITE_FILE", "args": "a"},
        {"action": "TOTALLY_BOGUS", "args": "x"},
    ]})
    steps = _parse_plan_json(text)
    assert [s.action for s in steps] == ["WRITE_FILE"]


def test_bare_array_root_accepted():
    text = json.dumps([{"action": "EXPLAIN", "body": "hi"}])
    steps = _parse_plan_json(text)
    assert steps[0].action == "EXPLAIN"


def test_malformed_json_raises():
    with pytest.raises(Exception):
        _parse_plan_json("not json at all { [")


def test_missing_steps_key_raises():
    with pytest.raises(ValueError):
        _parse_plan_json(json.dumps({"plan": "wrong key"}))


def test_after_tolerates_strings_and_floats():
    text = json.dumps({"steps": [
        {"action": "WRITE_FILE", "args": "a"},
        {"action": "WRITE_FILE", "args": "b", "after": ["1", 1.0, "x"]},
    ]})
    steps = _parse_plan_json(text)
    assert steps[1].deps == [1]                 # deduped, non-numeric dropped


# ---------------------------------------------------------------------------
# Schema / profile wiring
# ---------------------------------------------------------------------------

def test_plan_profile_carries_json_schema():
    assert _PROFILES["plan"].json_schema is _PLAN_JSON_SCHEMA
    # Other profiles must NOT (only the plan path uses structured output).
    assert _PROFILES["command"].json_schema is None
    assert _PROFILES["code"].json_schema is None


def test_schema_enum_matches_plan_actions():
    from inference.dev_agent import _PLAN_ACTIONS
    enum = set(_PLAN_JSON_SCHEMA["properties"]["steps"]["items"]
               ["properties"]["action"]["enum"])
    assert enum == _PLAN_ACTIONS


def test_call_ollama_sets_format_only_for_plan(monkeypatch):
    """_call_ollama must pass `format` iff the profile has a json_schema."""
    from inference import model_router as mr
    captured = {}

    class _Resp:
        def __init__(self, payload):
            self._payload = payload
        def read(self):
            return json.dumps({"response": '{"steps": []}'}).encode()
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def _fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode())
        return _Resp(captured["body"])

    monkeypatch.setattr(mr.urllib.request, "urlopen", _fake_urlopen)
    router = mr.ModelRouter()

    router._call_ollama(_PROFILES["plan"], "prompt", None)
    assert "format" in captured["body"]
    assert captured["body"]["format"] == _PLAN_JSON_SCHEMA

    router._call_ollama(_PROFILES["code"], "prompt", None)
    assert "format" not in captured["body"]


# ---------------------------------------------------------------------------
# plan_and_run integration: JSON preferred, regex fallback
# ---------------------------------------------------------------------------

class _RR:
    def __init__(self, text, ok=True, model="m"):
        self.text, self.ok, self.model = text, ok, model
        self.error = None if ok else "err"


def _agent_with_plan_text(plan_text):
    router = MagicMock()
    router.infer = AsyncMock(return_value=_RR(plan_text))
    agent = DevAgent(router=router)
    agent._approve_plan_upfront = AsyncMock(return_value=True)
    agent._rag_context = AsyncMock(return_value="")
    agent._git_context = AsyncMock(return_value="")
    agent._format_context = lambda: ""
    agent._reflect = AsyncMock(return_value="done")
    agent._speak_plan_completion = AsyncMock()
    agent._persist_run = AsyncMock()
    agent._persist_step = AsyncMock()
    agent._start_run = AsyncMock(return_value=-1)
    agent._finalize_run = AsyncMock()
    return agent


async def test_plan_and_run_prefers_json():
    plan = json.dumps({"steps": [
        {"action": "WRITE_FILE", "args": "a.py", "body": "OPEN nothing here"},
    ]})
    agent = _agent_with_plan_text(plan)
    ran = []

    async def _exec(step):
        ran.append((step.action, step.body))
        return "ok"

    agent._execute_step = _exec
    result = await agent.plan_and_run("do it")
    assert result.success is True
    assert ran == [("WRITE_FILE", "OPEN nothing here")]   # one step, body intact


async def test_plan_and_run_falls_back_to_regex_on_freetext():
    # A backend that ignored `format` returns the old free-text plan.
    agent = _agent_with_plan_text("Step 1: [WRITE_FILE a.py]\nStep 2: [EXPLAIN done]")
    ran = []

    async def _exec(step):
        ran.append(step.action)
        return "ok"

    agent._execute_step = _exec
    result = await agent.plan_and_run("do it")
    assert result.success is True
    assert ran == ["WRITE_FILE", "EXPLAIN"]               # regex fallback parsed it
