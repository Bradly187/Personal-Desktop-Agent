"""LM-as-judge evals: score a free-form agent output against a rubric.

The command/trajectory evals score deterministic structure (which verb, which
order). Some surfaces have no single right string: an EXPLAIN answer, a personal-KB
synthesis, a vision-grounding rationale. The white-paper scores these the way a
reviewer would — against a labelled rubric, with an LM judge: not "does it equal
the gold string" but "does it meet the bar — correct, grounded, on-task, and free
of hallucination?"

Design mirrors the rest of the package. The core here (rubric prompt, verdict
parsing, aggregation) is pure stdlib and unit-tested with a FAKE judge; the real
judge model is injected by run.py. The judge is asked for a STRICT JSON verdict so
parsing is deterministic. An unparseable or backend-down judge is recorded as an
ERROR (never a silent pass), so run.py's all-errors fail-safe (exit 2) still holds.

A case may carry a pre-recorded `output` (then it is a fixture for testing the
judge itself) or omit it (then run.py's producer generates the output from the live
agent, so the eval gates the AGENT, not just the judge).
"""

from __future__ import annotations

import json
import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path

_SUITES_DIR = Path(__file__).parent / "suites"


@dataclass
class Criterion:
    name: str
    description: str = ""
    weight: float = 1.0


@dataclass
class JudgeCase:
    """One (prompt [+output]) -> rubric verdict expectation."""

    id: str
    suite: str
    prompt: str
    criteria: list[Criterion] = field(default_factory=list)
    reference: str = ""              # gold notes / what a good answer must contain
    output: str = ""                # pre-recorded answer to judge (optional)
    pass_threshold: float = 0.7
    context: list[str] = field(default_factory=list)
    domain: str = "general"
    source: str = "curated"
    tags: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "JudgeCase":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        kw = {k: v for k, v in d.items() if k in known}
        crit = kw.get("criteria") or []
        kw["criteria"] = [
            c if isinstance(c, Criterion) else Criterion(**c) for c in crit
        ]
        return cls(**kw)


def load_judge_suite(name_or_path: str | Path) -> list[JudgeCase]:
    """Load a judge suite by bare name (evals/suites/<name>.jsonl) or path."""
    path = Path(name_or_path)
    if not path.suffix:
        path = _SUITES_DIR / f"{path.name}.jsonl"
    cases: list[JudgeCase] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        cases.append(JudgeCase.from_dict(json.loads(line)))
    return cases


def build_judge_prompt(case: JudgeCase, output: str) -> tuple[str, str]:
    """(system, user) for the judge model. Asks for STRICT JSON with a 0..1 score
    per criterion plus a one-line rationale. 0..1 (not 1..5) is requested to avoid
    scale ambiguity; the parser tolerates 1..5 anyway."""
    crit_lines = "\n".join(
        f"- {c.name}: {c.description}".rstrip(": ").rstrip()
        for c in case.criteria
    ) or "- quality: overall correctness and usefulness"
    names = [c.name for c in case.criteria] or ["quality"]
    system = (
        "You are a strict evaluator of an AI assistant's answer. Score ONLY on the "
        "rubric. Reward correctness and grounding; penalize hallucination, padding, "
        "and off-topic content. Reply with EXACTLY one JSON object and no other "
        'text: {"scores": {<criterion>: <0..1>, ...}, "overall": <0..1>, '
        '"rationale": "<one line>"}.'
    )
    ref = f"\nReference (what a good answer should contain):\n{case.reference}\n" if case.reference else ""
    user = (
        f"Question / task:\n{case.prompt}\n"
        f"{ref}"
        f"\nRubric criteria (score each 0..1):\n{crit_lines}\n"
        f"\nAssistant's answer to evaluate:\n\"\"\"\n{output}\n\"\"\"\n"
        f"\nScore these criteria: {names}. Return the JSON object now."
    )
    return system, user


_JSON_OBJ = re.compile(r"\{.*\}", re.DOTALL)


def parse_verdict(raw: str, case: JudgeCase) -> "Verdict":
    """Parse a judge reply into a Verdict. Tolerant: extracts the first {...} block,
    normalizes a 1..5 scale to 0..1, and derives `overall` as the weighted mean of
    criterion scores when the judge omits it. Returns a Verdict with `error` set
    (passed=False) when nothing parseable is found — never a silent pass."""
    text = (raw or "").strip()
    m = _JSON_OBJ.search(text)
    if not m:
        return Verdict(passed=False, raw=text, error="no JSON object in judge reply")
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError as exc:
        return Verdict(passed=False, raw=text, error=f"bad judge JSON: {exc}")

    scores_raw = data.get("scores") or {}
    if not isinstance(scores_raw, dict):
        scores_raw = {}
    scores: dict[str, float] = {}
    for k, v in scores_raw.items():
        try:
            scores[str(k)] = float(v)
        except (TypeError, ValueError):
            continue

    overall_raw = data.get("overall")
    weights = {c.name: c.weight for c in case.criteria}

    def _norm(x: float) -> float:
        # 1..5 rubric -> 0..1; clamp into [0, 1].
        if x > 1.0:
            x = x / 5.0
        return max(0.0, min(1.0, x))

    scores = {k: _norm(v) for k, v in scores.items()}

    if isinstance(overall_raw, (int, float)):
        overall = _norm(float(overall_raw))
    elif scores:
        wsum = sum(weights.get(k, 1.0) for k in scores)
        overall = sum(scores[k] * weights.get(k, 1.0) for k in scores) / wsum if wsum else 0.0
    else:
        return Verdict(passed=False, raw=text, error="judge returned no scores")

    return Verdict(
        scores=scores,
        overall=overall,
        passed=overall >= case.pass_threshold,
        rationale=str(data.get("rationale", "")).strip(),
        raw=text,
    )


@dataclass
class Verdict:
    scores: dict[str, float] = field(default_factory=dict)
    overall: float = 0.0
    passed: bool = False
    rationale: str = ""
    raw: str = ""
    latency_ms: float = 0.0
    error: str = ""


@dataclass
class JudgeResult:
    case_id: str
    overall: float
    passed: bool
    scores: dict[str, float]
    rationale: str = ""
    latency_ms: float = 0.0
    error: str = ""


def score_judge(case: JudgeCase, verdict: Verdict) -> JudgeResult:
    return JudgeResult(
        case_id=case.id,
        overall=verdict.overall,
        passed=verdict.passed and not verdict.error,
        scores=dict(verdict.scores),
        rationale=verdict.rationale,
        latency_ms=verdict.latency_ms,
        error=verdict.error,
    )


@dataclass
class JudgeReport:
    n: int
    pass_rate: float
    mean_overall: float
    p50_latency_ms: float
    errors: int
    by_criterion: dict[str, float]
    failures: list[JudgeResult]

    @property
    def exact_acc(self) -> float:
        """Alias so the shared regression gate / baseline writer can treat pass_rate
        as the gated metric, exactly like the other eval kinds."""
        return self.pass_rate

    def summary(self) -> str:
        return (
            f"n={self.n}  pass_rate={self.pass_rate:.1%}  "
            f"mean_overall={self.mean_overall:.2f}  "
            f"p50={self.p50_latency_ms:.0f}ms  errors={self.errors}  "
            f"failures={len(self.failures)}"
        )

    def metrics(self) -> dict:
        return {
            "n": self.n,
            "exact_acc": round(self.pass_rate, 4),   # gated metric == pass_rate
            "pass_rate": round(self.pass_rate, 4),
            "mean_overall": round(self.mean_overall, 4),
            "p50_latency_ms": round(self.p50_latency_ms, 1),
            "errors": self.errors,
        }


def aggregate_judge(results: list[JudgeResult]) -> JudgeReport:
    n = len(results)
    if n == 0:
        return JudgeReport(0, 0.0, 0.0, 0.0, 0, {}, [])
    lat = [r.latency_ms for r in results if r.latency_ms > 0]
    by: dict[str, list[float]] = {}
    for r in results:
        for k, v in r.scores.items():
            by.setdefault(k, []).append(v)
    by_criterion = {k: sum(vs) / len(vs) for k, vs in by.items()}
    return JudgeReport(
        n=n,
        pass_rate=sum(r.passed for r in results) / n,
        mean_overall=sum(r.overall for r in results) / n,
        p50_latency_ms=statistics.median(lat) if lat else 0.0,
        errors=sum(1 for r in results if r.error),
        by_criterion=by_criterion,
        failures=[r for r in results if not r.passed],
    )
