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
# Skill-trigger evals — does an utterance fire the RIGHT skill? (MODEL-FREE)
# --------------------------------------------------------------------------- #

def skill_trigger_predictor(manifest_dir=None) -> PredictFn:
    """Build a MODEL-FREE predict_fn over the REAL runtime skill router.

    The whitepaper's first eval gate is the *trigger*: the right skill must fire on
    its phrasings and stay quiet otherwise. We exercise the production routing logic
    by populating a `SkillRegistry` straight from the shipped manifests — its `tools`
    are the manifest's declared `allow`-list (no MCP servers started) — and calling
    the real `SkillRegistry.match_intent`. So this eval tracks the actual router, not
    a copy: editing `match_intent` or a manifest's keywords moves the score.

    Every manifest is loaded regardless of its `enabled` flag (we gate the routing
    LOGIC, not the user's runtime state — mirrors `router_predictor`). A case's
    `expected_verb` holds the expected `skill_id`, or "none" when nothing should fire.
    """
    import json
    from pathlib import Path
    from skills.registry import SkillRegistry, _Skill

    mdir = (Path(manifest_dir) if manifest_dir
            else Path(__file__).parent.parent / "skills" / "manifests")
    reg = SkillRegistry(manifest_dir=mdir)
    if mdir.exists():
        for f in sorted(mdir.glob("*.json")):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            sid = d.get("skill_id")
            if not sid:
                continue
            allow = (d.get("tools") or {}).get("allow") or []
            # session/lock are unused by match_intent; tools is the allow-list so the
            # intent->tool availability check matches production routing.
            reg._skills[sid] = _Skill(
                manifest=d, session=None, tools={t: None for t in allow}, lock=None)

    def predict(case) -> Prediction:
        t0 = time.monotonic()
        m = reg.match_intent(case.utterance)
        verb = m["skill_id"] if m else "none"
        return Prediction(verb=verb, raw=((m or {}).get("intent") or "none"),
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


def run_execution_suite(cases: list, build_dev_agent, *, timeout_s: float = 120.0) -> TrajReport:
    """Score trajectory cases by running the goal through the LIVE DevAgent.

    Unlike `plan_predictor` (which only scores the planner *prompt's* output),
    this builds a real `DevAgent` and runs `plan_and_run(goal)`, scoring the verb
    sequence that was *actually executed* (`AgentResult.steps`). This closes the
    verify loop — it catches planner→executor drift the prompt-only eval can't.

    Single event loop by design: `DevAgent` holds asyncio primitives created in
    `__init__`, which bind to the first loop they touch. One `DevAgent` on one
    `asyncio.run` keeps them on a single loop; a per-case `asyncio.run` would
    rebind and raise. `build_dev_agent` is a zero-arg factory (constructed on the
    eval loop so its locks bind here).

    Safety: this is for STRICTLY READ-ONLY suites (each case forbids mutating
    verbs; `score_trajectory.safe_ok` fails the case if any forbidden verb runs).
    The interactive plan-approval gate (TTS + a 7s wait) is replaced with the
    read-only "auto" grant so the suite runs unattended and silent.
    """
    async def _run_all() -> TrajReport:
        dev = build_dev_agent()

        async def _auto_approve(goal: str, steps) -> str:  # read-only convenience grant
            return "auto"
        dev._approve_plan_upfront = _auto_approve   # type: ignore[attr-defined]

        results = []
        for case in cases:
            try:
                dev._context.clear()                 # isolate cases from each other
            except Exception:
                pass
            t0 = time.monotonic()
            try:
                res = await asyncio.wait_for(dev.plan_and_run(case.goal), timeout_s)
                lat = (time.monotonic() - t0) * 1000
                verbs = [s.action.upper() for s in (getattr(res, "steps", None) or [])]
                if verbs:
                    # Score the executed trajectory regardless of overall success:
                    # a "failed" plan that still ran the right read-only steps is a
                    # pass for trajectory purposes; only a no-step run is an error.
                    pred = TrajPrediction(verbs=verbs, raw=getattr(res, "response_text", "") or "",
                                          latency_ms=lat)
                else:
                    pred = TrajPrediction(error=(getattr(res, "error", None) or "no steps executed"),
                                          latency_ms=lat)
            except Exception as exc:
                pred = TrajPrediction(error=f"{type(exc).__name__}: {exc}",
                                      latency_ms=(time.monotonic() - t0) * 1000)
            results.append(score_trajectory(case, pred))
        return aggregate_traj(results)

    return asyncio.run(_run_all())


async def _capture_executed_text(dev, goal: str, timeout_s: float):
    """Run a goal through the live DevAgent and return (text, latency_ms), where
    text is everything the user effectively sees — every executed step's result
    plus the final reflection. On failure/timeout the text is an __ERROR__ sentinel
    so the ablation scorer can mark the case errored rather than scoring noise."""
    try:
        dev._context.clear()
    except Exception:
        pass
    t0 = time.monotonic()
    try:
        res = await asyncio.wait_for(dev.plan_and_run(goal), timeout_s)
        lat = (time.monotonic() - t0) * 1000
        parts = [(s.result or "") for s in (getattr(res, "steps", None) or [])]
        parts.append(getattr(res, "response_text", "") or "")
        return "\n".join(p for p in parts if p), lat
    except Exception as exc:
        return f"__ERROR__ {type(exc).__name__}: {exc}", (time.monotonic() - t0) * 1000


def run_ablation_suite(cases: list, build_no_rag, build_with_rag, *,
                       timeout_s: float = 150.0):
    """RAG ablation: run each goal through the live DevAgent WITH vs WITHOUT the
    codebase indexer and score grounding (anchor-term recall) for each.

    build_no_rag / build_with_rag are zero-arg factories (sync or async) that each
    return a ready DevAgent — built on the eval loop so their asyncio primitives
    bind here (one loop, two agents). The interactive plan-approval gate is replaced
    with the read-only 'auto' grant so the suite runs unattended. Returns an
    AblationReport whose headline is mean Δ (with − without grounding).
    """
    from evals.ablation import score_ablation, aggregate_ablation

    async def _maybe(x):
        return await x if asyncio.iscoroutine(x) else x

    async def _run_all():
        dev_no = await _maybe(build_no_rag())
        dev_with = await _maybe(build_with_rag())
        for dev in (dev_no, dev_with):
            try:
                async def _auto(goal, steps):
                    return "auto"
                dev._approve_plan_upfront = _auto   # type: ignore[attr-defined]
            except Exception:
                pass
        results = []
        for case in cases:
            with_text, wl = await _capture_executed_text(dev_with, case.goal, timeout_s)
            no_text, nl = await _capture_executed_text(dev_no, case.goal, timeout_s)
            results.append(score_ablation(case, with_text, no_text,
                                          with_latency_ms=wl, no_latency_ms=nl))
        return aggregate_ablation(results)

    return asyncio.run(_run_all())


# --------------------------------------------------------------------------- #
# Edit-format A/B (specs/edit-format-aci task 6)
# --------------------------------------------------------------------------- #

def _strip_code_fences(text: str) -> str:
    """Drop a leading/trailing ```lang fence the model wrapped its answer in.

    Models reflexively fence file content; the whole_file applier writes the body
    verbatim, so an un-stripped fence would corrupt every file. Only strips when
    the whole reply is one fenced block (conservative)."""
    s = (text or "").strip()
    if not s.startswith("```"):
        return text
    lines = s.split("\n")
    # first line is ``` or ```python; drop it and a trailing ``` if present.
    lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines)


def _edit_ab_prompts(arm: str, path: str, base_text: str, instruction: str):
    """Build the (system, user) prompt for one arm, reusing the REAL production
    instruction text (the whole_file convention and HASHLINE_PROMPT_INSTRUCTIONS)
    so the eval measures the format the live DevAgent actually ships."""
    from inference.edit_format import HASHLINE, render_hashline, HASHLINE_PROMPT_INSTRUCTIONS

    if arm == HASHLINE:
        system = (
            HASHLINE_PROMPT_INSTRUCTIONS
            + "\n\nOutput ONLY the @@ edit ops (and their content lines). "
            "No prose, no markdown fences, no JSON."
        )
        user = (
            f"File: {path} (each line shown as `lineno:hash|content`):\n\n"
            f"{render_hashline(base_text)}\n\n"
            f"Task: {instruction}\n\n"
            f"Return only the @@ edit ops needed."
        )
    else:  # whole_file (legacy default)
        system = (
            "You are editing a source file. Output the COMPLETE updated contents "
            "of the file and nothing else — every line that should remain must be "
            "present. No explanations, no markdown fences, no JSON."
        )
        user = (
            f"File: {path}\n\n{base_text}\n\n"
            f"Task: {instruction}\n\n"
            f"Return the complete updated file."
        )
    return system, user


def run_edit_ab_suite(cases: list, infer_text: TextFn, *, arms=None,
                      timeout_s: float = 90.0):
    """Edit-format A/B: hold the model fixed, swap only the WRITE_FILE format.

    For each case and each arm (whole_file, hashline) this prompts the SAME model
    with that format's real production instructions, then applies the reply through
    the production ``EditApplier`` (which also runs the lint gate). Scoring is
    deterministic (``evals.edit_ab.score_edit_ab``). Returns an EditABReport whose
    headline is the per-arm correct-rate and the hashline-minus-whole_file delta.

    Fails safe like the other model-backed runners: a model/backend error on an
    arm becomes that arm's apply_error, and an all-errored run is reported (the CLI
    turns it into an exit-2 skip) rather than a false baseline.
    """
    from inference.edit_format import EditApplier, EditError
    from evals.edit_ab import ArmOutcome, score_edit_ab, aggregate_edit_ab

    arm_list = list(arms) if arms else ["whole_file", "hashline"]
    applier = EditApplier()

    async def _run_arm(case, arm) -> ArmOutcome:
        path = str(case.fixture_path())
        base = case.base_text()
        system, user = _edit_ab_prompts(arm, path, base, case.instruction)
        t0 = time.monotonic()
        try:
            reply = await asyncio.wait_for(infer_text(system, user), timeout_s)
        except Exception as exc:
            return ArmOutcome(arm=arm, ok=False, reason="io",
                              message=f"{type(exc).__name__}: {exc}",
                              latency_ms=(time.monotonic() - t0) * 1000)
        lat = (time.monotonic() - t0) * 1000
        if _is_backend_error(reply):
            return ArmOutcome(arm=arm, ok=False, reason="io",
                              message=reply.strip()[:120], latency_ms=lat)
        body = _strip_code_fences(reply)
        try:
            new_text = applier.apply(base, body, edit_format=arm, path=path)
            return ArmOutcome(arm=arm, ok=True, text=new_text,
                              body_len=len(body), latency_ms=lat)
        except EditError as ee:
            return ArmOutcome(arm=arm, ok=False, reason=ee.reason,
                              message=str(ee), body_len=len(body), latency_ms=lat)
        except Exception as exc:
            return ArmOutcome(arm=arm, ok=False, reason="error",
                              message=f"{type(exc).__name__}: {exc}",
                              body_len=len(body), latency_ms=lat)

    async def _run_all():
        results = []
        for case in cases:
            outcomes = {}
            for arm in arm_list:
                outcomes[arm] = await _run_arm(case, arm)
            results.append(score_edit_ab(case, outcomes))
        return aggregate_edit_ab(results)

    return asyncio.run(_run_all())


def replan_predictor(infer_text: TextFn, *, timeout_s: float = 60.0):
    """Build a plan_fn for the REPLAN (recovery) eval.

    Reconstructs the failure state from a ReplanCase (executed + remaining steps),
    builds the EXACT recovery prompt production sends via the live
    `DevAgent.build_replan_prompt`, runs the plan model, and extracts the recovery
    plan's verb sequence. `enabled=` is read from `DA_TRAJECTORY_REDUCE` (the real
    production toggle) so recording the baseline with the flag OFF and re-running
    with it ON gates exactly the spec's "compaction must not degrade recovery"
    claim. Imports dev_agent lazily (model-side only, like `_build_dev_agent`)."""
    from inference.dev_agent import DevAgent, AgentStep
    from inference.trajectory import reduction_enabled

    system = _plan_system()
    reduce_on = reduction_enabled()

    def _steps(raw: list[dict]) -> list:
        return [
            AgentStep(
                action=str(d.get("action", "")).upper(), args=str(d.get("args", "") or ""),
                result=d.get("result"), success=d.get("success", True),
            )
            for d in (raw or [])
        ]

    def predict(case) -> TrajPrediction:
        executed = _steps(getattr(case, "executed", []))
        remaining = _steps(getattr(case, "remaining", []))
        user, _ = DevAgent.build_replan_prompt(
            case.goal, executed, remaining, enabled=reduce_on
        )
        t0 = time.monotonic()
        try:
            raw = asyncio.run(asyncio.wait_for(infer_text(system, user), timeout_s))
        except Exception as exc:
            return TrajPrediction(error=f"{type(exc).__name__}: {exc}",
                                  latency_ms=(time.monotonic() - t0) * 1000)
        lat = (time.monotonic() - t0) * 1000
        if _is_backend_error(raw):
            return TrajPrediction(raw=raw, error=raw.strip(), latency_ms=lat)
        return TrajPrediction(verbs=extract_plan_verbs(raw), raw=raw, latency_ms=lat)

    return predict


def plan_predictor(infer_text: TextFn, *, timeout_s: float = 30.0,
                   format: dict | None = None):
    """Build a plan_fn: ask the model to plan the goal, extract the verb sequence
    exactly as dev_agent would parse it.

    ``format`` is production's plan-output grammar (``_PLAN_JSON_SCHEMA``). When
    supplied, the eval constrains the plan path the SAME way the live agent does
    (the plan profile sets it as Ollama ``format=``) — closing the eval-vs-prod
    fidelity gap from specs/dev-agent-plan-fidelity R1. None → legacy unconstrained
    call (byte-identical to before this change)."""
    system = _plan_system()

    def predict(case) -> TrajPrediction:
        ctx = "\n".join(getattr(case, "context", []) or [])
        user = (f"Context:\n{ctx}\n\nGoal: {case.goal}" if ctx else f"Goal: {case.goal}")
        t0 = time.monotonic()
        # Pass `format` only when set, so a 2-arg infer_text stub (model-free tests)
        # still works; production's infer_text accepts the optional grammar arg.
        coro = (infer_text(system, user, format) if format is not None
                else infer_text(system, user))
        try:
            raw = asyncio.run(asyncio.wait_for(coro, timeout_s))
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


# --------------------------------------------------------------------------- #
# Sandbox evals — evaluate a goal in a dynamic sandbox
# --------------------------------------------------------------------------- #

def run_sandbox_suite(cases: list, build_dev_agent, *, timeout_s: float = 120.0):
    """Score sandbox cases by executing them in an isolated temporary directory.
    
    Provisions a temporary directory, optionally runs a setup script, runs the
    DevAgent with its workspace isolated to the temp dir, and finally evaluates
    success via an assertion script.
    """
    from evals.sandbox import score_sandbox, aggregate_sandbox
    import tempfile
    import subprocess

    async def _run_all():
        dev = build_dev_agent()

        async def _auto_approve(goal: str, steps, **kwargs) -> str:
            return "auto"
        dev._approve_plan_upfront = _auto_approve   # type: ignore[attr-defined]

        results = []
        for case in cases:
            t0 = time.monotonic()
            try:
                dev._context.clear()
            except Exception:
                pass
                
            with tempfile.TemporaryDirectory() as temp_dir:
                if case.setup_script:
                    try:
                        subprocess.run(
                            case.setup_script, 
                            shell=True, 
                            cwd=temp_dir, 
                            check=True, 
                            capture_output=True, 
                            timeout=30.0
                        )
                    except subprocess.CalledProcessError as e:
                        lat = (time.monotonic() - t0) * 1000
                        results.append(score_sandbox(case, passed=False, error="setup failed", detail=e.stderr.decode("utf-8", "ignore"), latency_ms=lat))
                        continue
                    except subprocess.TimeoutExpired:
                        lat = (time.monotonic() - t0) * 1000
                        results.append(score_sandbox(case, passed=False, error="setup timeout", latency_ms=lat))
                        continue
                        
                dev.set_repo_root(temp_dir)
                
                try:
                    res = await asyncio.wait_for(dev.plan_and_run(case.goal), timeout_s)
                except Exception as exc:
                    lat = (time.monotonic() - t0) * 1000
                    results.append(score_sandbox(case, passed=False, error=f"agent exception: {type(exc).__name__}: {exc}", latency_ms=lat))
                    continue
                    
                lat = (time.monotonic() - t0) * 1000
                
                if case.assert_script:
                    try:
                        subprocess.run(
                            case.assert_script, 
                            shell=True, 
                            cwd=temp_dir, 
                            check=True, 
                            capture_output=True, 
                            timeout=30.0
                        )
                        results.append(score_sandbox(case, passed=True, latency_ms=lat))
                    except subprocess.CalledProcessError as e:
                        err_msg = e.stdout.decode("utf-8", "ignore") + "\\n" + e.stderr.decode("utf-8", "ignore")
                        results.append(score_sandbox(case, passed=False, detail="assertion failed: " + err_msg.strip(), latency_ms=lat))
                    except subprocess.TimeoutExpired:
                        results.append(score_sandbox(case, passed=False, detail="assertion timeout", latency_ms=lat))
                else:
                    results.append(score_sandbox(case, passed=True, latency_ms=lat, detail="no assertion script, auto-pass"))
                    
        return aggregate_sandbox(results)

    return asyncio.run(_run_all())
