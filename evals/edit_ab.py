"""Edit-format A/B eval — does a structured edit format beat whole-file overwrite?

The payoff experiment for ``specs/edit-format-aci`` (task 6). It holds the MODEL
FIXED and swaps only the WRITE_FILE edit format, then applies the model's output
through the REAL production ``EditApplier`` (same lint gate the live DevAgent
uses). This isolates the *format* as the only variable — a cleaner ablation than
routing through the nondeterministic planner, in the spirit of the slot eval's
focused-prompt proxy.

The dominant failure mode the spec targets is **silent elision**: a local model
re-typing a whole file drops a line it didn't mean to. Whole-file overwrite can't
detect that; a structured (hashline) edit never reproduces the surrounding code,
so it can't drop it. The ``preserved`` check is the direct measurement of that
failure mode — a case where the file's other methods vanish fails it.

Scoring is deterministic and model-free (this module is pure stdlib, unit-tested
without a GPU; only ``runner.run_edit_ab_suite`` touches the model):
    applied   — EditApplier produced text (no mismatch/overlap EditError)
    lint_ok   — the result passed the ``.py`` ast.parse gate (R1)
    intent    — every ``expect_contains`` present AND every ``expect_absent`` gone
    preserved — every ``preserve`` snippet survives (the silent-elision detector)
    correct   — applied AND lint_ok AND intent AND preserved

Mirrors ``evals/ablation.py``'s dataclass/report shape so the CLI prints/locks it
the same way.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median

from inference.edit_format import HASHLINE, WHOLE_FILE

_SUITES_DIR = Path(__file__).parent / "suites"
_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "edit_format"

# The two arms, in display order. whole_file is the legacy baseline.
ARMS = (WHOLE_FILE, HASHLINE)


@dataclass
class EditABCase:
    """One self-contained micro-edit + the deterministic checks for its result."""
    id: str
    suite: str
    fixture: str                                  # file under fixtures/edit_format/
    instruction: str                              # the editing task (model-facing)
    expect_contains: list[str] = field(default_factory=list)
    expect_absent: list[str] = field(default_factory=list)
    preserve: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "EditABCase":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})

    def fixture_path(self) -> Path:
        return _FIXTURES_DIR / self.fixture

    def base_text(self) -> str:
        return self.fixture_path().read_text(encoding="utf-8")


def load_edit_ab_suite(name: str) -> list[EditABCase]:
    """Load ``suites/<name>.jsonl`` (``#`` comment lines allowed)."""
    path = _SUITES_DIR / f"{name}.jsonl"
    cases: list[EditABCase] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        cases.append(EditABCase.from_dict(json.loads(s)))
    return cases


@dataclass
class ArmOutcome:
    """The result of running ONE arm (one format) for a case — built by the runner.

    ``ok``/``text`` describe a clean apply; ``reason``/``message`` describe an
    EditError. ``reason == 'syntax'`` means the edit was formed but the lint gate
    rejected it (applied-but-not-lint_ok).
    """
    arm: str                       # WHOLE_FILE | HASHLINE
    ok: bool = False               # EditApplier returned text
    text: str = ""                 # resulting file text (when ok)
    reason: str = ""               # EditError.reason when not ok ("syntax"|"mismatch"|...)
    message: str = ""              # EditError message / exception text
    body_len: int = 0              # chars the model emitted (cost proxy)
    latency_ms: float = 0.0


@dataclass
class ArmScore:
    arm: str
    applied: bool = False
    lint_ok: bool = False
    intent_ok: bool = False
    preserved: bool = False
    correct: bool = False
    body_len: int = 0
    latency_ms: float = 0.0
    note: str = ""                 # short failure reason for the per-case line


@dataclass
class EditABResult:
    case_id: str
    arms: dict = field(default_factory=dict)   # arm name -> ArmScore
    delta: float = 0.0                          # hashline.correct - whole_file.correct (per case: -1/0/1)
    error: str = ""

    def arm(self, name: str) -> "ArmScore | None":
        return self.arms.get(name)


def _contains_all(text: str, needles: list[str]) -> bool:
    return all(n in text for n in needles)


def _contains_none(text: str, needles: list[str]) -> bool:
    return not any(n in text for n in needles)


def score_arm(case: EditABCase, outcome: ArmOutcome) -> ArmScore:
    """Deterministically score one arm's outcome against the case's checks."""
    applied = outcome.ok or outcome.reason == "syntax"
    lint_ok = outcome.ok
    intent_ok = preserved = False
    note = ""
    if outcome.ok:
        text = outcome.text
        intent_ok = (_contains_all(text, case.expect_contains)
                     and _contains_none(text, case.expect_absent))
        preserved = _contains_all(text, case.preserve)
        if not preserved:
            missing = [p for p in case.preserve if p not in text]
            note = f"ELISION: dropped {missing[:3]}"
        elif not intent_ok:
            miss = [n for n in case.expect_contains if n not in text]
            bad = [n for n in case.expect_absent if n in text]
            note = f"intent miss={miss} present_but_forbidden={bad}"
    else:
        note = f"{outcome.reason or 'error'}: {outcome.message[:80]}"
    correct = bool(outcome.ok and intent_ok and preserved)
    return ArmScore(
        arm=outcome.arm, applied=applied, lint_ok=lint_ok, intent_ok=intent_ok,
        preserved=preserved, correct=correct, body_len=outcome.body_len,
        latency_ms=outcome.latency_ms, note=note,
    )


def score_edit_ab(case: EditABCase, outcomes: dict) -> EditABResult:
    """Score all arms for a case. ``outcomes`` maps arm name -> ArmOutcome."""
    arms = {name: score_arm(case, oc) for name, oc in outcomes.items()}
    wf = arms.get(WHOLE_FILE)
    hl = arms.get(HASHLINE)
    delta = 0.0
    if wf is not None and hl is not None:
        delta = float(hl.correct) - float(wf.correct)
    # A case "errors" (vs merely fails) only when EVERY arm hit a backend/io
    # error — the signal the CLI uses to detect an unreachable model and skip
    # (exit 2) instead of writing a false baseline.
    error = ""
    if outcomes and all(
        (not oc.ok) and oc.reason in ("io", "error") for oc in outcomes.values()
    ):
        first = next(iter(outcomes.values()))
        error = f"{first.reason}: {first.message}"
    return EditABResult(case_id=case.id, arms=arms, delta=delta, error=error)


@dataclass
class EditABReport:
    n: int
    rates: dict = field(default_factory=dict)         # arm -> correct rate
    counts: dict = field(default_factory=dict)        # arm -> dict(applied/lint/intent/preserved/correct/elision)
    mean_delta: float = 0.0                           # hashline rate - whole_file rate
    p50_latency_ms: dict = field(default_factory=dict)  # arm -> p50 latency
    mean_body_len: dict = field(default_factory=dict)   # arm -> mean emitted chars
    errors: int = 0
    results: list = field(default_factory=list)

    @property
    def failures(self) -> list:
        # For the shared CLI "failures" view: any case where an arm wasn't correct.
        return [r for r in self.results
                if r.error or any(not a.correct for a in r.arms.values())]

    @property
    def exact_acc(self) -> float:
        # Gated metric = the legacy whole_file correct-rate (the baseline we must
        # not regress). hashline's rate + the delta are recorded alongside.
        return self.rates.get(WHOLE_FILE, 0.0)

    def metrics(self) -> dict:
        m = {
            "n": self.n,
            "whole_file_rate": round(self.rates.get(WHOLE_FILE, 0.0), 4),
            "hashline_rate": round(self.rates.get(HASHLINE, 0.0), 4),
            "mean_delta": round(self.mean_delta, 4),
            "exact_acc": round(self.rates.get(WHOLE_FILE, 0.0), 4),
            "errors": self.errors,
        }
        for arm in ARMS:
            c = self.counts.get(arm, {})
            m[f"{arm}_elision_failures"] = c.get("elision", 0)
            m[f"{arm}_lint_rejections"] = c.get("lint_rejected", 0)
            m[f"{arm}_apply_errors"] = c.get("apply_error", 0)
            m[f"{arm}_p50_latency_ms"] = round(self.p50_latency_ms.get(arm, 0.0), 1)
            m[f"{arm}_mean_body_len"] = round(self.mean_body_len.get(arm, 0.0), 1)
        return m

    def summary(self) -> str:
        # ASCII-only (Windows cp1252 consoles can't encode arrows/Greek).
        wf = self.rates.get(WHOLE_FILE, 0.0)
        hl = self.rates.get(HASHLINE, 0.0)
        wfe = self.counts.get(WHOLE_FILE, {}).get("elision", 0)
        hle = self.counts.get(HASHLINE, {}).get("elision", 0)
        return (f"n={self.n}  whole_file={wf:.1%}  hashline={hl:.1%}  "
                f"delta={self.mean_delta:+.1%}  "
                f"elision(wf={wfe}/hl={hle})  errors={self.errors}")


def aggregate_edit_ab(results: list) -> EditABReport:
    n = len(results)
    if n == 0:
        return EditABReport(0)
    valid = [r for r in results if not r.error]
    rates: dict = {}
    counts: dict = {}
    p50: dict = {}
    body: dict = {}
    for arm in ARMS:
        scores = [r.arms[arm] for r in valid if arm in r.arms]
        k = len(scores) or 1
        rates[arm] = sum(s.correct for s in scores) / k
        counts[arm] = {
            "applied": sum(s.applied for s in scores),
            "lint_ok": sum(s.lint_ok for s in scores),
            "intent_ok": sum(s.intent_ok for s in scores),
            "preserved": sum(s.preserved for s in scores),
            "correct": sum(s.correct for s in scores),
            # elision = the edit applied & linted clean but dropped a preserved symbol
            "elision": sum(1 for s in scores if s.lint_ok and not s.preserved),
            "lint_rejected": sum(1 for s in scores if s.applied and not s.lint_ok),
            "apply_error": sum(1 for s in scores if not s.applied),
        }
        lats = [s.latency_ms for s in scores]
        p50[arm] = median(lats) if lats else 0.0
        blens = [s.body_len for s in scores]
        body[arm] = (sum(blens) / len(blens)) if blens else 0.0
    mean_delta = rates.get(HASHLINE, 0.0) - rates.get(WHOLE_FILE, 0.0)
    return EditABReport(
        n=n, rates=rates, counts=counts, mean_delta=mean_delta,
        p50_latency_ms=p50, mean_body_len=body,
        errors=sum(1 for r in results if r.error), results=results,
    )
