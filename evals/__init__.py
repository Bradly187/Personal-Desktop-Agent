"""Behavioral eval harness for the agent's LLM-judgment surfaces.

The pytest suite verifies the *plumbing* around the model (it mocks inference).
This package verifies the model's *decisions*: given an utterance (+ context),
does it produce the right verb and slots? It is the missing left half of the
quality flywheel — a standing, versioned benchmark that communicates intent and
gates change — seeded from the production corpus we already collect in agent.db
(commands.corrected_to is gold-standard intent).

Layout:
    corpus.py   — EvalCase + JSONL IO + harvest_from_agent_db (gold cases)
    scoring.py  — parse an action string, score a prediction, aggregate a Report
    runner.py   — run a suite against an injected predict_fn (real model or fake)
    run.py      — CLI: run a suite vs the live local model + regression-lock check
    suites/     — versioned labelled cases (JSONL)
    baselines/  — locked accuracy baselines (regression gate)

The model is touched ONLY by run.py / the predictor factories. corpus/scoring/
runner are pure stdlib so the harness logic is itself unit-tested without a GPU
(tests/test_evals.py) and can never wedge CI the way a live-service test can.
"""

from evals.corpus import EvalCase, load_suite, save_suite, harvest_from_agent_db
from evals.scoring import (
    Prediction,
    CaseResult,
    Report,
    parse_action_string,
    score_case,
    aggregate,
)
from evals.trajectory import (
    TrajectoryCase,
    TrajPrediction,
    TrajResult,
    TrajReport,
    load_trajectory_suite,
    extract_plan_verbs,
    score_trajectory,
    aggregate_traj,
)
from evals.judge import (
    Criterion,
    JudgeCase,
    Verdict,
    JudgeResult,
    JudgeReport,
    load_judge_suite,
    build_judge_prompt,
    parse_verdict,
    score_judge,
    aggregate_judge,
)

__all__ = [
    # output evals (utterance -> verb/slots)
    "EvalCase",
    "load_suite",
    "save_suite",
    "harvest_from_agent_db",
    "Prediction",
    "CaseResult",
    "Report",
    "parse_action_string",
    "score_case",
    "aggregate",
    # trajectory evals (goal -> plan path)
    "TrajectoryCase",
    "TrajPrediction",
    "TrajResult",
    "TrajReport",
    "load_trajectory_suite",
    "extract_plan_verbs",
    "score_trajectory",
    "aggregate_traj",
    # LM-as-judge evals (free-form answer -> rubric verdict)
    "Criterion",
    "JudgeCase",
    "Verdict",
    "JudgeResult",
    "JudgeReport",
    "load_judge_suite",
    "build_judge_prompt",
    "parse_verdict",
    "score_judge",
    "aggregate_judge",
]
