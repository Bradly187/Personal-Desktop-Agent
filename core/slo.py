"""Per-domain Service-Level Objectives for routing (gap H).

Routing was governed by a single set of GLOBAL gate thresholds. Real workloads
differ wildly by domain: a `command` should resolve in ~hundreds of ms, while a
`plan` legitimately takes tens of seconds. This module is the single source of
per-domain SLO TARGETS (latency budget + success floor) plus the helpers that
turn observed stats into a verdict and estimate a query's difficulty.

What consumes it:
  - HybridCoordinator: Gate-4 sources its latency budget from the command SLO
    (and per-domain overrides the trainer may set).
  - ContinuousTrainer._adapt_per_domain_slo(): evaluates rolling per-domain
    inference stats against these SLOs and logs breaches (observability +
    operator signal). Aggressive auto-retuning and a learned route classifier
    are deferred (the latter is data-blocked — see experiments/routing_classifier).
  - estimate_difficulty(): an AVR-lite cheap difficulty score for difficulty-aware
    escalation (the cheapest model/route that can still meet the success SLO).

Targets are seeded from this project's measured/realistic numbers and are meant
to be tuned; nothing here mutates global state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class DomainSLO:
    latency_budget_ms: float   # p50 latency the domain is expected to meet
    min_success_rate: float    # acceptable success floor in [0, 1]


# Verdicts from evaluate().
OK = "ok"
BREACH_LATENCY = "breach_latency"
BREACH_SUCCESS = "breach_success"
HEADROOM = "headroom"

# command budget 600 ms preserves the historical Gate-4 default exactly; dev
# domains get realistic budgets (warm specialist calls are seconds, plans tens of
# seconds). Success floors are deliberately looser for open-ended dev domains.
_DEFAULT: dict[str, DomainSLO] = {
    "command": DomainSLO(latency_budget_ms=600.0,   min_success_rate=0.95),
    "code":    DomainSLO(latency_budget_ms=20000.0, min_success_rate=0.85),
    "math":    DomainSLO(latency_budget_ms=15000.0, min_success_rate=0.85),
    "vision":  DomainSLO(latency_budget_ms=8000.0,  min_success_rate=0.80),
    "plan":    DomainSLO(latency_budget_ms=30000.0, min_success_rate=0.80),
    "general": DomainSLO(latency_budget_ms=12000.0, min_success_rate=0.85),
}
_FALLBACK = DomainSLO(latency_budget_ms=10000.0, min_success_rate=0.85)


class SLOConfig:
    """Per-domain SLO lookup. Construct with `overrides` to tune individual domains."""

    def __init__(self, overrides: Optional[dict[str, DomainSLO]] = None) -> None:
        self._slos: dict[str, DomainSLO] = dict(_DEFAULT)
        if overrides:
            self._slos.update(overrides)

    def get(self, domain: str) -> DomainSLO:
        return self._slos.get(domain, _FALLBACK)

    def latency_budget_ms(self, domain: str) -> float:
        return self.get(domain).latency_budget_ms

    def domains(self) -> list[str]:
        return list(self._slos)


def evaluate(
    slo: DomainSLO,
    p50_latency_ms: Optional[float],
    success_rate: Optional[float],
    headroom_factor: float = 0.5,
) -> str:
    """Classify observed stats against an SLO.

    A success breach dominates (a wrong-but-fast answer is worse than a slow one).
    HEADROOM means comfortably under budget AND meeting success — a signal that a
    cheaper model/route might still satisfy the SLO.
    """
    if success_rate is not None and success_rate < slo.min_success_rate:
        return BREACH_SUCCESS
    if p50_latency_ms is not None and p50_latency_ms > slo.latency_budget_ms:
        return BREACH_LATENCY
    if (
        p50_latency_ms is not None
        and p50_latency_ms < slo.latency_budget_ms * headroom_factor
        and (success_rate is None or success_rate >= slo.min_success_rate)
    ):
        return HEADROOM
    return OK


# Multi-step / reasoning markers (mirror the coordinator's complexity keywords).
_DIFFICULTY_KEYWORDS = (
    "and then", "after that", "for each", "followed by", "then ",
    "while ", "repeat", "step by step", "refactor", "across", "all the",
)


def estimate_difficulty(text: str) -> float:
    """Cheap 0..1 difficulty estimate (AVR-lite) from length + multi-step markers.

    No model call. Used for difficulty-aware escalation: a high score means even a
    capable local model may miss the success SLO, so escalate. Length contributes
    up to 0.6, multi-step keywords up to 0.4.
    """
    if not text:
        return 0.0
    t = text.lower()
    length_score = min(len(t.split()) / 40.0, 1.0) * 0.6
    hits = sum(1 for k in _DIFFICULTY_KEYWORDS if k in t)
    keyword_score = min(hits * 0.2, 0.4)
    return round(min(length_score + keyword_score, 1.0), 3)
