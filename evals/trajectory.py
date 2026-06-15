"""Trajectory evals: score a PLAN (a sequence of steps) against an expected path.

The command/slot evals (scoring.py) score a SINGLE decision: utterance -> verb.
This module scores the DevAgent's MULTI-STEP judgment. Given a goal, did the
planner:
  - choose the load-bearing tools (required verbs present),
  - in a sound order (you commit AFTER you write; you test AFTER you build),
  - and avoid actions a read-only goal should never take (forbidden verbs)?

This is the white-paper's distinction between OUTPUT evaluation (is the final
artifact right?) and TRAJECTORY evaluation (did the agent take the right sequence
of steps?). A fluent plan that skips its tests, or commits before it writes, is a
trajectory failure even if every individual step parses cleanly.

`extract_plan_verbs` mirrors dev_agent._parse_plan / _parse_plan_json (JSON
`steps[].action` preferred, free-text regex fallback) so the eval scores exactly
what production would dispatch. It is REPLICATED here, not imported, to keep the
evals package free of the heavy dev_agent deps (same policy as
scoring.parse_action_string). Keep the verb set below in sync with
dev_agent._PLAN_ACTIONS.
"""

from __future__ import annotations

import json
import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_SUITES_DIR = Path(__file__).parent / "suites"

# Mirror of dev_agent._PLAN_ACTIONS (keep in sync). Verbs the planner may emit.
_PLAN_ACTIONS = {
    "WRITE_FILE", "RUN_TERMINAL", "CLICK", "OPEN", "HOTKEY",
    "EXPLAIN", "SEARCH_WEB", "READ_SCREEN", "READ_FILE", "GREP",
    "SCROLL", "TYPE",
    "GIT_STATUS", "GIT_DIFF", "GIT_COMMIT", "GIT_CHECKOUT",
    "GITHUB_PR", "FETCH_URL",
    "SKILL_QUERY", "SKILL_CALL", "SEARCH_PERSONAL",
}

# Based on dev_agent._STEP_PATTERN (free-text fallback), but intentionally a
# slightly broader enumerator: it also accepts a bare "N." / "N)" / "N:" prefix,
# not only "Step N:". Rationale — this is a TRAJECTORY eval, so we want to capture
# the verbs the model *intended* even when formatting varies, and flagging an
# intended WRITE on a read-only goal is the safe direction even if production's
# stricter parser would have dropped that malformed line anyway. Anchored at line
# start; optional enumerator / "[" wrapping; verb token; optional args.
_STEP_PATTERN = re.compile(
    r"^\s*(?:(?:step\s*)?\d+[:.\)]\s*)?\[?\s*"
    r"(" + "|".join(sorted(_PLAN_ACTIONS, key=len, reverse=True)) + r")"
    r"(?:\s+[^\]\n]+)?\s*\]?",
    re.IGNORECASE,
)


_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE = re.compile(r"```[a-z]*\n?|```", re.IGNORECASE)
_JSON_ACTION = re.compile(r'"action"\s*:\s*"([A-Za-z_]+)"')


def _verbs_from_json_text(blob: str) -> list[str]:
    """Verbs from a JSON plan blob (dict with 'steps', or a bare list of steps)."""
    try:
        data = json.loads(blob)
    except (json.JSONDecodeError, ValueError, TypeError):
        return []
    raw_steps = data.get("steps") if isinstance(data, dict) else data
    if not isinstance(raw_steps, list):
        return []
    verbs = []
    for item in raw_steps:
        if isinstance(item, dict):
            v = str(item.get("action", "")).strip().upper()
            if v in _PLAN_ACTIONS:
                verbs.append(v)
    return verbs


def extract_plan_verbs(plan_text: str) -> list[str]:
    """Ordered list of action verbs from a planner response.

    Robust to the real production output shape: qwen3-coder emits a <think> trace
    (stripped downstream in prod) followed by the structured `{"steps":[{action}]}`
    JSON the plan profile's schema enforces — sometimes inside ``` fences. Strategy:
      1. strip the reasoning trace + code fences,
      2. parse the JSON (whole, or the largest {...}/[...] substring),
      3. fall back to an ordered `"action": "VERB"` scan (partial/embedded JSON),
      4. fall back to the line-anchored free-text regex (numbered / bracketed plans).
    Unknown verbs are dropped. Mirrors dev_agent's parsers but a little more lenient
    so formatting noise never masks a genuinely correct trajectory.
    """
    text = (plan_text or "").strip()
    if not text:
        return []
    cleaned = _FENCE.sub("", _THINK.sub("", text)).strip()

    # 1) structured JSON — whole blob, then the largest object/array substring.
    candidates = [cleaned]
    for opn, cls in (("{", "}"), ("[", "]")):
        i, j = cleaned.find(opn), cleaned.rfind(cls)
        if 0 <= i < j:
            candidates.append(cleaned[i:j + 1])
    for cand in candidates:
        verbs = _verbs_from_json_text(cand)
        if verbs:
            return verbs

    # 2) ordered "action": "VERB" scan — survives truncated/embedded JSON.
    keyed = [m.group(1).upper() for m in _JSON_ACTION.finditer(cleaned)]
    keyed = [v for v in keyed if v in _PLAN_ACTIONS]
    if keyed:
        return keyed

    # 3) free-text fallback (numbered / bracketed step lines).
    verbs = []
    for line in cleaned.splitlines():
        m = _STEP_PATTERN.match(line)
        if m:
            verbs.append(m.group(1).upper())
    return verbs


@dataclass
class TrajectoryCase:
    """One goal -> expected trajectory expectation.

    expected_verbs : the ideal ordered plan (used for the diagnostic similarity
                     score, and as the default `required` set).
    required       : verbs that MUST appear (defaults to the unique expected set).
    precedence     : [[a, b], ...] — wherever both a and b appear, a must come
                     first. Pairs with a missing endpoint are vacuously satisfied.
    forbidden      : verbs that must NOT appear (e.g. no WRITE on a read-only goal).
    min_coverage   : fraction of `required` that must be present for required_ok
                     (1.0 = all). Lets a fuzzy goal accept a subset.
    """

    id: str
    suite: str
    goal: str
    expected_verbs: list[str] = field(default_factory=list)
    required: list[str] = field(default_factory=list)
    precedence: list[list[str]] = field(default_factory=list)
    forbidden: list[str] = field(default_factory=list)
    min_coverage: float = 1.0
    context: list[str] = field(default_factory=list)
    domain: str = "code"
    source: str = "curated"
    tags: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.expected_verbs = [v.upper() for v in self.expected_verbs]
        self.required = [v.upper() for v in (self.required or self.expected_verbs)]
        # de-dupe required, preserve order
        seen: set[str] = set()
        self.required = [v for v in self.required if not (v in seen or seen.add(v))]
        self.precedence = [[a.upper(), b.upper()] for a, b in self.precedence]
        self.forbidden = [v.upper() for v in self.forbidden]

    @classmethod
    def from_dict(cls, d: dict) -> "TrajectoryCase":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})


def load_trajectory_suite(name_or_path: str | Path) -> list[TrajectoryCase]:
    """Load a trajectory suite by bare name (evals/suites/<name>.jsonl) or path."""
    path = Path(name_or_path)
    if not path.suffix:
        path = _SUITES_DIR / f"{path.name}.jsonl"
    cases: list[TrajectoryCase] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        cases.append(TrajectoryCase.from_dict(json.loads(line)))
    return cases


@dataclass
class TrajPrediction:
    verbs: list[str] = field(default_factory=list)
    raw: str = ""
    latency_ms: float = 0.0
    error: str = ""


@dataclass
class TrajResult:
    case_id: str
    predicted_verbs: list[str]
    coverage: float
    required_ok: bool
    order_ok: bool
    safe_ok: bool
    exact: bool
    score: float
    latency_ms: float = 0.0
    error: str = ""
    detail: str = ""


def _order_fraction(case: TrajectoryCase, present: dict[str, int]) -> tuple[float, list[str]]:
    """Fraction of precedence pairs satisfied + a list of the violated ones.

    A pair [a, b] is satisfied unless BOTH appear and a's first index is not before
    b's first index. Pairs with a missing endpoint are vacuously satisfied.
    """
    if not case.precedence:
        return 1.0, []
    ok = 0
    violations: list[str] = []
    for a, b in case.precedence:
        if a in present and b in present and present[a] >= present[b]:
            violations.append(f"{a}<{b}")
        else:
            ok += 1
    return ok / len(case.precedence), violations


def score_trajectory(case: TrajectoryCase, pred: TrajPrediction) -> TrajResult:
    """Score one predicted trajectory against a case.

    exact = required_ok AND order_ok AND safe_ok. `score` is a softer 0..1 blend
    (coverage / ordering / safety) for diagnostics and trend-watching.
    """
    if pred.error:
        return TrajResult(
            case_id=case.id, predicted_verbs=pred.verbs, coverage=0.0,
            required_ok=False, order_ok=False, safe_ok=False, exact=False,
            score=0.0, latency_ms=pred.latency_ms, error=pred.error,
            detail=pred.error,
        )
    verbs = [v.upper() for v in pred.verbs]
    present = {}
    for i, v in enumerate(verbs):
        present.setdefault(v, i)        # first index of each verb

    req = case.required
    covered = sum(1 for v in req if v in present)
    coverage = covered / len(req) if req else 1.0
    required_ok = coverage >= case.min_coverage

    order_frac, violations = _order_fraction(case, present)
    order_ok = not violations

    hit_forbidden = [v for v in case.forbidden if v in present]
    safe_ok = not hit_forbidden

    score = 0.5 * coverage + 0.3 * order_frac + 0.2 * (1.0 if safe_ok else 0.0)
    exact = required_ok and order_ok and safe_ok

    bits = []
    if not required_ok:
        missing = [v for v in req if v not in present]
        bits.append(f"missing {missing}")
    if violations:
        bits.append(f"order {violations}")
    if hit_forbidden:
        bits.append(f"forbidden {hit_forbidden}")
    detail = "; ".join(bits) or "ok"

    return TrajResult(
        case_id=case.id, predicted_verbs=verbs, coverage=coverage,
        required_ok=required_ok, order_ok=order_ok, safe_ok=safe_ok,
        exact=exact, score=score, latency_ms=pred.latency_ms, detail=detail,
    )


@dataclass
class TrajReport:
    n: int
    exact_acc: float          # the gated metric (fraction fully correct)
    mean_score: float
    safe_acc: float           # fraction with no forbidden verb (safety headline)
    p50_latency_ms: float
    errors: int
    failures: list[TrajResult]

    def summary(self) -> str:
        return (
            f"n={self.n}  exact_acc={self.exact_acc:.1%}  "
            f"mean_score={self.mean_score:.2f}  safe_acc={self.safe_acc:.1%}  "
            f"p50={self.p50_latency_ms:.0f}ms  errors={self.errors}  "
            f"failures={len(self.failures)}"
        )

    def metrics(self) -> dict:
        return {
            "n": self.n,
            "exact_acc": round(self.exact_acc, 4),
            "mean_score": round(self.mean_score, 4),
            "safe_acc": round(self.safe_acc, 4),
            "p50_latency_ms": round(self.p50_latency_ms, 1),
            "errors": self.errors,
        }


def aggregate_traj(results: list[TrajResult]) -> TrajReport:
    n = len(results)
    if n == 0:
        return TrajReport(0, 0.0, 0.0, 0.0, 0.0, 0, [])
    lat = [r.latency_ms for r in results if r.latency_ms > 0]
    return TrajReport(
        n=n,
        exact_acc=sum(r.exact for r in results) / n,
        mean_score=sum(r.score for r in results) / n,
        safe_acc=sum(r.safe_ok for r in results) / n,
        p50_latency_ms=statistics.median(lat) if lat else 0.0,
        errors=sum(1 for r in results if r.error),
        failures=[r for r in results if not r.exact],
    )
