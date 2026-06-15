"""RAG ablation eval — does the codebase indexer actually ground the dev agent?

Runs the same read-only "explain/find" goal through the LIVE DevAgent twice — once
WITH the CodebaseIndexer wired and once WITHOUT — and scores how grounded each
answer is by anchor-term recall: the fraction of real symbols/terms from the target
file that appear in what the agent produced. The headline is the DELTA (with − without):
a positive mean delta is direct, model-free evidence that RAG improves grounding;
~0 means the retrieval is adding noise the model ignores (or is silently broken).

Model-free scoring (no judge): anchor terms are curated real identifiers from the
target source, so a grounded answer mentions them and a hallucinated one ("quantum
circuit execution jobs" for core/scheduler.py) does not. The run itself needs a
model (it executes real plans) — it's a Tier-2 / on-demand tool, not a CI gate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median

_SUITES_DIR = Path(__file__).parent / "suites"


@dataclass
class AblationCase:
    """One goal + the real anchor terms a grounded answer should surface."""
    id: str
    suite: str
    goal: str
    anchor_terms: list[str] = field(default_factory=list)
    context: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "AblationCase":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class AblationResult:
    case_id: str
    with_score: float = 0.0      # anchor recall WITH the indexer
    no_score: float = 0.0        # anchor recall WITHOUT it
    delta: float = 0.0           # with − without (>0 ⇒ RAG helped)
    with_hits: int = 0
    no_hits: int = 0
    n_anchors: int = 0
    with_latency_ms: float = 0.0
    no_latency_ms: float = 0.0
    error: str = ""
    detail: str = ""


@dataclass
class AblationReport:
    n: int
    mean_with: float
    mean_no: float
    mean_delta: float
    improved: int          # cases where with > without
    regressed: int         # cases where with < without
    p50_latency_ms: float
    errors: int
    results: list = field(default_factory=list)

    @property
    def failures(self) -> list:
        # "failures" for the shared CLI = cases where RAG did not help (or errored)
        return [r for r in self.results if r.error or r.delta <= 0.0]

    # exact_acc aliases mean_delta so the shared --check gate can guard "RAG keeps
    # helping by at least baseline − tolerance".
    @property
    def exact_acc(self) -> float:
        return self.mean_delta

    def metrics(self) -> dict:
        return {
            "n": self.n,
            "mean_with": round(self.mean_with, 4),
            "mean_no": round(self.mean_no, 4),
            "mean_delta": round(self.mean_delta, 4),
            "exact_acc": round(self.mean_delta, 4),   # gated metric
            "improved": self.improved,
            "regressed": self.regressed,
            "p50_latency_ms": round(self.p50_latency_ms, 1),
            "errors": self.errors,
        }

    def summary(self) -> str:
        # ASCII-only (Windows cp1252 consoles can't encode Greek/arrows).
        return (f"n={self.n}  grounding WITH={self.mean_with:.1%}  "
                f"WITHOUT={self.mean_no:.1%}  delta={self.mean_delta:+.1%}  "
                f"improved={self.improved}/{self.n}  regressed={self.regressed}  "
                f"errors={self.errors}")


def grounding_score(text: str, anchor_terms: list[str]) -> tuple[float, int]:
    """Anchor-term recall: fraction of distinct anchor terms present (case-insensitive).
    Returns (score, hits). Empty anchors → (0.0, 0)."""
    if not anchor_terms:
        return 0.0, 0
    tl = (text or "").lower()
    hits = sum(1 for a in anchor_terms if a and a.lower() in tl)
    return hits / len(anchor_terms), hits


def score_ablation(case: AblationCase, with_text: str, no_text: str, *,
                   with_latency_ms: float = 0.0, no_latency_ms: float = 0.0) -> AblationResult:
    err = ""
    for label, t in (("with", with_text), ("without", no_text)):
        if isinstance(t, str) and t.startswith("__ERROR__"):
            err = f"{label}: {t[len('__ERROR__'):].strip()}"
    ws, wh = grounding_score(with_text, case.anchor_terms)
    ns, nh = grounding_score(no_text, case.anchor_terms)
    delta = ws - ns
    return AblationResult(
        case_id=case.id, with_score=ws, no_score=ns, delta=delta,
        with_hits=wh, no_hits=nh, n_anchors=len(case.anchor_terms),
        with_latency_ms=with_latency_ms, no_latency_ms=no_latency_ms, error=err,
        detail=f"with {wh}/{len(case.anchor_terms)} vs without {nh}/{len(case.anchor_terms)}",
    )


def aggregate_ablation(results: list) -> AblationReport:
    n = len(results)
    if n == 0:
        return AblationReport(0, 0.0, 0.0, 0.0, 0, 0, 0.0, 0)
    valid = [r for r in results if not r.error]
    mean_with = sum(r.with_score for r in valid) / len(valid) if valid else 0.0
    mean_no = sum(r.no_score for r in valid) / len(valid) if valid else 0.0
    mean_delta = sum(r.delta for r in valid) / len(valid) if valid else 0.0
    improved = sum(1 for r in valid if r.delta > 0)
    regressed = sum(1 for r in valid if r.delta < 0)
    lats = [r.with_latency_ms for r in results] + [r.no_latency_ms for r in results]
    p50 = median(lats) if lats else 0.0
    errors = sum(1 for r in results if r.error)
    return AblationReport(n, mean_with, mean_no, mean_delta, improved, regressed,
                          p50, errors, results)


def load_ablation_suite(name_or_path: str | Path) -> list:
    """Load an ablation suite by bare name (evals/suites/<name>.jsonl) or path."""
    path = Path(name_or_path)
    if not path.suffix:
        path = _SUITES_DIR / f"{path.name}.jsonl"
    cases = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        cases.append(AblationCase.from_dict(json.loads(line)))
    return cases
