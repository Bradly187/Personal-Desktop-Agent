"""Plan-contract auto-repair eval — model-free, deterministic (specs/dev-agent-plan-contract).

The repair loop is deterministic given the planner's outputs, so — unlike the
model-driven edit-format A/B — this eval needs no GPU. Each case scripts the
planner's successive responses and runs them through the REAL production
``DevAgent._acquire_plan_steps`` (same code the live agent uses), asserting it
recovers the right plan (or fails safe to an empty plan) with the expected number
of corrective re-prompts.

Self-contained model-free entrypoint, in the spirit of ``evals/token_budget.py``::

    python -m evals.plan_contract                 # run + print summary
    python -m evals.plan_contract --check         # nonzero exit on baseline regression
    python -m evals.plan_contract --update-baseline
    python -m evals.plan_contract --json

Pure-stdlib scoring (unit-tested in ``tests/test_evals_plan_contract.py``); the
only import that touches the agent is the production parser/repair loop, which is
itself model-free.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from inference.dev_agent import DevAgent

_SUITES_DIR = Path(__file__).parent / "suites"
_BASELINES_DIR = Path(__file__).parent / "baselines"
_SUITE = "plan_contract"


# --------------------------------------------------------------------------- #
# Scripted planner — model-free stand-in for ModelRouter
# --------------------------------------------------------------------------- #

class _Result:
    """Minimal RouterResult stand-in (text/ok/model/error)."""
    def __init__(self, text, ok=True, model="plan-model", error=None):
        self.text = text
        self.ok = ok
        self.model = model
        self.error = error


class _ScriptedRouter:
    """Returns queued plan responses on successive infer() calls; counts calls."""
    def __init__(self, texts):
        self._results = [_Result(t) for t in texts]
        self.calls = 0

    async def infer(self, **kw):
        self.calls += 1
        return self._results.pop(0) if self._results else _Result("{}")


# --------------------------------------------------------------------------- #
# Cases
# --------------------------------------------------------------------------- #

@dataclass
class PlanContractCase:
    id: str
    responses: list                       # responses[0] = initial plan; [1:] = repair replies
    repair: bool = False
    max_repairs: int = 1
    expect_actions: list = field(default_factory=list)
    expect_calls: int = 0
    tags: list = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "PlanContractCase":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})


def load_suite(name: str = _SUITE) -> list[PlanContractCase]:
    path = _SUITES_DIR / f"{name}.jsonl"
    cases: list[PlanContractCase] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        cases.append(PlanContractCase.from_dict(json.loads(s)))
    return cases


# --------------------------------------------------------------------------- #
# Run + score (deterministic)
# --------------------------------------------------------------------------- #

@dataclass
class CaseResult:
    id: str
    actions: list
    calls: int
    expect_actions: list
    expect_calls: int
    correct: bool
    note: str = ""


async def _run_case(case: PlanContractCase) -> CaseResult:
    router = _ScriptedRouter(case.responses[1:])
    agent = DevAgent(router=router)
    agent._plan_repair_enabled = bool(case.repair)
    agent._plan_repair_max = int(case.max_repairs)
    initial = _Result(case.responses[0]) if case.responses else _Result("{}")
    steps, _final = await agent._acquire_plan_steps(case.id, initial, "")
    actions = [s.action for s in steps]
    actions_ok = actions == list(case.expect_actions)
    calls_ok = router.calls == int(case.expect_calls)
    note = ""
    if not actions_ok:
        note = f"actions {actions} != {case.expect_actions}"
    elif not calls_ok:
        note = f"calls {router.calls} != {case.expect_calls}"
    return CaseResult(
        id=case.id, actions=actions, calls=router.calls,
        expect_actions=list(case.expect_actions), expect_calls=int(case.expect_calls),
        correct=actions_ok and calls_ok, note=note,
    )


def run_case(case: PlanContractCase) -> CaseResult:
    """Synchronous wrapper around the async repair loop (one event loop per case)."""
    return asyncio.run(_run_case(case))


@dataclass
class Report:
    n: int
    correct: int
    results: list = field(default_factory=list)

    @property
    def exact_acc(self) -> float:
        return (self.correct / self.n) if self.n else 0.0

    @property
    def failures(self) -> list:
        return [r for r in self.results if not r.correct]

    def metrics(self) -> dict:
        return {"n": self.n, "exact_acc": round(self.exact_acc, 4),
                "correct": self.correct, "failures": len(self.failures)}

    def summary(self) -> str:
        return (f"n={self.n}  exact_acc={self.exact_acc:.1%}  "
                f"correct={self.correct}  failures={len(self.failures)}")


def aggregate(results: list) -> Report:
    return Report(n=len(results), correct=sum(1 for r in results if r.correct),
                  results=results)


def run_suite(name: str = _SUITE) -> Report:
    return aggregate([run_case(c) for c in load_suite(name)])


# --------------------------------------------------------------------------- #
# Baseline lock / regression gate
# --------------------------------------------------------------------------- #

def _baseline_path(name: str = _SUITE) -> Path:
    return _BASELINES_DIR / f"{name}.json"


def load_baseline(name: str = _SUITE) -> dict | None:
    p = _baseline_path(name)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def write_baseline(report: Report, name: str = _SUITE) -> Path:
    p = _baseline_path(name)
    data = {
        "suite": name,
        "mode": "plan_contract",
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
        "model": "none (model-free, deterministic)",
        "tolerance": 0.0,
        **report.metrics(),
    }
    p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return p


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Plan-contract auto-repair eval (model-free).")
    ap.add_argument("--check", action="store_true", help="nonzero exit on baseline regression")
    ap.add_argument("--update-baseline", action="store_true", help="lock the current result")
    ap.add_argument("--json", action="store_true", help="emit metrics as JSON")
    args = ap.parse_args(argv)

    report = run_suite()
    if args.json:
        print(json.dumps(report.metrics()))
    else:
        print(report.summary())
        for r in report.failures:
            print(f"  FAIL {r.id}: {r.note}")

    if args.update_baseline:
        p = write_baseline(report)
        print(f"baseline written: {p}")
        return 0

    if args.check:
        base = load_baseline()
        if base is None:
            print("no baseline — run with --update-baseline first", file=sys.stderr)
            return 2
        tol = float(base.get("tolerance", 0.0))
        if report.exact_acc + 1e-9 < float(base.get("exact_acc", 0.0)) - tol:
            print(f"REGRESSION: exact_acc {report.exact_acc:.3f} < "
                  f"baseline {base.get('exact_acc')} (tol {tol})", file=sys.stderr)
            return 1
        print("OK (no regression)")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
