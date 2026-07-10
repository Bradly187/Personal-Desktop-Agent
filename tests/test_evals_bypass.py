"""Bypass-eval tests — the _BYPASS_SOURCES routing eval is MODEL-FREE, so this
exercises the real predictor end to end (no fakes) and runs in CI instantly.

specs/bugfix-b1-bypass-sources. The predictor scores the single-source-of-truth
core.routing_constants._BYPASS_SOURCES, so a reintroduced divergent copy (the
"multi" vs "multimodal" split-brain B1 fixed) is caught here and by the shared
baseline-lock gate.

Run: python -m pytest tests/test_evals_bypass.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from evals.corpus import EvalCase, load_suite
from evals.runner import bypass_predictor, run_suite


def _c(source: str) -> EvalCase:
    return EvalCase(id="x", suite="t", utterance=source, expected_verb="")


def test_multimodal_and_touch_bypass():
    predict = bypass_predictor()
    # The exact bug B1 fixed: FusionEngine emits source="multimodal" for voice-click.
    assert predict(_c("multimodal")).verb == "bypass"
    assert predict(_c("touch")).verb == "bypass"


def test_stale_multi_and_gated_sources_are_gated():
    predict = bypass_predictor()
    # "multi" was the stale event_dispatcher value emitted nowhere — it must NOT
    # be a bypass source; if the old tuple returns, this flips to "bypass".
    assert predict(_c("multi")).verb == "gate"
    for src in ("voice", "voice_local", "dev_agent", "trackpad", ""):
        assert predict(_c(src)).verb == "gate", src


def test_shipped_routing_suite_is_perfect():
    cases = load_suite("routing")
    assert len(cases) >= 6
    for c in cases:
        assert c.expected_verb in ("bypass", "gate"), f"{c.id}: bad label"
    rep = run_suite(cases, bypass_predictor())
    # Deterministic membership test over a curated suite — must be exact.
    assert rep.exact_acc == 1.0, rep.summary()
    assert rep.errors == 0
