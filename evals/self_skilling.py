"""Self-skilling (rung 2 / macros) eval — model-free, deterministic.

Macro detection and replay are deterministic given the journaled trajectories,
so — like ``evals/plan_contract.py`` — this needs no GPU. Each case scripts a
set of synthetic successful runs, feeds them through the REAL production
``MacroDetector`` + ``MacroStore`` (the same code the live agent uses), and
asserts the expected macros are staged (and, optionally, that a promoted macro
replays with the expected outcome).

    python -m evals.self_skilling                 # run + print summary
    python -m evals.self_skilling --check         # nonzero exit on regression
    python -m evals.self_skilling --update-baseline
    python -m evals.self_skilling --json

Scoring is pure-stdlib (unit-tested in tests/test_evals_self_skilling.py); the
only imports that touch the agent are the production detector/store, themselves
model-free.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from adaptive.macro_detector import MacroDetector
from core.macro_store import MacroStore
from storage.db import AgentDB

_SUITES_DIR = Path(__file__).parent / "suites"
_BASELINES_DIR = Path(__file__).parent / "baselines"
_SUITE = "self_skilling"

_DEFAULT_VERBS = {"OPEN", "CLICK", "TYPE", "HOTKEY", "SCROLL",
                  "WRITE_FILE", "RUN_TERMINAL", "READ_FILE"}


class _FakeExec:
    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on

    async def execute(self, cmd):
        idx = len(self.calls)
        self.calls.append(cmd.action)
        if self.fail_on is not None and idx == self.fail_on:
            return {"status": "error", "error": "boom"}
        return {"status": "ok"}


# --------------------------------------------------------------------------- #
# Cases
# --------------------------------------------------------------------------- #

@dataclass
class SelfSkillingCase:
    id: str
    runs: list                              # [{goal, steps:[[action, args], ...]}, ...]
    min_occurrences: int = 3
    similarity: float = 1.0
    known_verbs: list = field(default_factory=lambda: sorted(_DEFAULT_VERBS))
    expect_staged: int = 0
    expect_signature: str = ""              # "" = don't assert
    replay: dict = field(default_factory=dict)   # {} = no replay assertion
    tags: list = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "SelfSkillingCase":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})


def load_suite(name: str = _SUITE) -> list[SelfSkillingCase]:
    path = _SUITES_DIR / f"{name}.jsonl"
    cases: list[SelfSkillingCase] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        cases.append(SelfSkillingCase.from_dict(json.loads(s)))
    return cases


# --------------------------------------------------------------------------- #
# Run + score (deterministic)
# --------------------------------------------------------------------------- #

@dataclass
class CaseResult:
    id: str
    correct: bool
    note: str = ""


async def _run_case(case: SelfSkillingCase) -> CaseResult:
    d = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    db = AgentDB()
    await db.open(os.path.join(d.name, "agent.db"))
    try:
        for run in case.runs:
            steps = run["steps"]
            rid = await db.insert_agent_run(
                None, run.get("goal", ""), "command", "test", len(steps), True, 1.0)
            for i, (action, args) in enumerate(steps):
                await db.insert_agent_step(rid, i, action, args, None, "ok", True, 1.0)

        verbs = set(case.known_verbs)
        det = MacroDetector(db, known_verbs=verbs,
                            min_occurrences=case.min_occurrences,
                            similarity=case.similarity)
        staged = await det.run_once()

        if len(staged) != case.expect_staged:
            return CaseResult(case.id, False,
                              f"staged {len(staged)} != {case.expect_staged}")

        if case.expect_signature:
            rows = await db.get_evolution_candidates(status="proposed", kind="macro")
            sigs = [r["action_or_wrong"] for r in rows]
            if case.expect_signature not in sigs:
                return CaseResult(case.id, False,
                                  f"signature {case.expect_signature!r} not in {sigs}")

        if case.replay and staged:
            await db.set_evolution_candidate_status(staged[0], "promoted")
            rows = await db.get_evolution_candidates(status="promoted", kind="macro")
            store = MacroStore(known_verbs=set(case.replay.get("known_verbs", case.known_verbs)))
            macro = store.register(rows[0])
            want = case.replay.get("expect_status")
            if macro is None:
                if want != "unregistered":
                    return CaseResult(case.id, False, "macro failed to register")
            else:
                params = {int(k): v for k, v in case.replay.get("params", {}).items()}
                res = await store.replay(macro, _FakeExec(fail_on=case.replay.get("fail_on")),
                                         params=params)
                if res.get("status") != want:
                    return CaseResult(case.id, False,
                                      f"replay status {res.get('status')} != {want}")
                if "expect_steps_run" in case.replay and \
                        res.get("steps_run") != case.replay["expect_steps_run"]:
                    return CaseResult(case.id, False,
                                      f"steps_run {res.get('steps_run')} != "
                                      f"{case.replay['expect_steps_run']}")
        return CaseResult(case.id, True)
    finally:
        await db.close()
        d.cleanup()


def run_case(case: SelfSkillingCase) -> CaseResult:
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


def load_baseline(name: str = _SUITE) -> "dict | None":
    p = _baseline_path(name)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def write_baseline(report: Report, name: str = _SUITE) -> Path:
    p = _baseline_path(name)
    data = {
        "suite": name,
        "mode": "self_skilling",
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
        "model": "none (model-free, deterministic)",
        "tolerance": 0.0,
        **report.metrics(),
    }
    p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return p


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Self-skilling (macros) eval — model-free.")
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
