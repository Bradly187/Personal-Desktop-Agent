# Tasks — B1: Unify `_BYPASS_SOURCES`

> **Gate 2 — awaiting Brad's approval before any task executes.**
> Reference spec: `specs/bugfix-b1-bypass-sources/requirements.md`

---

## Tasks

- [x] 1. **Create `core/routing_constants.py`** — pure-constants leaf module; no imports from `core`/`inference`/`storage`.
  - Contents: `_BYPASS_SOURCES: frozenset[str] = frozenset({"touch", "multimodal"})`
  - Contents: `_SKIP_GATE1_SOURCES: frozenset[str] = frozenset({"voice_local"})`
  - Satisfies R1.1, R1.2, R1.4

- [x] 2. **Update `core/event_dispatcher.py`** — remove local `_BYPASS_SOURCES = ("touch", "multi")` at line 20; add `from core.routing_constants import _BYPASS_SOURCES`.
  - Satisfies R1.3, R1.5

- [x] 3. **Update `core/hybrid_coordinator.py`** — remove local `_BYPASS_SOURCES = {"touch", "multimodal"}` (line 227) and `_SKIP_GATE1_SOURCES` (line 228); add `from core.routing_constants import _BYPASS_SOURCES, _SKIP_GATE1_SOURCES`.
  - Satisfies R1.3

- [x] 4. **Add unit tests** — `tests/test_routing_constants.py`:
  - Assert `"multimodal" in _BYPASS_SOURCES` and `"multi" not in _BYPASS_SOURCES` (R1.4)
  - Assert `"touch" in _BYPASS_SOURCES` (R2.1)
  - Assert `_BYPASS_SOURCES` is a `frozenset` (not a tuple — prevents accidental mutation)
  - Assert only one module in `core/` defines `_BYPASS_SOURCES` at module level
  - Mock `route_impl` path: `source="multimodal"` → de-glue skipped (R1.5)
  - Mock `route_impl` path: `source="touch"` → de-glue skipped (R2.1)

- [x] 5. **Add eval case** — `evals/suites/routing.jsonl`: `source=multimodal` → bypass verdict (R2.2); `source=touch` → bypass verdict (R2.3). Lock baseline.

  **Result (2026-07-10):** Added a MODEL-FREE `bypass` predictor
  (`evals/runner.py:bypass_predictor`, wired into `evals/run.py --predictor bypass`)
  that scores the single-source-of-truth `core.routing_constants._BYPASS_SOURCES` —
  so a reintroduced divergent copy moves the score. Suite `evals/suites/routing.jsonl`
  (9 cases incl. the `multi`→gate stale-value regression guard and `multimodal`→bypass
  bug case). Baseline `evals/baselines/routing.json` locked: n=9, `exact_acc=1.0`,
  errors=0; `--check` passes. Wired into CI (`scripts/run_evals.ps1` "bypass gate")
  and the instant pytest gate (`tests/test_evals_bypass.py`, 3 tests).

- [x] 6. **Run full test suite** — `pytest -x`; confirm green.
