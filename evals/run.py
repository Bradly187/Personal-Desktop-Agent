"""CLI: run an eval suite against the live local model + optional regression gate.

Three eval kinds, selected by --mode:
  single      (default) utterance -> verb/slots         [command_verbs, *_slots]
  trajectory  goal -> multi-step plan path              [dev_trajectory]
  judge       free-form answer scored by an LM rubric   [explain_quality]

This is the ONLY place the harness touches a GPU/model, and it fails safe: if the
model is unreachable every case is recorded as an error and the tool exits 2 (skip)
WITHOUT writing a baseline — it can report a hung backend but never wedge. All
three kinds share the regression gate (on each report's `exact_acc`: exact rate for
single/trajectory, pass rate for judge).

Examples:
    python -m evals.run --suite command_verbs
    python -m evals.run --suite command_verbs --check          # nonzero on regression
    python -m evals.run --suite pain_journal_slots --predictor slots
    python -m evals.run --suite dev_trajectory --mode trajectory
    python -m evals.run --suite explain_quality --mode judge --judge-model gemma3:27b
    python -m evals.run --suite command_verbs --db agent.db    # + harvest gold cases
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from evals.corpus import load_suite, harvest_from_agent_db
from evals.runner import (
    run_suite, command_predictor, slot_predictor, router_predictor,
    skill_trigger_predictor,
    run_trajectory_suite, plan_predictor, replan_predictor,
    run_judge_suite, llm_judge, answer_producer,
)
from evals.scoring import check_regression

_BASELINE_DIR = Path(__file__).parent / "baselines"


def _infer_text(model: str | None):
    """A (system, user) -> reply coroutine fn over the local model. Imports lazily
    so --help / a missing aiohttp never break the CLI."""
    from inference.local_inference import OllamaInference

    oi = OllamaInference(model=model) if model else OllamaInference()

    async def infer_text(system: str, user: str) -> str:
        resp = await oi._chat([
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ])
        msg = (resp or {}).get("message", {})
        return msg.get("content", "") if isinstance(msg, dict) else ""

    return oi, infer_text


# --------------------------------------------------------------------------- #
# per-mode run + print
# --------------------------------------------------------------------------- #

def _run_single(args):
    cases = load_suite(args.suite)
    if args.db and args.predictor not in ("router", "skill_trigger"):
        gold = harvest_from_agent_db(args.db, suite=args.suite, limit=args.limit)
        print(f"harvested {len(gold)} gold case(s) from {args.db}")
        cases += gold
    if not cases:
        print("no cases to run", file=sys.stderr)
        return None
    if args.predictor == "router":          # deterministic, no model needed
        return run_suite(cases, router_predictor())
    if args.predictor == "skill_trigger":   # deterministic, no model needed
        return run_suite(cases, skill_trigger_predictor())
    oi, infer_text = _infer_text(args.model)
    predict = (command_predictor(oi.infer, timeout_s=args.timeout)
               if args.predictor == "command"
               else slot_predictor(infer_text, timeout_s=args.timeout))
    return run_suite(cases, predict)


def _run_trajectory(args):
    from evals.trajectory import load_trajectory_suite
    cases = load_trajectory_suite(args.suite)
    if not cases:
        print("no cases to run", file=sys.stderr)
        return None
    _, infer_text = _infer_text(args.model)
    return run_trajectory_suite(cases, plan_predictor(infer_text, timeout_s=args.timeout))


def _run_replan(args):
    from evals.trajectory import load_replan_suite
    cases = load_replan_suite(args.suite)
    if not cases:
        print("no cases to run", file=sys.stderr)
        return None
    _, infer_text = _infer_text(args.model)
    from inference.trajectory import reduction_enabled
    print(f"trajectory reduction: {'ON' if reduction_enabled() else 'OFF'} "
          f"(DA_TRAJECTORY_REDUCE)")
    return run_trajectory_suite(cases, replan_predictor(infer_text, timeout_s=args.timeout))


def _run_judge(args):
    from evals.judge import load_judge_suite
    cases = load_judge_suite(args.suite)
    if not cases:
        print("no cases to run", file=sys.stderr)
        return None
    _, producer_infer = _infer_text(args.model)
    _, judge_infer = _infer_text(args.judge_model or args.model)
    return run_judge_suite(
        cases,
        llm_judge(judge_infer, timeout_s=args.timeout),
        produce_fn=answer_producer(producer_infer, timeout_s=args.timeout),
    )


def _build_dev_agent(model: str | None):
    """Construct a minimal real DevAgent for the execution eval.

    DevAgent only *requires* a ModelRouter; coordinator / indexer / scheduler /
    memory are optional (None → guarded no-ops). The plan domain is routed to the
    plan specialist by ModelRouter, so --model is informational here (the router
    decides the planner model). Built lazily so --help never imports the model stack.
    """
    from inference.dev_agent import DevAgent
    from inference.model_router import ModelRouter
    return DevAgent(router=ModelRouter())


def _run_execution(args):
    from evals.trajectory import load_trajectory_suite
    from evals.runner import run_execution_suite
    cases = load_trajectory_suite(args.suite)
    if not cases:
        print("no cases to run", file=sys.stderr)
        return None
    return run_execution_suite(
        cases, lambda: _build_dev_agent(args.model), timeout_s=args.timeout)


async def _build_dev_agent_with_rag():
    """A real DevAgent with the live CodebaseIndexer wired (queries the persisted
    chroma_db as-is — no re-index, so the ablation is fast and deterministic)."""
    from pathlib import Path as _P
    from inference.dev_agent import DevAgent
    from inference.model_router import ModelRouter
    from inference.codebase_indexer import CodebaseIndexer
    agent = DevAgent(router=ModelRouter())
    root = str(_P(__file__).resolve().parents[1])
    idx = CodebaseIndexer(project_root=root, embedder=None)
    if await idx.start():
        agent.set_indexer(idx)
    else:
        print("WARNING: CodebaseIndexer unavailable — with-RAG arm has no index",
              file=sys.stderr)
    return agent


def _run_edit_ab(args):
    from evals.edit_ab import load_edit_ab_suite
    from evals.runner import run_edit_ab_suite
    cases = load_edit_ab_suite(args.suite)
    if not cases:
        print("no cases to run", file=sys.stderr)
        return None
    _, infer_text = _infer_text(args.model)
    print(f"edit-format A/B: model={args.model or 'default'} "
          f"arms=whole_file vs hashline  n={len(cases)}")
    return run_edit_ab_suite(cases, infer_text, timeout_s=args.timeout)


def _run_ablation(args):
    from evals.ablation import load_ablation_suite
    from evals.runner import run_ablation_suite
    cases = load_ablation_suite(args.suite)
    if not cases:
        print("no cases to run", file=sys.stderr)
        return None
    return run_ablation_suite(
        cases,
        build_no_rag=lambda: _build_dev_agent(args.model),
        build_with_rag=_build_dev_agent_with_rag,
        timeout_s=args.timeout)


def _print_ablation(report, *, show: int = 20) -> None:
    print(report.summary())
    print("\nper case (anchor recall  with -> without):")
    for r in report.results:
        flag = "ERR " if r.error else ("+   " if r.delta > 0 else ("-   " if r.delta < 0 else "=   "))
        line = (f"  {flag}[{r.case_id}] {r.with_score:.0%} -> {r.no_score:.0%} "
                f"(d{r.delta:+.0%}; {r.with_hits}/{r.n_anchors} vs {r.no_hits}/{r.n_anchors})")
        if r.error:
            line += f"  {r.error[:60]}"
        print(line)


def _print_edit_ab(report) -> None:
    print(report.summary())
    print("\nper case (arm: applied/lint/intent/preserved -> correct):")
    for r in report.results:
        if r.error:
            print(f"  ERR [{r.case_id}] {r.error[:70]}")
            continue
        print(f"  [{r.case_id}]")
        for arm, s in r.arms.items():
            flag = "OK " if s.correct else "XX "
            bits = (f"applied={int(s.applied)} lint={int(s.lint_ok)} "
                    f"intent={int(s.intent_ok)} preserved={int(s.preserved)}")
            line = f"     {flag}{arm:<11} {bits}  {s.latency_ms:.0f}ms"
            if not s.correct and s.note:
                line += f"  :: {s.note}"
            print(line)


def _print_report(report, mode: str, *, show_failures: int = 12) -> None:
    print(report.summary())
    by_verb = getattr(report, "by_verb", None)
    if by_verb:
        print("\nby verb:")
        for verb, b in sorted(by_verb.items()):
            print(f"  {verb:<12} n={b['n']:<3} verb={b['verb_ok']}/{b['n']} "
                  f"exact={b['exact']}/{b['n']}")
    by_crit = getattr(report, "by_criterion", None)
    if by_crit:
        print("\nby criterion (mean):")
        for k, v in sorted(by_crit.items()):
            print(f"  {k:<16} {v:.2f}")
    if report.failures:
        print(f"\nfailures (first {show_failures}):")
        for r in report.failures[:show_failures]:
            if mode in ("trajectory", "execution", "replan"):
                why = r.error or r.detail or f"got {r.predicted_verbs}"
            elif mode == "judge":
                why = r.error or f"overall={r.overall:.2f} :: {r.rationale}"
            else:
                why = r.error or f"got {r.predicted_verb!r}, want {r.expected_verb!r}"
            print(f"  [{r.case_id}] {why}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run a behavioral eval suite.")
    ap.add_argument("--suite", required=True, help="suite name (evals/suites/<name>.jsonl)")
    ap.add_argument("--mode",
                    choices=["single", "trajectory", "judge", "execution",
                             "ablation", "replan", "edit_ab"],
                    default="single",
                    help="execution = run the goal through the LIVE DevAgent and "
                         "score the executed trajectory (read-only suites only); "
                         "ablation = run each goal WITH vs WITHOUT the codebase "
                         "indexer and score grounding (anchor recall) delta; "
                         "replan = score recovery from a failure via the production "
                         "build_replan_prompt (DA_TRAJECTORY_REDUCE toggles compaction); "
                         "edit_ab = A/B the WRITE_FILE edit format (whole_file vs "
                         "hashline) on the same fixed model, applied through the real "
                         "EditApplier + lint gate (specs/edit-format-aci task 6)")
    ap.add_argument("--predictor",
                    choices=["command", "slots", "router", "skill_trigger"],
                    default="command",
                    help="single mode only (router = model-free DomainClassifier eval; "
                         "skill_trigger = model-free skill-router eval)")
    ap.add_argument("--model", default=None, help="override local model name")
    ap.add_argument("--judge-model", default=None, help="judge mode: model for the judge")
    ap.add_argument("--timeout", type=float, default=60.0,
                    help="per-model-call timeout seconds (raise for cold/large models)")
    ap.add_argument("--db", default=None, help="single mode: agent.db to harvest gold cases")
    ap.add_argument("--limit", type=int, default=200, help="max harvested cases")
    ap.add_argument("--update-baseline", action="store_true")
    ap.add_argument("--tolerance", type=float, default=0.05,
                    help="regression band when writing a baseline (widen for "
                         "nondeterministic model gates, e.g. thinking models)")
    ap.add_argument("--check", action="store_true", help="exit nonzero on regression")
    ap.add_argument("--json", action="store_true", help="emit the report as JSON")
    args = ap.parse_args(argv)

    runner = {"single": _run_single, "trajectory": _run_trajectory,
              "judge": _run_judge, "execution": _run_execution,
              "ablation": _run_ablation, "replan": _run_replan,
              "edit_ab": _run_edit_ab}[args.mode]
    report = runner(args)
    if report is None:
        return 2

    # Fail safe: an all-errors report means the backend is down — skip, don't lie.
    if report.errors == report.n and report.n:
        first = report.failures[0].error if report.failures else "unknown"
        print(f"model unreachable — all {report.n} cases errored (first: {first})",
              file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps({"suite": args.suite, "mode": args.mode, **report.metrics()},
                         indent=2))
    elif args.mode == "ablation":
        _print_ablation(report)
    elif args.mode == "edit_ab":
        _print_edit_ab(report)
    else:
        _print_report(report, args.mode)

    baseline_path = _BASELINE_DIR / f"{args.suite}.json"
    if args.update_baseline:
        _BASELINE_DIR.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(json.dumps({
            "suite": args.suite,
            "mode": args.mode,
            "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "model": args.model or "default",
            "tolerance": args.tolerance,
            **report.metrics(),
        }, indent=2) + "\n", encoding="utf-8")
        print(f"\nbaseline updated: {baseline_path}")

    if args.check:
        baseline = json.loads(baseline_path.read_text(encoding="utf-8")) \
            if baseline_path.exists() else {}
        ok, msg = check_regression(report, baseline)
        print(f"\n{msg}")
        return 0 if ok else 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
