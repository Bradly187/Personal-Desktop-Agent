"""Sandbox evals: evaluate an agent dynamically by running it in an isolated temp dir.

Provides SandboxCase, SandboxResult, SandboxReport and loading functions.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

_SUITES_DIR = Path(__file__).parent / "suites"

@dataclass
class SandboxCase:
    """One goal -> dynamic assertion in a sandbox.
    
    goal: The instruction for the agent to execute.
    setup_script: Optional bash/powershell script to prime the sandbox directory.
    assert_script: A script that runs in the sandbox. If it exits with 0, the case passes.
    """
    id: str
    suite: str
    goal: str
    setup_script: str = ""
    assert_script: str = ""
    context: list[str] = field(default_factory=list)
    domain: str = "code"
    source: str = "curated"
    tags: list[str] = field(default_factory=list)
    
    @classmethod
    def from_dict(cls, d: dict) -> "SandboxCase":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class SandboxResult:
    case_id: str
    passed: bool
    score: float  # 1.0 if passed else 0.0
    latency_ms: float = 0.0
    error: str = ""
    detail: str = ""


@dataclass
class SandboxReport:
    n: int
    exact_acc: float          # the gated metric (fraction fully correct)
    mean_score: float
    p50_latency_ms: float
    errors: int
    failures: list[SandboxResult]

    def summary(self) -> str:
        return (
            f"n={self.n}  exact_acc={self.exact_acc:.1%}  "
            f"mean_score={self.mean_score:.2f}  "
            f"p50={self.p50_latency_ms:.0f}ms  errors={self.errors}  "
            f"failures={len(self.failures)}"
        )

    def metrics(self) -> dict:
        return {
            "n": self.n,
            "exact_acc": round(self.exact_acc, 4),
            "mean_score": round(self.mean_score, 4),
            "p50_latency_ms": round(self.p50_latency_ms, 1),
            "errors": self.errors,
        }


def score_sandbox(case: SandboxCase, passed: bool, error: str = "", detail: str = "", latency_ms: float = 0.0) -> SandboxResult:
    return SandboxResult(
        case_id=case.id,
        passed=passed,
        score=1.0 if passed else 0.0,
        latency_ms=latency_ms,
        error=error,
        detail=detail,
    )


def aggregate_sandbox(results: list[SandboxResult]) -> SandboxReport:
    import statistics
    n = len(results)
    if n == 0:
        return SandboxReport(0, 0.0, 0.0, 0.0, 0, [])
    lat = [r.latency_ms for r in results if r.latency_ms > 0]
    return SandboxReport(
        n=n,
        exact_acc=sum(r.passed for r in results) / n,
        mean_score=sum(r.score for r in results) / n,
        p50_latency_ms=statistics.median(lat) if lat else 0.0,
        errors=sum(1 for r in results if r.error),
        failures=[r for r in results if not r.passed],
    )


def load_sandbox_suite(name_or_path: str | Path) -> list[SandboxCase]:
    """Load a sandbox suite by bare name (evals/suites/<name>.jsonl) or path."""
    path = Path(name_or_path)
    if not path.suffix:
        path = _SUITES_DIR / f"{path.name}.jsonl"
    cases: list[SandboxCase] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        cases.append(SandboxCase.from_dict(json.loads(line)))
    return cases
