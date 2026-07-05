"""dev_critic eval — does the Critic catch plausible-but-wrong edits?

The payoff experiment for ``specs/dev-agent-critic`` (task 6). Each case is a
fixture (original good file) + a "bad edit" (what a planner hallucinated) that
passes ``ast.parse`` but has a correctness or security bug. The eval runs two arms:

  lint   — just the ast.parse gate (no Critic). Baseline: should catch 0 of the
            syntactically-valid bugs, confirming the lint gate's blind spot.
  critic — the production ``Critic.review()`` call on the diff (original -> bad).
           Should return REVISE or BLOCK on every case.

Scoring (deterministic for the lint arm, model-driven for the critic arm):
  lint_catches   — ast.parse raises on bad_code  (expected: False for all cases)
  critic_catches — verdict.decision in {revise, block}
  catch_rate     — fraction of cases where critic_catches

The headline metric is ``catch_rate`` (gated by ``exact_acc``); the ``lift``
(critic − lint) proves the Critic adds value the lint gate cannot provide alone.
Model and latency/cost are recorded alongside.

A/B structure mirrors ``evals/edit_ab.py``: pure stdlib scoring, model only in the
runner. Run via ``python -m evals.dev_critic`` or the shared CLI
``python -m evals.run --mode dev_critic --suite dev_critic``.
"""

from __future__ import annotations

import ast
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median

_SUITES_DIR = Path(__file__).parent / "suites"
_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "dev_critic"
_BASELINE_DIR = Path(__file__).parent / "baselines"

# The two arms, in display order.
LINT = "lint"
CRITIC = "critic"
ARMS = (LINT, CRITIC)


# --------------------------------------------------------------------------- #
# Case dataclass
# --------------------------------------------------------------------------- #

@dataclass
class DevCriticCase:
    """One fixture + bad edit + meta for the Critic eval."""
    id: str
    suite: str
    fixture: str            # filename under fixtures/dev_critic/
    goal: str               # what the edit was supposed to accomplish
    bad_code: str           # the buggy version of the file (syntactically valid)
    bug_type: str = ""      # "correctness" | "security" | ...
    bug_description: str = ""
    tags: list = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "DevCriticCase":
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})

    def fixture_path(self) -> Path:
        return _FIXTURES_DIR / self.fixture

    def original_code(self) -> str:
        return self.fixture_path().read_text(encoding="utf-8")


def load_dev_critic_suite(name: str) -> list[DevCriticCase]:
    """Load ``suites/<name>.jsonl`` (``#``-comment lines allowed)."""
    path = _SUITES_DIR / f"{name}.jsonl"
    cases: list[DevCriticCase] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        cases.append(DevCriticCase.from_dict(json.loads(s)))
    return cases


# --------------------------------------------------------------------------- #
# Per-arm result + scoring
# --------------------------------------------------------------------------- #

@dataclass
class ArmResult:
    arm: str
    catches: bool = False       # lint: syntax error caught; critic: REVISE/BLOCK
    decision: str = ""          # critic only: pass/revise/block
    confidence: float = 0.0     # critic only
    findings_count: int = 0     # critic only
    latency_ms: float = 0.0
    error: str = ""             # inference/IO error


@dataclass
class DevCriticResult:
    case_id: str
    bug_type: str = ""
    arms: dict = field(default_factory=dict)    # arm -> ArmResult
    error: str = ""

    @property
    def lift(self) -> float:
        """critic.catches - lint.catches (1 = Critic found what lint missed)."""
        c = self.arms.get(CRITIC)
        l = self.arms.get(LINT)
        if c is None or l is None:
            return 0.0
        return float(c.catches) - float(l.catches)


def _lint_catches(code: str) -> tuple[bool, str]:
    """True if ast.parse raises on `code` (the lint gate would catch this)."""
    try:
        ast.parse(code)
        return False, ""
    except SyntaxError as e:
        return True, str(e)


def score_case(case: DevCriticCase, arm_results: dict) -> DevCriticResult:
    return DevCriticResult(
        case_id=case.id,
        bug_type=case.bug_type,
        arms=arm_results,
    )


# --------------------------------------------------------------------------- #
# Aggregate report
# --------------------------------------------------------------------------- #

@dataclass
class DevCriticReport:
    n: int
    catch_rate_lint: float = 0.0    # expected ~0 (bugs are syntax-valid)
    catch_rate_critic: float = 0.0  # headline: fraction the Critic caught
    lift: float = 0.0               # catch_rate_critic - catch_rate_lint
    mean_confidence: float = 0.0    # critic confidence on caught cases
    p50_latency_ms: float = 0.0     # median Critic call latency
    errors: int = 0
    results: list = field(default_factory=list)

    @property
    def exact_acc(self) -> float:
        return self.catch_rate_critic

    @property
    def failures(self) -> list:
        return [r for r in self.results
                if r.error or not (r.arms.get(CRITIC) or ArmResult(CRITIC)).catches]

    def metrics(self) -> dict:
        return {
            "n": self.n,
            "catch_rate_critic": round(self.catch_rate_critic, 4),
            "catch_rate_lint": round(self.catch_rate_lint, 4),
            "lift": round(self.lift, 4),
            "mean_confidence": round(self.mean_confidence, 4),
            "p50_latency_ms": round(self.p50_latency_ms, 1),
            "exact_acc": round(self.catch_rate_critic, 4),
            "errors": self.errors,
        }

    def summary(self) -> str:
        return (
            f"n={self.n}  critic_catch={self.catch_rate_critic:.1%}  "
            f"lint_catch={self.catch_rate_lint:.1%}  lift={self.lift:+.1%}  "
            f"conf={self.mean_confidence:.2f}  p50={self.p50_latency_ms:.0f}ms  "
            f"errors={self.errors}"
        )


def aggregate_dev_critic(results: list) -> DevCriticReport:
    n = len(results)
    if n == 0:
        return DevCriticReport(0)
    valid = [r for r in results if not r.error]
    errors = sum(1 for r in results if r.error)

    def _rate(arm: str) -> float:
        scores = [r.arms.get(arm) for r in valid if r.arms.get(arm)]
        if not scores:
            return 0.0
        return sum(1 for s in scores if s.catches) / len(scores)

    critic_results = [r.arms[CRITIC] for r in valid if CRITIC in r.arms]
    caught = [s for s in critic_results if s.catches]
    mean_conf = (sum(s.confidence for s in caught) / len(caught)) if caught else 0.0
    lats = [s.latency_ms for s in critic_results if s.latency_ms > 0]
    p50 = median(lats) if lats else 0.0
    cr = _rate(CRITIC)
    lr = _rate(LINT)

    return DevCriticReport(
        n=n,
        catch_rate_lint=lr,
        catch_rate_critic=cr,
        lift=cr - lr,
        mean_confidence=mean_conf,
        p50_latency_ms=p50,
        errors=errors,
        results=results,
    )


# --------------------------------------------------------------------------- #
# Runner (async, injected router)
# --------------------------------------------------------------------------- #

async def run_dev_critic_suite_async(cases: list, router, *,
                                     timeout_s: float = 90.0) -> DevCriticReport:
    """Score every case against both arms. `router` must implement
    ``async infer(domain, user_text, context) -> result`` with ``.text`` / ``.ok``
    (the same duck-type ``Critic`` uses, so this exercises the real production path).
    """
    import asyncio
    from inference.critic import Critic, REVISE, BLOCK

    critic = Critic(router=router, model_domain="plan")
    results = []

    for case in cases:
        try:
            original = case.original_code()
        except Exception as e:
            results.append(DevCriticResult(
                case_id=case.id, bug_type=case.bug_type,
                error=f"fixture read: {e}",
            ))
            continue

        # Arm 1: lint gate only (deterministic, no model).
        lint_catches, lint_msg = _lint_catches(case.bad_code)
        lint_arm = ArmResult(arm=LINT, catches=lint_catches,
                             decision="syntax_error" if lint_catches else "pass",
                             error=lint_msg if lint_catches else "")

        # Arm 2: Critic (model call).
        t0 = time.monotonic()
        try:
            verdict = await asyncio.wait_for(
                critic.review(
                    goal=case.goal,
                    path=case.fixture,
                    old_text=original,
                    new_text=case.bad_code,
                ),
                timeout=timeout_s,
            )
            lat = (time.monotonic() - t0) * 1000
            critic_arm = ArmResult(
                arm=CRITIC,
                catches=verdict.decision in (REVISE, BLOCK),
                decision=verdict.decision,
                confidence=verdict.confidence,
                findings_count=len(verdict.findings),
                latency_ms=lat,
            )
        except Exception as exc:
            lat = (time.monotonic() - t0) * 1000
            critic_arm = ArmResult(
                arm=CRITIC, catches=False,
                error=f"{type(exc).__name__}: {exc}",
                latency_ms=lat,
            )

        results.append(score_case(case, {LINT: lint_arm, CRITIC: critic_arm}))

    return aggregate_dev_critic(results)


def run_dev_critic_suite(cases: list, router, *, timeout_s: float = 90.0):
    """Synchronous entry point (wraps the async runner)."""
    import asyncio
    return asyncio.run(run_dev_critic_suite_async(cases, router, timeout_s=timeout_s))


# --------------------------------------------------------------------------- #
# Minimal router wrapper for OllamaInference
# --------------------------------------------------------------------------- #

def _make_ollama_router(model: str | None = None):
    """Build a duck-type router that wraps OllamaInference for the eval.

    The Critic calls ``await router.infer(domain, user_text, context)`` and reads
    ``.text`` / ``.ok`` from the result. This wrapper maps that to a direct
    OllamaInference chat call (same backend the real DevAgent uses).
    """
    from inference.local_inference import OllamaInference

    oi = OllamaInference(model=model) if model else OllamaInference()

    class _Result:
        __slots__ = ("text", "ok")

        def __init__(self, text: str):
            self.text = text
            self.ok = bool(text)

    class _Router:
        async def infer(self, domain: str, user_text: str, context: str = "") -> _Result:
            messages = [{"role": "user", "content": user_text}]
            resp = await oi._chat(messages)
            text = ""
            if isinstance(resp, dict):
                msg = resp.get("message", {})
                text = msg.get("content", "") if isinstance(msg, dict) else ""
            return _Result(text)

    return oi, _Router()


# --------------------------------------------------------------------------- #
# CLI  (python -m evals.dev_critic)
# --------------------------------------------------------------------------- #

def _print_report(report: DevCriticReport, *, show: int = 20) -> None:
    print(report.summary())
    by_type: dict[str, list] = {}
    for r in report.results:
        by_type.setdefault(r.bug_type or "unknown", []).append(r)
    if len(by_type) > 1:
        print("\nby bug type:")
        for btype, rs in sorted(by_type.items()):
            caught = sum(1 for r in rs if (r.arms.get(CRITIC) or ArmResult(CRITIC)).catches)
            print(f"  {btype:<14} {caught}/{len(rs)}")
    print("\nper case (lint | critic -> catches):")
    for r in report.results[:show]:
        lint = r.arms.get(LINT) or ArmResult(LINT)
        crit = r.arms.get(CRITIC) or ArmResult(CRITIC)
        flag = "OK " if crit.catches else "XX "
        parts = [f"  {flag}[{r.case_id}]",
                 f"lint={'Y' if lint.catches else 'N'}",
                 f"critic={crit.decision or 'ERR':<8}",
                 f"conf={crit.confidence:.2f}" if crit.decision else "",
                 f"{crit.latency_ms:.0f}ms"]
        if crit.error:
            parts.append(f"  ERR: {crit.error[:60]}")
        print("  ".join(p for p in parts if p))


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    ap = argparse.ArgumentParser(description="dev_critic eval: does the Critic catch planted bugs?")
    ap.add_argument("--suite", default="dev_critic",
                    help="suite name (evals/suites/<name>.jsonl)")
    ap.add_argument("--model", default=None,
                    help="Ollama model (default: OllamaInference default)")
    ap.add_argument("--timeout", type=float, default=90.0,
                    help="per-case Critic call timeout (s); use 120+ for large models")
    ap.add_argument("--update-baseline", action="store_true",
                    help="write/overwrite evals/baselines/dev_critic.json")
    ap.add_argument("--check", action="store_true",
                    help="exit nonzero if catch_rate_critic < baseline (regression gate)")
    ap.add_argument("--json", dest="json_out", action="store_true",
                    help="emit report as JSON")
    ap.add_argument("--tolerance", type=float, default=0.10,
                    help="regression band for --check (default 0.10 = 10pp)")
    args = ap.parse_args(argv)

    cases = load_dev_critic_suite(args.suite)
    if not cases:
        print("no cases in suite", file=sys.stderr)
        return 2

    oi, router = _make_ollama_router(args.model)
    model_name = getattr(oi, "model", None) or (args.model or "default")
    print(f"dev_critic: model={model_name}  n={len(cases)}  timeout={args.timeout}s")

    # Pre-warm: the first real infer call on a cold model can exceed the aiohttp
    # timeout while VRAM loads. A cheap ping settles the load before timed cases.
    import asyncio
    try:
        asyncio.run(asyncio.wait_for(
            oi._chat([{"role": "user", "content": "ping"}]), timeout=60.0))
        print("model warm.")
    except Exception:
        pass  # best-effort; if it fails the per-case timeout will absorb the load

    print("running Critic on each case (lint arm is model-free)...")

    report = run_dev_critic_suite(cases, router, timeout_s=args.timeout)

    if report.errors == report.n and report.n:
        print(f"all {report.n} Critic calls errored — is Ollama running?", file=sys.stderr)
        return 2

    if args.json_out:
        import time as _t
        print(json.dumps({
            "suite": args.suite,
            "mode": "dev_critic",
            "model": model_name,
            "recorded_at": _t.strftime("%Y-%m-%dT%H:%M:%S"),
            **report.metrics(),
        }, indent=2))
    else:
        _print_report(report)

    baseline_path = _BASELINE_DIR / f"{args.suite}.json"
    if args.update_baseline:
        import time as _t
        _BASELINE_DIR.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(json.dumps({
            "suite": args.suite,
            "mode": "dev_critic",
            "recorded_at": _t.strftime("%Y-%m-%dT%H:%M:%S"),
            "model": model_name,
            "tolerance": args.tolerance,
            **report.metrics(),
        }, indent=2) + "\n", encoding="utf-8")
        print(f"\nbaseline written: {baseline_path}")

    if args.check:
        if not baseline_path.exists():
            print(f"no baseline at {baseline_path} — run with --update-baseline first",
                  file=sys.stderr)
            return 2
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        locked = float(baseline.get("catch_rate_critic", 0.0))
        tol = float(baseline.get("tolerance", args.tolerance))
        actual = report.catch_rate_critic
        ok = actual >= locked - tol
        status = "OK" if ok else "REGRESSION"
        print(f"\n{status}: catch_rate_critic={actual:.1%}  locked={locked:.1%}  "
              f"tolerance={tol:.1%}")
        return 0 if ok else 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
