"""Run a suite against a predict_fn, and factories that build real-model predictors.

`run_suite` is predict_fn-agnostic: tests pass a fake `(case) -> Prediction`; the
CLI passes a model-backed one. Every prediction is guarded — one failing case
becomes a CaseResult(error=...), never an abort — and the model factories bound
each call with asyncio.wait_for so the harness can never wedge.
"""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable

from evals.scoring import (
    Prediction,
    CaseResult,
    Report,
    parse_action_string,
    score_case,
    aggregate,
)
from evals.trajectory import (
    TrajPrediction,
    TrajReport,
    extract_plan_verbs,
    score_trajectory,
    aggregate_traj,
)
from evals.judge import (
    Verdict,
    JudgeReport,
    build_judge_prompt,
    parse_verdict,
    score_judge,
    aggregate_judge,
)

PredictFn = Callable[[object], Prediction]
InferFn = Callable[[object], Awaitable[str]]   # (Command) -> action string
TextFn = Callable[[str, str], Awaitable[str]]  # (system, user) -> reply

# Backend-down sentinels returned by LocalInference (it degrades to a string
# rather than raising). These are NOT a genuine model CLARIFY — a run made of
# them means the backend is unreachable, not that the model chose to clarify.
_BACKEND_ERROR_PREFIXES = (
    "clarify inference",   # "CLARIFY inference backend unavailable" / "... error: ..."
    "clarify vllm",        # "CLARIFY vllm unavailable" / "CLARIFY vLLM server error"
    "clarify aiohttp",     # "CLARIFY aiohttp not installed"
)


def _is_backend_error(raw: str) -> bool:
    return (raw or "").strip().lower().startswith(_BACKEND_ERROR_PREFIXES)


def run_suite(cases: list, predict_fn: PredictFn) -> Report:
    """Score every case with predict_fn and aggregate. Exceptions are captured."""
    results: list[CaseResult] = []
    for case in cases:
        try:
            pred = predict_fn(case)
        except Exception as exc:  # pragma: no cover - defensive
            pred = Prediction(error=f"{type(exc).__name__}: {exc}")
        results.append(score_case(case, pred))
    return aggregate(results)


# --------------------------------------------------------------------------- #
# Real-model predictors (used by run.py; the model is injected, not imported here)
# --------------------------------------------------------------------------- #

_SLOT_EXTRACT_SYSTEM = (
    "Extract the body area and the pain severity from the user's note. "
    "Severity is an integer 0-10 (map 'six out of ten' -> 6, 'an eight' -> 8). "
    "Reply with EXACTLY one line: area=<area> severity=<n>. No other text."
)


def command_predictor(infer: InferFn, *, timeout_s: float = 20.0) -> PredictFn:
    """Production-accurate command predictor: run the local model on the utterance
    and parse its action string exactly as the coordinator would."""
    from core.command_executor import Command

    def predict(case) -> Prediction:
        cmd = Command(
            text=case.utterance, action="", source="voice",
            session_context=list(getattr(case, "context", []) or []),
        )
        t0 = time.monotonic()
        try:
            raw = asyncio.run(asyncio.wait_for(infer(cmd), timeout_s))
        except Exception as exc:
            return Prediction(error=f"{type(exc).__name__}: {exc}",
                              latency_ms=(time.monotonic() - t0) * 1000)
        lat = (time.monotonic() - t0) * 1000
        if _is_backend_error(raw):
            return Prediction(raw=raw, error=raw.strip(), latency_ms=lat)
        verb, slots = parse_action_string(raw)
        return Prediction(verb=verb, slots=slots, raw=raw, latency_ms=lat)

    return predict


def slot_predictor(infer_text: Callable[[str, str], Awaitable[str]],
                   *, timeout_s: float = 20.0) -> PredictFn:
    """Approximates the planner's slot extraction for pain_journal (area/severity).

    NOTE: production extracts these via DevAgent.plan_and_run -> SKILL args; this
    focused-prompt proxy isolates the *extraction* judgment for a cheap standing
    eval. `infer_text(system, user) -> str` returns the model's raw reply.
    """
    import re as _re

    def predict(case) -> Prediction:
        t0 = time.monotonic()
        try:
            raw = asyncio.run(asyncio.wait_for(
                infer_text(_SLOT_EXTRACT_SYSTEM, case.utterance), timeout_s))
        except Exception as exc:
            return Prediction(error=f"{type(exc).__name__}: {exc}",
                              latency_ms=(time.monotonic() - t0) * 1000)
        slots: dict = {}
        m_area = _re.search(r"area\s*=\s*([A-Za-z ]+)", raw)
        m_sev = _re.search(r"severity\s*=\s*(\d+)", raw)
        if m_area:
            slots["area"] = m_area.group(1).strip()
        if m_sev:
            slots["severity"] = int(m_sev.group(1))
        return Prediction(verb=case.expected_verb, slots=slots, raw=raw,
                          latency_ms=(time.monotonic() - t0) * 1000)

    return predict


# --------------------------------------------------------------------------- #
# Router evals — score the DomainClassifier's domain selection (MODEL-FREE)
# --------------------------------------------------------------------------- #

def _register_skill_keywords_from_manifests(cls) -> None:
    """Load every shipped skill manifest's intent keywords and register them, so the
    router eval exercises skill-domain classification regardless of which skills are
    currently enabled at runtime (we gate the classification LOGIC, not user state)."""
    import json
    from pathlib import Path

    kws: set[str] = set()
    mdir = Path(__file__).parent.parent / "skills" / "manifests"
    if not mdir.exists():
        return
    for f in mdir.glob("*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        for intent in (d.get("intents") or {}).values():
            for kw in (intent.get("keywords") or []):
                kws.add(str(kw).lower())
    if kws:
        cls.register_skill_keywords(kws)


def router_predictor(*, register_skills: bool = True) -> PredictFn:
    """Build a MODEL-FREE predict_fn over the deterministic DomainClassifier. The
    case's `expected_verb` holds the expected DOMAIN (command/code/math/vision/plan/
    general/skill); the prediction's `verb` is the classified domain, so the regular
    scoring/aggregate (incl. per-domain `by_verb`) applies unchanged."""
    from core.domain_classifier import DomainClassifier

    if register_skills:
        _register_skill_keywords_from_manifests(DomainClassifier)
    clf = DomainClassifier()

    def predict(case) -> Prediction:
        t0 = time.monotonic()
        domain = clf.classify(case.utterance)
        return Prediction(verb=domain, raw=domain,
                          latency_ms=(time.monotonic() - t0) * 1000)

    return predict


# --------------------------------------------------------------------------- #
# Trajectory evals — score a multi-step plan against an expected path
# --------------------------------------------------------------------------- #

# Fallback plan prompt — only used if the production prompt can't be imported (so
# the evals package never hard-depends on the inference stack). The REAL eval uses
# the production _PLAN_PROMPT (see _plan_system below), so tightening the production
# prompt directly moves the trajectory score — a true closed loop, not a proxy.
_PLAN_SYSTEM_FALLBACK = (
    "You are a software agent's planner. Output a numbered, ordered plan to achieve "
    "the goal. ONE step per line as 'N. VERB args', optionally '(after: K)' to mark "
    "a dependency. Use ONLY these verbs: READ_FILE, GREP, WRITE_FILE, RUN_TERMINAL, "
    "GIT_STATUS, GIT_DIFF, GIT_COMMIT, GIT_CHECKOUT, GITHUB_PR, SEARCH_WEB, "
    "FETCH_URL, SKILL_QUERY, SKILL_CALL, SEARCH_PERSONAL, EXPLAIN. Least privilege: "
    "use only read-only verbs for an explain/find/show goal; never commit unless "
    "asked. No prose outside the numbered steps."
)


def _plan_system() -> str:
    """The planner system prompt the eval scores against. Prefers the live
    production prompt (so the eval tracks the real agent); falls back to a minimal
    equivalent if the inference package is unavailable."""
    try:
        from inference.model_router import _PLAN_PROMPT
        return _PLAN_PROMPT
    except Exception:
        return _PLAN_SYSTEM_FALLBACK


def run_trajectory_suite(cases: list, plan_fn) -> TrajReport:
    """Score every trajectory case with plan_fn: (case) -> TrajPrediction."""
    results = []
    for case in cases:
        try:
            pred = plan_fn(case)
        except Exception as exc:  # pragma: no cover - defensive
            pred = TrajPrediction(error=f"{type(exc).__name__}: {exc}")
        results.append(score_trajectory(case, pred))
    return aggregate_traj(results)


def plan_predictor(infer_text: TextFn, *, timeout_s: float = 30.0):
    """Build a plan_fn: ask the model to plan the goal, extract the verb sequence
    exactly as dev_agent would parse it."""
    system = _plan_system()

    def predict(case) -> TrajPrediction:
        ctx = "\n".join(getattr(case, "context", []) or [])
        user = (f"Context:\n{ctx}\n\nGoal: {case.goal}" if ctx else f"Goal: {case.goal}")
        t0 = time.monotonic()
        try:
            raw = asyncio.run(asyncio.wait_for(
                infer_text(system, user), timeout_s))
        except Exception as exc:
            return TrajPrediction(error=f"{type(exc).__name__}: {exc}",
                                  latency_ms=(time.monotonic() - t0) * 1000)
        lat = (time.monotonic() - t0) * 1000
        if _is_backend_error(raw):
            return TrajPrediction(raw=raw, error=raw.strip(), latency_ms=lat)
        return TrajPrediction(verbs=extract_plan_verbs(raw), raw=raw, latency_ms=lat)

    return predict


# --------------------------------------------------------------------------- #
# LM-as-judge evals — score a free-form answer against a rubric
# --------------------------------------------------------------------------- #

def run_judge_suite(cases: list, judge_fn, produce_fn=None) -> JudgeReport:
    """For each case: get the answer (case.output or produce_fn(case)), then judge
    it with judge_fn: (case, output) -> Verdict. A producer/judge exception becomes
    a recorded error (passed=False), never an abort."""
    results = []
    for case in cases:
        try:
            output = case.output or (produce_fn(case) if produce_fn else "")
        except Exception as exc:  # pragma: no cover - defensive
            results.append(score_judge(case, Verdict(error=f"producer: {exc}")))
            continue
        if not output:
            results.append(score_judge(case, Verdict(error="no output to judge")))
            continue
        try:
            verdict = judge_fn(case, output)
        except Exception as exc:  # pragma: no cover - defensive
            verdict = Verdict(error=f"judge: {type(exc).__name__}: {exc}")
        results.append(score_judge(case, verdict))
    return aggregate_judge(results)


def answer_producer(infer_text: TextFn, *, timeout_s: float = 30.0):
    """Build a produce_fn: answer the case prompt with the live model (this is what
    the judge then scores, so the eval gates the AGENT, not just the judge)."""
    _SYS = ("You are a helpful, precise assistant. Answer the question directly and "
            "concisely. If you are not sure or lack the needed context, say so "
            "rather than guessing.")

    def produce(case) -> str:
        ctx = "\n".join(getattr(case, "context", []) or [])
        user = (f"{ctx}\n\n{case.prompt}" if ctx else case.prompt)
        raw = asyncio.run(asyncio.wait_for(infer_text(_SYS, user), timeout_s))
        if _is_backend_error(raw):
            raise RuntimeError(raw.strip())
        return raw

    return produce


def llm_judge(infer_text: TextFn, *, timeout_s: float = 30.0):
    """Build a judge_fn: (case, output) -> Verdict using the model as a rubric judge."""
    def judge(case, output: str) -> Verdict:
        system, user = build_judge_prompt(case, output)
        t0 = time.monotonic()
        try:
            raw = asyncio.run(asyncio.wait_for(infer_text(system, user), timeout_s))
        except Exception as exc:
            return Verdict(error=f"{type(exc).__name__}: {exc}",
                           latency_ms=(time.monotonic() - t0) * 1000)
        lat = (time.monotonic() - t0) * 1000
        if _is_backend_error(raw):
            return Verdict(error=raw.strip(), latency_ms=lat)
        v = parse_verdict(raw, case)
        v.latency_ms = lat
        return v

    return judge
