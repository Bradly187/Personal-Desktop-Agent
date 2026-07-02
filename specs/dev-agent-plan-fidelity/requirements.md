# Spec: Dev-Agent Plan Fidelity — eval grammar-parity + under-planning repair

> One feature → one folder. This spec is the source of truth; the code is the
> build artifact. Design (§4) and Tasks (§6) are inline until they outgrow the file.

---

## 1. Background — the "Why"

The 2026-06-29 test+eval sweep flagged a reproducible regression in the
`dev_trajectory` eval (`exact_acc` 54.5% vs the 2026-06-14 baseline 72.7%) and a
softer one in `dev_critic` (87.5% vs 100%). **`safe_acc` stayed at 1.0** — this is
a plan-*completeness* signal, not a safety alarm (per `evals/README.md`).

Root-cause investigation found the regression is **not** model drift (the Ollama
`qwen3-coder:30b` snapshot is 2026-03-08, older than the baseline) and **not** a
suite/baseline edit. Two compounding causes:

1. **Eval-vs-production grammar gap (primary).** Production's plan path is
   grammar-constrained: the plan profile passes `_PLAN_JSON_SCHEMA` as Ollama's
   `format=` ([inference/model_router.py:393](../../inference/model_router.py),
   applied at `:1245`/`:1299`), physically forcing a `{"steps":[…]}` array. The
   trajectory eval's plan predictor calls `OllamaInference._chat(...)` **without**
   `format=` ([evals/run.py:59](../../evals/run.py)), so the model is free to
   revert to legacy bracket notation and emit a single line. Dumping raw plans
   confirmed this: `"fix the bug and commit"` → just `[READ_FILE src/utils.py]`;
   `"open a pull request"` → `[EXPLAIN …]`. Production's `format=` would forbid
   that shape. The constraint was strengthened **after** the baseline was locked,
   so the eval silently drifted out of fidelity with the real agent.

2. **Under-planning is still possible under the constraint (secondary).** The
   grammar forces the *shape* (a steps array) but **not the count** — a
   one-element `steps` array is schema-valid. So even with `format=` applied, the
   planner could under-scope an imperative goal. This must be *measured*, not
   assumed, before any prompt change.

The existing [`../dev-agent-plan-contract/`](../dev-agent-plan-contract/) spec
already repairs **malformed / unparseable** plans (unknown verb, empty parse). It
does **not** fire on a *valid-but-incomplete* plan — the parser accepts it. That
is the gap this spec addresses, smallest-first: close the eval-fidelity gap, then
(conditionally) add under-planning detection through the existing replan channel.

**Status:** Building → Shipped (PR #___)
**Approved:** Brad, 2026-06-29 (spec gate + tasks gate)
**Owner / author session:** Claude Code

---

## 2. Glossary

- **Plan path**: `ModelRouter` "plan" profile → DevAgent planner; emits a JSON
  `steps` array parsed by `_parse_plan_json` ([inference/dev_agent.py:234](../../inference/dev_agent.py)).
- **`_PLAN_JSON_SCHEMA`**: the Ollama `format=` grammar that constrains plan output
  to `{"steps":[{action,args,body,after}]}` ([inference/model_router.py:250](../../inference/model_router.py)).
- **Grammar parity**: the eval's plan predictor applying the *same* `format=`
  constraint production applies, so the eval scores what production runs.
- **Under-planning**: a schema-valid plan that omits an action the goal explicitly
  requested (e.g. imperative "fix and commit" goal whose plan has no
  `WRITE_FILE`/`EDIT_FILE` and no `GIT_COMMIT`). Distinct from a *malformed* plan.
- **Imperative goal**: a goal asking to create/write/change/fix/add/refactor/
  build/run/commit/push/open-PR (mirrors `_PLAN_PROMPT` lines 202–205).

---

## 3. Requirements (EARS acceptance criteria)

### Requirement 1: Eval grammar parity with production

**User Story:** As Brad, I want the trajectory eval to score the plan path under
the *same* grammar constraint production uses, so the number reflects the real
agent rather than an under-constrained proxy.

#### Acceptance Criteria
1. THE trajectory plan predictor SHALL submit `_PLAN_JSON_SCHEMA` as the inference
   `format=` whenever the model backend supports structured output (Ollama).
2. WHEN the backend does not support `format=`, THE predictor SHALL degrade to the
   current unconstrained call and record that the run was unconstrained (no silent
   parity claim).
3. THE parity path SHALL reuse production's schema object by import, never a
   hand-copied duplicate, so the two cannot drift (mirrors the `_PLAN_ACTIONS`
   sync policy already noted in `evals/trajectory.py`).
4. After parity lands, THE `dev_trajectory` and `dev_critic` baselines SHALL be
   re-recorded on a machine with Ollama up, and the new numbers logged.

### Requirement 2: Faithful re-measurement before any prompt change

**User Story:** As Brad, I want the genuine post-parity score measured before we
touch the planner prompt, so we fix the real problem and not an eval artifact.

#### Acceptance Criteria
1. THE re-measurement SHALL report `exact_acc`, `mean_score`, and `safe_acc` for
   `dev_trajectory` and `dev_critic` under grammar parity.
2. IF the post-parity `exact_acc` recovers to within the baseline tolerance band,
   THEN Requirement 3 SHALL NOT be built — the regression was an eval-fidelity
   artifact, and this spec closes after re-locking baselines (+ a `docs/decisions.md` entry).
3. IF genuine under-planning remains after parity, THEN Requirement 3 applies.

### Requirement 3: Under-planning detection + bounded repair (CONDITIONAL on R2.3)

**User Story:** As Brad, I want a plan that omits an explicitly-requested action to
be re-prompted once, so the agent doesn't silently stop after a single read step.

#### Acceptance Criteria
1. WHEN the goal is imperative AND the parsed plan contains only read-only verbs
   (`READ_FILE`,`GREP`,`SEARCH_PERSONAL`,`READ_SCREEN`,`FETCH_URL`,`EXPLAIN`),
   THE DevAgent SHALL treat the plan as under-scoped and re-prompt once through
   the existing `_replan` channel with a corrective note ("the goal asks to <X>;
   your plan took no write/commit/PR action — resend a complete plan").
2. THE re-prompt SHALL be bounded by the existing plan-repair budget
   (`DA_PLAN_REPAIR_MAX=1`); a second under-scoped plan SHALL be accepted as-is and
   surfaced, never looped.
3. IF the goal is read-only (explain/show/find/summarize), THEN R3.1 SHALL NOT
   fire — a read-only plan is correct (preserves `safe_acc`, AGENTS.md #4).
4. THE detector SHALL be behind a default-OFF feature flag (`DA_PLAN_COMPLETENESS`)
   so the change ships dark and is byte-identical to today when unset, until the
   eval gate confirms it improves `exact_acc` without lowering `safe_acc`.

---

## 4. Design sketch (inline until it grows)

- **R1**: in `evals/run.py::_infer_text` (or a plan-specific predictor variant),
  thread an optional `format=` through to `OllamaInference._chat`; have
  `plan_predictor` request it using `from inference.model_router import _PLAN_JSON_SCHEMA`.
  Verify `_chat`/`infer` already forward a `format`/`json_schema` kwarg to the
  Ollama payload; if not, add a minimal pass-through (no behavior change when None).
- **R3** (if needed): a pure helper `is_underscoped(goal_is_imperative, verbs) -> bool`
  in `inference/dev_agent.py`, unit-tested without a model, called right after
  `_parse_plan_json`; reuses `_replan`. Goal-imperative classification reuses the
  same lexical signal `_PLAN_PROMPT` already encodes (no new model call).

## 5. Non-goals

- Rewriting `_PLAN_PROMPT` wholesale (the scope/tool-selection rules at lines
  201–215 are already correct; whole-file regen would drop them — AGENTS.md #10).
- Forcing a minimum step count in the grammar (the schema constrains shape, not
  semantics; count is goal-dependent).
- Touching the cloud plan path's constraint (covered by `../dev-agent-plan-contract/`).

## 6. Tasks

The phase plan lives in [`tasks.md`](tasks.md) (gate-2 approval pending). Phases:
(0) pre-flight numbers, (1) eval grammar parity, (2) **gating** re-measurement +
build/no-build decision, (3) re-lock baselines, (4) under-planning repair —
*conditional on Phase 2*, (5) close-out.
