"""Scoring: parse a model action string, compare to a case, aggregate a report.

`parse_action_string` mirrors ActionExecutor.parse_action (verb = first token,
target = remainder, tolerating a leading "Action:"/"Command:" label) so the eval's
notion of correctness matches what production actually dispatches. It is replicated
here rather than imported to keep the evals package free of heavy coordinator deps.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from typing import Any


def parse_action_string(action_str: str) -> tuple[str, dict[str, Any]]:
    """(verb, slots) from a raw action string. slots carries 'target' when present."""
    text = (action_str or "").strip()
    low = text.lower()
    for label in ("action:", "command:"):
        if low.startswith(label):
            text = text[len(label):].strip()
            break
    parts = text.split(None, 1)
    if not parts:
        return "", {}
    verb = parts[0].upper().rstrip(":")
    target = parts[1].strip() if len(parts) > 1 else ""
    slots: dict[str, Any] = {}
    if target:
        slots["target"] = target
    return verb, slots


def _norm(v: Any) -> str:
    return re.sub(r"\s+", " ", str(v).lower().strip())


def _slot_matches(key: str, expected: Any, predicted: dict[str, Any]) -> bool:
    """Per-key match rule. 'target'/'area' are fuzzy (containment either way);
    everything else is exact (severity compared as int)."""
    if key not in predicted:
        return False
    if key in ("target", "area"):
        e, p = _norm(expected), _norm(predicted[key])
        return bool(e) and (e in p or p in e)
    if key == "severity":
        try:
            return int(expected) == int(predicted[key])
        except (TypeError, ValueError):
            return False
    return _norm(expected) == _norm(predicted[key])


@dataclass
class Prediction:
    verb: str = ""
    slots: dict[str, Any] = field(default_factory=dict)
    raw: str = ""
    latency_ms: float = 0.0
    error: str = ""


@dataclass
class CaseResult:
    case_id: str
    expected_verb: str
    predicted_verb: str
    verb_ok: bool
    slots_ok: bool
    exact: bool
    latency_ms: float = 0.0
    error: str = ""


def score_case(case, pred: Prediction) -> CaseResult:
    """Score one prediction. slots_ok is True when every EXPECTED slot matches
    (an empty expected_slots — e.g. CLOSE/SCREENSHOT — is trivially satisfied)."""
    verb_ok = _norm(case.expected_verb) == _norm(pred.verb)
    slots_ok = all(
        _slot_matches(k, v, pred.slots) for k, v in case.expected_slots.items()
    )
    return CaseResult(
        case_id=case.id,
        expected_verb=case.expected_verb,
        predicted_verb=pred.verb,
        verb_ok=verb_ok,
        slots_ok=slots_ok,
        exact=verb_ok and slots_ok,
        latency_ms=pred.latency_ms,
        error=pred.error,
    )


@dataclass
class Report:
    n: int
    verb_acc: float
    exact_acc: float
    p50_latency_ms: float
    errors: int
    by_verb: dict[str, dict[str, int]]
    failures: list[CaseResult]

    def summary(self) -> str:
        return (
            f"n={self.n}  verb_acc={self.verb_acc:.1%}  exact_acc={self.exact_acc:.1%}  "
            f"p50={self.p50_latency_ms:.0f}ms  errors={self.errors}  "
            f"failures={len(self.failures)}"
        )

    def metrics(self) -> dict:
        return {
            "n": self.n,
            "verb_acc": round(self.verb_acc, 4),
            "exact_acc": round(self.exact_acc, 4),
            "p50_latency_ms": round(self.p50_latency_ms, 1),
            "errors": self.errors,
        }


def aggregate(results: list[CaseResult]) -> Report:
    n = len(results)
    if n == 0:
        return Report(0, 0.0, 0.0, 0.0, 0, {}, [])
    verb_ok = sum(r.verb_ok for r in results)
    exact = sum(r.exact for r in results)
    lat = [r.latency_ms for r in results if r.latency_ms > 0]
    by_verb: dict[str, dict[str, int]] = {}
    for r in results:
        b = by_verb.setdefault(r.expected_verb, {"n": 0, "verb_ok": 0, "exact": 0})
        b["n"] += 1
        b["verb_ok"] += int(r.verb_ok)
        b["exact"] += int(r.exact)
    return Report(
        n=n,
        verb_acc=verb_ok / n,
        exact_acc=exact / n,
        p50_latency_ms=statistics.median(lat) if lat else 0.0,
        errors=sum(1 for r in results if r.error),
        by_verb=by_verb,
        failures=[r for r in results if not r.exact],
    )


def check_regression(report: Report, baseline: dict) -> tuple[bool, str]:
    """Gate a report against a locked baseline.

    Returns (ok, message). An unset baseline (exact_acc is None) is informational
    — it passes but says so, so a first run is never a false failure.
    """
    base = baseline.get("exact_acc")
    if base is None:
        return True, "no baseline recorded — informational run (use --update-baseline)"
    tol = float(baseline.get("tolerance", 0.05))
    threshold = base - tol
    ok = report.exact_acc >= threshold
    verdict = "OK" if ok else "REGRESSION"
    return ok, (
        f"{verdict}: exact_acc={report.exact_acc:.1%} vs baseline {base:.1%} "
        f"(threshold {threshold:.1%}, tol {tol:.0%})"
    )
