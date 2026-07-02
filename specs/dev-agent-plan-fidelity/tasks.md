# Tasks: Dev-Agent Plan Fidelity

> Phase plan for [`requirements.md`](requirements.md). **Gate 2 (AGENTS.md #11):**
> this file is a DRAFT — no task executes until Brad approves it explicitly.
> Each task names its requirement(s), the files it touches, and its done-check.
> Phases are sequential: a later phase's scope can be cancelled by an earlier
> phase's measurement (that is the whole point of Phase 2).

**Status:** Approved (Brad, 2026-06-29) — Phases 0–3 done; Phase 4 CANCELLED by the
Phase-2 gate (D018); Phase 5 PR pending.

> **Outcome (2026-06-29):** grammar parity recovered `dev_critic` 0.875→1.0 and
> `dev_trajectory` 0.545→0.6364 (gate passes). Per R2.2 the gate recovered within
> tolerance → **Phase 4 (R3) not built**. Residual `dev_trajectory` misses are
> one-shot under-planning; the precondition to revisit R3 is an execution-mode
> (iterative) measurement. See [docs/decisions.md D018](../../docs/decisions.md).

---

## Phase 0 — Pre-flight (no behavior change)

- [x] **T0.1** Confirm Ollama is up with `qwen3-coder:30b` and capture the *current*
  pre-change numbers for `dev_trajectory` + `dev_critic` as a baseline-of-record in
  the PR description (54.5% / 87.5% observed 2026-06-29). _Refs: R1.4._
  _Done:_ both numbers pasted into the working notes; no files changed.

## Phase 1 — Eval grammar parity (R1)

- [x] **T1.1** Add an optional structured-output pass-through to the Ollama chat
  path: `OllamaInference._chat(..., format: dict | None = None)` →
  `if format: payload["format"] = format` ([inference/local_inference.py:630](../../inference/local_inference.py)).
  Byte-identical when `format is None`. _Refs: R1.1._
  _Done:_ existing `_chat` callers unaffected; a unit test asserts the key is
  present only when passed.
- [x] **T1.2** Thread `format=` through the eval plan predictor: `_infer_text`'s
  inner `infer_text` accepts an optional `format`; `plan_predictor` (and the
  `dev_critic` trajectory predictor) pass `_PLAN_JSON_SCHEMA`
  ([evals/run.py:59](../../evals/run.py), [evals/runner.py:545](../../evals/runner.py)).
  Import the schema from production — `from inference.model_router import _PLAN_JSON_SCHEMA`
  — never copy it. _Refs: R1.1, R1.3._
  _Done:_ a unit test (no model) asserts the predictor forwards the imported schema
  object identity.
- [x] **T1.3** Backend-capability guard: if the active backend has no `format=`
  equivalent, fall back to the unconstrained call and stamp the report
  `constrained=False`. _Refs: R1.2._
  _Done:_ report/JSON carries the flag; README note added if the field is new.
- [x] **T1.4** Add unit coverage to `tests/test_evals*.py` for T1.1–T1.3 (all
  model-free). _Done:_ `pytest tests/test_evals*.py` green.

## Phase 2 — Faithful re-measurement + decision (R2) — **GATING**

- [x] **T2.1** Re-run `dev_trajectory` and `dev_critic` under parity; record
  `exact_acc`, `mean_score`, `safe_acc`. _Refs: R2.1._
- [x] **T2.2** **Decision fork** ([docs/decisions.md](../../docs/decisions.md) D-entry, mandatory either way):
  - IF `exact_acc` is back within the baseline tolerance band → **STOP after Phase 3**;
    Phase 4 is cancelled (the regression was an eval-fidelity artifact). _Refs: R2.2._
  - ELSE genuine under-planning remains → **Phase 4 proceeds**. _Refs: R2.3._
  _Done:_ D-entry written with the parity numbers and the build/no-build call.

## Phase 3 — Re-lock baselines (R1.4) — runs in both forks

- [x] **T3.1** `python -m evals.run --suite dev_trajectory --mode trajectory --model qwen3-coder:30b --update-baseline`
  and the same for `dev_critic`; commit the regenerated `evals/baselines/*.json`.
- [x] **T3.2** If parity changed any reported metric's meaning, update
  `evals/README.md` (the "read these before fixing a low number" section). _Refs: R1.4._
  _Done:_ `--check` passes against the freshly-locked baselines.

## Phase 4 — Under-planning repair (R3) — **ONLY IF T2.2 says build**

- [~] **T4.1** (CANCELLED — see Outcome) Pure helper `is_underscoped(goal_is_imperative: bool, verbs: list[str]) -> bool`
  in [inference/dev_agent.py](../../inference/dev_agent.py) (read-only verb set per
  R3.1); unit-tested without a model. _Refs: R3.1, R3.3._
- [~] **T4.2** (CANCELLED — see Outcome) Add the `DA_PLAN_COMPLETENESS` flag (default OFF) — one row in the
  CLAUDE.md Feature Flags table; gate all new behavior behind it. _Refs: R3.4._
- [~] **T4.3** (CANCELLED — see Outcome) After `_parse_plan_json`, when the flag is ON and the plan is
  under-scoped on an imperative goal, route one corrective re-prompt through the
  existing `_replan` channel, bounded by `DA_PLAN_REPAIR_MAX=1`; a second
  under-scoped plan is accepted as-is. _Refs: R3.1, R3.2._
- [~] **T4.4** (CANCELLED — see Outcome) Read-only goals never trip the detector (preserve `safe_acc`); add a
  unit test proving an explain/find goal with a read-only plan is untouched.
  _Refs: R3.3._
- [~] **T4.5** (CANCELLED — see Outcome) Add a `dev_trajectory` case that fails pre-fix and passes post-fix;
  re-run the gate with `DA_PLAN_COMPLETENESS=1` and confirm `exact_acc` rises with
  `safe_acc` unchanged at 1.0. Re-lock if accepted. _Refs: R3.4._

## Phase 5 — Close-out

- [ ] **T5.1** Run `/doc-update`; flip `requirements.md` Status to `Shipped (PR #__)`.
- [ ] **T5.2** Open one PR off a feature branch (not `master`); paste before/after
  eval numbers in the body.

---

### Test/gate checklist (every PR)
- `pytest tests/test_evals*.py` green (model-free harness logic).
- `dev_trajectory` `safe_acc` MUST remain 1.0 (safety gate — never regress).
- No `_PLAN_PROMPT` whole-file rewrite (AGENTS.md #10); R3 edits are additive.
