"""Eval-harness logic tests — scoring, parsing, corpus IO, harvest, runner, gate.

These exercise the harness WITHOUT a model (predictors are fakes), so they run in
CI and never hang. The model-backed path lives in evals/run.py and is exercised
manually against the live local model.

Run: python -m pytest tests/test_evals.py -q
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from evals.corpus import EvalCase, load_suite, save_suite, harvest_from_agent_db
from evals.scoring import (
    Prediction, parse_action_string, score_case, aggregate, check_regression,
)
from evals.runner import run_suite


# --------------------------------------------------------------------------- #
# parse_action_string (mirrors ActionExecutor.parse_action)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("raw,verb,target", [
    ("CLICK save button", "CLICK", "save button"),
    ("Action: OPEN Chrome", "OPEN", "Chrome"),
    ("command: CLOSE", "CLOSE", ""),
    ("SCREENSHOT", "SCREENSHOT", ""),
    ("CLICK:", "CLICK", ""),
    ("", "", ""),
])
def test_parse_action_string(raw, verb, target):
    v, slots = parse_action_string(raw)
    assert v == verb
    assert slots.get("target", "") == target


# --------------------------------------------------------------------------- #
# scoring
# --------------------------------------------------------------------------- #

def _case(verb="CLICK", slots=None, **kw):
    return EvalCase(id=kw.get("id", "c1"), suite="t", utterance=kw.get("u", "x"),
                    expected_verb=verb, expected_slots=slots or {})


def test_score_exact_match():
    r = score_case(_case("CLICK", {"target": "save button"}),
                   Prediction(verb="CLICK", slots={"target": "the save button"}))
    assert r.verb_ok and r.slots_ok and r.exact


def test_score_verb_only_when_slot_wrong():
    r = score_case(_case("CLICK", {"target": "save"}),
                   Prediction(verb="CLICK", slots={"target": "cancel"}))
    assert r.verb_ok and not r.slots_ok and not r.exact


def test_score_empty_expected_slots_trivially_ok():
    r = score_case(_case("SCREENSHOT", {}), Prediction(verb="SCREENSHOT"))
    assert r.exact


def test_score_severity_is_int_compared():
    ok = score_case(_case("log_symptom", {"area": "hands", "severity": 6}),
                    Prediction(verb="log_symptom", slots={"area": "hands", "severity": 6}))
    assert ok.exact
    bad = score_case(_case("log_symptom", {"area": "hands", "severity": 6}),
                     Prediction(verb="log_symptom", slots={"area": "hands", "severity": 7}))
    assert bad.verb_ok and not bad.exact


def test_aggregate_metrics_and_by_verb():
    results = [
        score_case(_case("CLICK", {"target": "a"}, id="1"),
                   Prediction("CLICK", {"target": "a"}, latency_ms=10)),
        score_case(_case("CLICK", {"target": "b"}, id="2"),
                   Prediction("CLICK", {"target": "x"}, latency_ms=20)),  # slot miss
        score_case(_case("OPEN", {"target": "chrome"}, id="3"),
                   Prediction("CLOSE", latency_ms=30)),                   # verb miss
    ]
    rep = aggregate(results)
    assert rep.n == 3
    assert rep.verb_acc == pytest.approx(2 / 3)
    assert rep.exact_acc == pytest.approx(1 / 3)
    assert rep.p50_latency_ms == 20
    assert rep.by_verb["CLICK"]["exact"] == 1
    assert len(rep.failures) == 2


def test_aggregate_empty():
    rep = aggregate([])
    assert rep.n == 0 and rep.exact_acc == 0.0


# --------------------------------------------------------------------------- #
# regression gate
# --------------------------------------------------------------------------- #

def test_check_regression_unset_baseline_is_informational():
    ok, msg = check_regression(aggregate([]), {"exact_acc": None})
    assert ok and "no baseline" in msg.lower()


def test_check_regression_passes_within_tolerance():
    rep = aggregate([score_case(_case("CLICK", {"target": "a"}),
                                Prediction("CLICK", {"target": "a"}))])  # 100%
    ok, _ = check_regression(rep, {"exact_acc": 1.0, "tolerance": 0.05})
    assert ok


def test_check_regression_fails_on_drop():
    rep = aggregate([
        score_case(_case("CLICK", id="1"), Prediction("CLOSE")),  # fail
        score_case(_case("CLICK", id="2"), Prediction("CLICK")),  # pass
    ])  # exact_acc 0.5
    ok, msg = check_regression(rep, {"exact_acc": 0.95, "tolerance": 0.05})
    assert not ok and "REGRESSION" in msg


# --------------------------------------------------------------------------- #
# corpus IO + runner
# --------------------------------------------------------------------------- #

def test_suite_roundtrip(tmp_path):
    cases = [_case("CLICK", {"target": "save"}, id="a"),
             _case("OPEN", {"target": "chrome"}, id="b")]
    p = tmp_path / "s.jsonl"
    save_suite(cases, p)
    back = load_suite(p)
    assert [c.id for c in back] == ["a", "b"]
    assert back[0].expected_slots == {"target": "save"}


def test_shipped_command_suite_loads_and_is_valid():
    cases = load_suite("command_verbs")
    assert len(cases) >= 12
    valid = {"CLICK", "SCROLL", "TYPE", "OPEN", "CLOSE", "HOTKEY", "DICTATE",
             "CLARIFY", "SCREENSHOT", "MOUSEDOWN", "MOUSEUP"}
    for c in cases:
        assert c.expected_verb in valid, f"{c.id}: bad verb {c.expected_verb}"


def test_run_suite_with_fake_predictor():
    cases = [_case("CLICK", {"target": "save"}, id="1"),
             _case("OPEN", {"target": "chrome"}, id="2")]

    def fake(case):  # perfect oracle
        return Prediction(verb=case.expected_verb, slots=dict(case.expected_slots))

    rep = run_suite(cases, fake)
    assert rep.exact_acc == 1.0


def test_run_suite_captures_predictor_exception():
    def boom(case):
        raise RuntimeError("backend down")

    rep = run_suite([_case()], boom)
    assert rep.errors == 1 and rep.n == 1


def test_command_predictor_flags_backend_down_as_error():
    # OllamaInference degrades to a "CLARIFY inference ..." string instead of
    # raising; the predictor must record that as an ERROR, not a real CLARIFY,
    # so an all-down run trips run.py's unreachable fail-safe (exit 2).
    from evals.runner import command_predictor

    async def down_infer(cmd):
        return "CLARIFY inference backend unavailable (circuit open)"

    pred = command_predictor(down_infer)(_case())
    assert pred.error and pred.verb == ""

    async def real_clarify(cmd):
        return "CLARIFY which save button did you mean"

    pred2 = command_predictor(real_clarify)(_case())
    assert not pred2.error and pred2.verb == "CLARIFY"


# --------------------------------------------------------------------------- #
# harvest gold cases from a synthetic agent.db
# --------------------------------------------------------------------------- #

def _seed_db(path):
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE commands (id INTEGER PRIMARY KEY, ts REAL, source TEXT, "
                 "text TEXT, action TEXT, success INTEGER, corrected_to TEXT)")
    conn.executemany(
        "INSERT INTO commands (ts, source, text, action, success, corrected_to) "
        "VALUES (?,?,?,?,?,?)",
        [
            (1.0, "voice", "scroll done", "TYPE done", 0, "SCROLL down"),   # gold
            (2.0, "voice", "clothes the window", "TYPE", 0, "CLOSE"),       # gold
            (3.0, "voice", "open chrome", "OPEN Chrome", 1, None),          # silver only
        ],
    )
    conn.commit()
    conn.close()


def test_harvest_gold_cases(tmp_path):
    db = tmp_path / "agent.db"
    _seed_db(db)
    gold = harvest_from_agent_db(db)
    assert {c.id for c in gold} == {"gold-1", "gold-2"}
    g1 = next(c for c in gold if c.id == "gold-1")
    assert g1.utterance == "scroll done"
    assert g1.expected_verb == "SCROLL" and g1.expected_slots == {"target": "down"}
    assert g1.source == "gold"


def test_harvest_includes_silver_when_requested(tmp_path):
    db = tmp_path / "agent.db"
    _seed_db(db)
    cases = harvest_from_agent_db(db, include_silver=True)
    assert any(c.id == "silver-3" and c.expected_verb == "OPEN" for c in cases)


def test_harvest_missing_db_is_safe(tmp_path):
    assert harvest_from_agent_db(tmp_path / "nope.db") == []
