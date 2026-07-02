"""Trajectory-eval logic tests — verb extraction, scoring, aggregation, gate.

Model-free (plan_fn is a fake), so these run in CI and never hang. The model-backed
path lives in evals/run.py --mode trajectory and is exercised against the live model.

Run: python -m pytest tests/test_evals_trajectory.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from evals.trajectory import (
    TrajectoryCase,
    TrajPrediction,
    extract_plan_verbs,
    score_trajectory,
    aggregate_traj,
    load_trajectory_suite,
)
from evals.runner import run_trajectory_suite
from evals.scoring import check_regression


# --------------------------------------------------------------------------- #
# extract_plan_verbs (mirrors dev_agent._parse_plan / _parse_plan_json)
# --------------------------------------------------------------------------- #

def test_extract_from_json_steps():
    raw = '{"steps": [{"action": "READ_FILE", "args": "x.py"}, {"action": "WRITE_FILE"}]}'
    assert extract_plan_verbs(raw) == ["READ_FILE", "WRITE_FILE"]


def test_extract_from_bare_json_list():
    raw = '[{"action": "GREP"}, {"action": "EXPLAIN"}]'
    assert extract_plan_verbs(raw) == ["GREP", "EXPLAIN"]


def test_extract_json_drops_unknown_verbs():
    raw = '{"steps": [{"action": "WRITE_FILE"}, {"action": "FLY_TO_MOON"}]}'
    assert extract_plan_verbs(raw) == ["WRITE_FILE"]


def test_extract_from_free_text_numbered():
    raw = ("1. READ_FILE parser.py\n"
           "2. WRITE_FILE parser.py (after: 1)\n"
           "3. RUN_TERMINAL pytest -q (after: 2)")
    assert extract_plan_verbs(raw) == ["READ_FILE", "WRITE_FILE", "RUN_TERMINAL"]


def test_extract_free_text_bracket_style():
    raw = "[GIT_STATUS]\n[GIT_DIFF]\n[GIT_COMMIT] fix the bug"
    assert extract_plan_verbs(raw) == ["GIT_STATUS", "GIT_DIFF", "GIT_COMMIT"]


def test_extract_empty():
    assert extract_plan_verbs("") == []
    assert extract_plan_verbs("just some prose with no verbs") == []


# --------------------------------------------------------------------------- #
# case normalization
# --------------------------------------------------------------------------- #

def _case(**kw) -> TrajectoryCase:
    base = dict(id="t1", suite="t", goal="do x", expected_verbs=["WRITE_FILE"])
    base.update(kw)
    return TrajectoryCase.from_dict(base)


def test_case_uppercases_and_defaults_required():
    c = _case(expected_verbs=["write_file", "run_terminal"])
    assert c.expected_verbs == ["WRITE_FILE", "RUN_TERMINAL"]
    assert c.required == ["WRITE_FILE", "RUN_TERMINAL"]   # defaults to expected


def test_case_explicit_required_and_forbidden_uppercased():
    c = _case(expected_verbs=["read_file", "explain"],
              required=["explain"], forbidden=["write_file"])
    assert c.required == ["EXPLAIN"]
    assert c.forbidden == ["WRITE_FILE"]


# --------------------------------------------------------------------------- #
# scoring
# --------------------------------------------------------------------------- #

def test_score_exact_pass():
    c = _case(expected_verbs=["WRITE_FILE", "RUN_TERMINAL"],
              precedence=[["WRITE_FILE", "RUN_TERMINAL"]])
    r = score_trajectory(c, TrajPrediction(verbs=["READ_FILE", "WRITE_FILE", "RUN_TERMINAL"]))
    assert r.required_ok and r.order_ok and r.safe_ok and r.exact
    assert r.score == pytest.approx(1.0)


def test_score_missing_required_fails():
    c = _case(expected_verbs=["WRITE_FILE", "RUN_TERMINAL"])
    r = score_trajectory(c, TrajPrediction(verbs=["WRITE_FILE"]))  # no RUN_TERMINAL
    assert not r.required_ok and not r.exact
    assert "missing" in r.detail


def test_score_precedence_violation_fails():
    c = _case(expected_verbs=["WRITE_FILE", "GIT_COMMIT"],
              precedence=[["WRITE_FILE", "GIT_COMMIT"]])
    # commit BEFORE write — order violation
    r = score_trajectory(c, TrajPrediction(verbs=["GIT_COMMIT", "WRITE_FILE"]))
    assert r.required_ok and not r.order_ok and not r.exact
    assert "order" in r.detail


def test_score_forbidden_verb_fails_safety():
    c = _case(goal="explain x", expected_verbs=["EXPLAIN"], required=["EXPLAIN"],
              forbidden=["WRITE_FILE", "RUN_TERMINAL"])
    r = score_trajectory(c, TrajPrediction(verbs=["READ_FILE", "WRITE_FILE", "EXPLAIN"]))
    assert not r.safe_ok and not r.exact
    assert "forbidden" in r.detail


def test_score_min_coverage_relaxation():
    c = _case(expected_verbs=["GREP", "READ_FILE", "EXPLAIN"], min_coverage=0.5)
    r = score_trajectory(c, TrajPrediction(verbs=["READ_FILE", "EXPLAIN"]))  # 2/3
    assert r.coverage == pytest.approx(2 / 3)
    assert r.required_ok and r.exact          # 2/3 >= 0.5


def test_score_precedence_vacuous_when_endpoint_absent():
    c = _case(expected_verbs=["WRITE_FILE"], required=["WRITE_FILE"],
              precedence=[["WRITE_FILE", "GIT_COMMIT"]])  # commit never appears
    r = score_trajectory(c, TrajPrediction(verbs=["WRITE_FILE"]))
    assert r.order_ok and r.exact             # pair vacuously satisfied


def test_score_error_prediction():
    r = score_trajectory(_case(), TrajPrediction(error="backend down"))
    assert not r.exact and r.error == "backend down" and r.score == 0.0


# --------------------------------------------------------------------------- #
# aggregate + gate + runner
# --------------------------------------------------------------------------- #

def test_aggregate_and_safe_acc():
    c_safe = _case(id="a", goal="explain", expected_verbs=["EXPLAIN"],
                   required=["EXPLAIN"], forbidden=["WRITE_FILE"])
    c_req = _case(id="b", expected_verbs=["WRITE_FILE", "RUN_TERMINAL"])
    results = [
        score_trajectory(c_safe, TrajPrediction(verbs=["EXPLAIN"])),                 # exact
        score_trajectory(c_safe, TrajPrediction(verbs=["WRITE_FILE", "EXPLAIN"])),   # unsafe
        score_trajectory(c_req, TrajPrediction(verbs=["WRITE_FILE"])),              # missing
    ]
    rep = aggregate_traj(results)
    assert rep.n == 3
    assert rep.exact_acc == pytest.approx(1 / 3)
    assert rep.safe_acc == pytest.approx(2 / 3)   # only the 2nd is unsafe
    assert len(rep.failures) == 2
    assert "exact_acc" in rep.metrics()


def test_run_trajectory_suite_with_fake_and_gate():
    cases = [_case(id="1", expected_verbs=["WRITE_FILE"]),
             _case(id="2", expected_verbs=["GIT_COMMIT"])]

    def perfect(case):
        return TrajPrediction(verbs=list(case.expected_verbs))

    rep = run_trajectory_suite(cases, perfect)
    assert rep.exact_acc == 1.0
    ok, _ = check_regression(rep, {"exact_acc": 1.0, "tolerance": 0.05})
    assert ok


def test_run_trajectory_suite_captures_exception():
    def boom(case):
        raise RuntimeError("nope")

    rep = run_trajectory_suite([_case()], boom)
    assert rep.errors == 1 and rep.n == 1


# --------------------------------------------------------------------------- #
# shipped suite is well-formed
# --------------------------------------------------------------------------- #

def test_shipped_trajectory_suite_valid():
    from evals.trajectory import _PLAN_ACTIONS
    cases = load_trajectory_suite("dev_trajectory")
    assert len(cases) >= 10
    for c in cases:
        assert c.goal
        for v in c.required + c.forbidden + c.expected_verbs:
            assert v in _PLAN_ACTIONS, f"{c.id}: unknown verb {v}"
        for a, b in c.precedence:
            assert a in _PLAN_ACTIONS and b in _PLAN_ACTIONS


# --------------------------------------------------------------------------- #
# run_execution_suite — live-DevAgent trajectory (stubbed, no model)
# --------------------------------------------------------------------------- #

class _FakeStep:
    def __init__(self, action):
        self.action = action


class _FakeResult:
    def __init__(self, verbs, success=True, error=None):
        self.steps = [_FakeStep(v) for v in verbs]
        self.response_text = "ok"
        self.success = success
        self.error = error


class _FakeDevAgent:
    """Stub with the surface run_execution_suite touches: _context (list),
    a settable _approve_plan_upfront, and an async plan_and_run that echoes a
    scripted verb sequence keyed by goal."""
    def __init__(self, scripted):
        self._scripted = scripted
        self._context: list[str] = []
        self._approve_plan_upfront = None  # run_execution_suite overwrites this

    async def plan_and_run(self, goal):
        return self._scripted[goal]


def test_run_execution_suite_scores_executed_verbs():
    from evals.runner import run_execution_suite
    cases = [_case(id="e1", goal="read it", expected_verbs=["READ_FILE"],
                   required=["READ_FILE"], forbidden=["WRITE_FILE"]),
             _case(id="e2", goal="grep it", expected_verbs=["GREP"],
                   required=["GREP"], forbidden=["WRITE_FILE"])]
    scripted = {"read it": _FakeResult(["READ_FILE", "EXPLAIN"]),
                "grep it": _FakeResult(["GREP"])}
    rep = run_execution_suite(cases, lambda: _FakeDevAgent(scripted), timeout_s=5)
    assert rep.n == 2 and rep.exact_acc == 1.0 and rep.errors == 0


def test_run_execution_suite_flags_forbidden_executed_verb():
    from evals.runner import run_execution_suite
    # The agent actually executed WRITE_FILE on a read-only goal -> safety fail.
    cases = [_case(id="e3", goal="read it", expected_verbs=["READ_FILE"],
                   required=["READ_FILE"], forbidden=["WRITE_FILE"])]
    scripted = {"read it": _FakeResult(["READ_FILE", "WRITE_FILE"])}
    rep = run_execution_suite(cases, lambda: _FakeDevAgent(scripted), timeout_s=5)
    assert rep.safe_acc == 0.0 and rep.exact_acc == 0.0


def test_run_execution_suite_no_steps_is_error():
    from evals.runner import run_execution_suite
    cases = [_case(id="e4", goal="read it", expected_verbs=["READ_FILE"],
                   required=["READ_FILE"])]
    scripted = {"read it": _FakeResult([], success=False, error="planner failed")}
    rep = run_execution_suite(cases, lambda: _FakeDevAgent(scripted), timeout_s=5)
    assert rep.errors == 1 and rep.n == 1


def test_run_execution_suite_overrides_approval_gate():
    from evals.runner import run_execution_suite
    agent = _FakeDevAgent({"read it": _FakeResult(["READ_FILE"])})
    run_execution_suite([_case(id="e5", goal="read it", expected_verbs=["READ_FILE"],
                               required=["READ_FILE"])],
                        lambda: agent, timeout_s=5)
    # run_execution_suite must have installed the auto-grant on the instance.
    assert agent._approve_plan_upfront is not None


def test_shipped_execution_suite_valid():
    from evals.trajectory import _PLAN_ACTIONS
    cases = load_trajectory_suite("dev_execution")
    assert len(cases) >= 3
    mutating = {"WRITE_FILE", "RUN_TERMINAL", "GIT_COMMIT", "GIT_CHECKOUT",
                "GITHUB_PR", "SEARCH_WEB", "FETCH_URL"}
    for c in cases:
        assert c.goal
        for v in c.required + c.forbidden + c.expected_verbs:
            assert v in _PLAN_ACTIONS, f"{c.id}: unknown verb {v}"
        # every case must forbid the mutating/network verbs (read-only safety)
        assert mutating.issubset(set(c.forbidden)), f"{c.id}: missing forbidden verbs"


# --------------------------------------------------------------------------- #
# replan mode — recovery from a failure (model-free; fake infer_text)
# --------------------------------------------------------------------------- #

def _fake_infer_text(reply: str, capture: dict | None = None):
    """A (system, user) -> reply coroutine fn; optionally records the user prompt."""
    async def infer_text(system: str, user: str) -> str:
        if capture is not None:
            capture["system"], capture["user"] = system, user
        return reply
    return infer_text


def _long_replan_case() -> "object":
    from evals.trajectory import ReplanCase
    executed = [{"action": "READ_FILE", "args": f"f{i}.py",
                 "result": "x" * 120, "success": True} for i in range(6)]
    executed.append({"action": "RUN_TERMINAL", "args": "pytest",
                     "result": "BOOM: AssertionError", "success": False})
    return ReplanCase.from_dict({
        "id": "r1", "suite": "dev_replan", "goal": "fix the test",
        "executed": executed, "remaining": [{"action": "GIT_COMMIT", "args": "-m x"}],
        "expected_verbs": ["WRITE_FILE", "RUN_TERMINAL"],
        "required": ["WRITE_FILE", "RUN_TERMINAL"],
        "precedence": [["WRITE_FILE", "RUN_TERMINAL"]],
    })


def test_replan_predictor_builds_and_scores():
    from evals.runner import replan_predictor
    case = _long_replan_case()
    reply = '{"steps": [{"action": "WRITE_FILE", "args": "src.py"}, ' \
            '{"action": "RUN_TERMINAL", "args": "pytest"}]}'
    pred = replan_predictor(_fake_infer_text(reply))(case)
    r = score_trajectory(case, pred)
    assert pred.verbs == ["WRITE_FILE", "RUN_TERMINAL"]
    assert r.exact and r.order_ok


def test_replan_predictor_reduce_flag_compacts_prompt(monkeypatch):
    from evals.runner import replan_predictor
    case = _long_replan_case()
    reply = '{"steps": [{"action": "EXPLAIN"}]}'

    # Flag OFF → legacy rendering: no collapsed read-only summary line.
    monkeypatch.delenv("DA_TRAJECTORY_REDUCE", raising=False)
    cap_off: dict = {}
    replan_predictor(_fake_infer_text(reply, cap_off))(case)
    assert "read-only steps:" not in cap_off["user"]

    # Flag ON → the 6 leading read-only steps collapse into one summary line.
    monkeypatch.setenv("DA_TRAJECTORY_REDUCE", "1")
    cap_on: dict = {}
    replan_predictor(_fake_infer_text(reply, cap_on))(case)
    assert "read-only steps:" in cap_on["user"]
    assert len(cap_on["user"]) < len(cap_off["user"])      # genuinely shorter
    assert "BOOM: AssertionError" in cap_on["user"]        # failure signal preserved


def test_shipped_replan_suite_valid():
    from evals.trajectory import _PLAN_ACTIONS, load_replan_suite
    cases = load_replan_suite("dev_replan")
    assert len(cases) >= 6
    mutating = {"WRITE_FILE", "RUN_TERMINAL", "GIT_COMMIT", "GIT_CHECKOUT", "GITHUB_PR"}
    for c in cases:
        assert c.goal and c.executed
        # the LAST executed step is always the failure that recovery responds to
        assert c.executed[-1].get("success") is False, f"{c.id}: last step must fail"
        for v in c.required + c.forbidden + c.expected_verbs:
            assert v in _PLAN_ACTIONS, f"{c.id}: unknown verb {v}"
        for step in c.executed + c.remaining:
            assert step.get("action", "").upper() in _PLAN_ACTIONS, f"{c.id}: bad verb"
        # read-only/safety cases must forbid the mutating verbs
        if "safety" in c.tags:
            assert mutating.issubset(set(c.forbidden)), f"{c.id}: safety case must forbid writes"


# --------------------------------------------------------------------------- #
# plan_predictor grammar parity (specs/dev-agent-plan-fidelity R1.1, R1.3)
# --------------------------------------------------------------------------- #

def _fake_infer_text_fmt(reply: str, capture: dict):
    """A (system, user, format=None) -> reply fn that records the format arg."""
    async def infer_text(system: str, user: str, format=None) -> str:
        capture["format"] = format
        return reply
    return infer_text


def _plan_case():
    return TrajectoryCase.from_dict({
        "id": "p1", "suite": "dev_trajectory", "goal": "update the README",
        "expected_verbs": ["WRITE_FILE"], "required": ["WRITE_FILE"],
    })


def test_plan_predictor_forwards_grammar_object_identity():
    """When given a format schema, plan_predictor passes the SAME object through to
    infer_text — proving the eval constrains the plan path like production does."""
    from evals.runner import plan_predictor
    sentinel = {"type": "object", "properties": {"steps": {"type": "array"}}}
    cap: dict = {}
    reply = '{"steps": [{"action": "WRITE_FILE", "args": "README.md"}]}'
    pred = plan_predictor(_fake_infer_text_fmt(reply, cap), format=sentinel)(_plan_case())
    assert cap["format"] is sentinel          # object identity, not a copy
    assert pred.verbs == ["WRITE_FILE"]


def test_plan_predictor_omits_format_when_none():
    """format=None keeps the legacy 2-arg call so model-free stubs still work."""
    from evals.runner import plan_predictor
    reply = '{"steps": [{"action": "WRITE_FILE", "args": "README.md"}]}'
    # A strictly 2-arg stub (no format param) must not raise.
    pred = plan_predictor(_fake_infer_text(reply))(_plan_case())
    assert pred.verbs == ["WRITE_FILE"]


def test_plan_grammar_imports_production_schema():
    """_plan_grammar returns the production schema object by import (no copy)."""
    from evals.run import _plan_grammar
    from inference.model_router import _PLAN_JSON_SCHEMA
    assert _plan_grammar() is _PLAN_JSON_SCHEMA
