# Spec: Dev-Agent Plan Contract — auto-repair on malformed/partial plans

> One feature → one folder. This spec is the source of truth; the code is the
> build artifact. Design and Tasks are inline (§4–§6) until they outgrow the file.

---

## 1. Background — the "Why"

The DevAgent's planner emits a structured plan that `_parse_plan_json`
([inference/dev_agent.py:234](../../inference/dev_agent.py)) turns into
`AgentStep`s. The local plan path is **grammar-constrained** — the plan profile
passes `_PLAN_JSON_SCHEMA` as Ollama's `format=`
([inference/model_router.py:246](../../inference/model_router.py)), so the model
is physically forced to emit a `steps` array with an `action` enum. That half is
already best-practice.

The **failure half is not**. Two silent holes:

- **Silent step drop.** When a step has an unknown verb or isn't a dict,
  `_parse_plan_json` `continue`-skips it ([inference/dev_agent.py:249-253](../../inference/dev_agent.py)).
  A dropped step becomes *a missing action the agent never notices* — not a caught
  error. If both JSON and the regex fallback (`_parse_plan`) yield nothing, the
  agent gets an **empty plan and halts** with no corrective attempt.
- **Cloud path is unconstrained.** The Bedrock plan path has no `format=`
  equivalent, so a cloud planner can emit malformed JSON that only the lenient
  text parser catches — and then silently drops from.

The standard "Strict I/O Contract" pattern pairs grammar-constrained decoding with
an **auto-repair loop**: when output violates the schema, the runtime *bypasses
execution*, catches the validation error, and feeds it straight back to the model
("you emitted X; the schema requires Y; resend"). We already do exactly this for
**file edits** — `EditError` from the lint gate is fed to `_replan` before any
write ([../edit-format-aci/](../edit-format-aci/)). This spec brings the same
auto-repair discipline to the **plan structure** itself, and constrains the cloud
plan path where the backend supports it.

Smallest-first: (a) replace silent drop / empty-halt with a **bounded corrective
re-prompt** through the existing replan channel; (b) constrain the cloud plan path
via the backend's structured-output mechanism, degrading to (a) where unsupported.

**Status:** Draft
**Owner / author session:** Claude Code (Opus 4.8)
**Related:** `../edit-format-aci/` (the `EditError`→`_replan` auto-repair this
mirrors for plans), `../dev-agent-critic/` (sibling sprint — the Critic reviews a
*parsed* plan, so this lands first), `../trajectory-reduction/` (same
plan→replan loop). Honors AGENTS.md #4 (fail-safe: repair-exhausted → halt/CLARIFY,
never execute a guessed plan), #6 (no new model), #1 (no schema change).

---

## 2. Glossary

- **Plan parse**: `_parse_plan_json` (structured) → `_parse_plan` (regex fallback)
  → list of `AgentStep`. Today drops bad steps silently and halts on empty.
- **Auto-repair re-prompt**: a single, bounded follow-up planner call carrying a
  corrective message that names what failed (unknown verb, dropped step count,
  empty/unparseable output) and restates the required schema. Reuses the planner
  model already loaded — no new model, no new channel.
- **PlanParseReport**: structured result of a parse attempt — `steps`,
  `dropped` (list of `{index, raw_action, reason}`), `parsed_ok` (bool). Drives
  whether a repair re-prompt fires. A dataclass, never a raw dict across the
  boundary.
- **Cloud structured output**: the Bedrock backend's mechanism for constraining
  output to a schema (tool-use / JSON response). Used to give the cloud plan path
  the decode-time guarantee the local `format=` path already has.

---

## 3. Requirements (EARS acceptance criteria)

### Requirement 1: Surface, don't swallow, a partial parse

**User Story:** As Brad, I want a dropped or malformed plan step to trigger a
corrective retry instead of vanishing, so the agent doesn't quietly skip an
action it meant to take.

#### Acceptance Criteria
1. WHEN `_parse_plan_json` skips one or more steps (unknown verb / non-dict item),
   THE planner SHALL produce a `PlanParseReport` recording each dropped step
   (index, raw action, reason) rather than discarding it silently.
2. WHEN a parse drops ≥1 step OR yields an empty plan while the raw model output
   was non-empty, THE DevAgent SHALL issue **one** bounded auto-repair re-prompt
   carrying a corrective message that names the offending content and restates the
   required schema, BEFORE halting.
3. THE corrective message SHALL be surfaced through the existing `_replan`/planner
   feedback channel — no new feedback path (mirrors `EditError` in
   `../edit-format-aci/`).
4. THE auto-repair re-prompt count SHALL be bounded by config
   (`max_repair_prompts`, default 1); once exhausted THE DevAgent SHALL halt with a
   clearly-surfaced "could not produce a valid plan" message.
5. IF repair is exhausted, THEN THE DevAgent SHALL fail safe — halt / CLARIFY and
   execute NOTHING, never run a partial or guessed plan (AGENTS.md #4).

### Requirement 2: Constrain the cloud plan path — DEFERRED (no current surface)

> **Finding (2026-06-21, verified against the code):** there is no executable
> cloud plan path to constrain. `DevAgent.plan_and_run` generates executable plans
> only via `ModelRouter.infer` ([inference/model_router.py:1060](../../inference/model_router.py)),
> which routes to **vLLM/Ollama only — no cloud branch** (the local path already
> carries the `format=_PLAN_JSON_SCHEMA` grammar constraint). The cloud agent
> (`CloudDevAgent`) is reached on a *separate* coordinator branch
> ([core/hybrid_coordinator.py:1068](../../core/hybrid_coordinator.py)) that calls
> `CloudDevAgent.run(...)` and returns `"steps": 0` — its `domain="plan"` output is
> **advisory numbered text shown to the user, never parsed into executable steps**.
>
> Therefore the R2 premise ("a Bedrock plan the parser has to guess at") does not
> occur. Forcing that advisory text into JSON would *regress* its readability for
> zero execution benefit. And R1's auto-repair is **backend-agnostic** — if a
> future change ever routes a cloud-generated plan into `_acquire_plan_steps`, the
> repair loop already covers malformed JSON with no R2-specific code.
>
> **R2 is deferred until/unless the architecture makes the cloud an
> executable-plan source.** Re-open this requirement then; do not add speculative
> tool-use plumbing now (AGENTS.md #10 — no untested dead code).

#### Acceptance Criteria (deferred)
1. ~~constrain the cloud plan response at decode time~~ — N/A: no executable cloud
   plan path exists (see finding above).
2. THE local plan path (`format=_PLAN_JSON_SCHEMA`) SHALL remain unchanged (met —
   R2 added nothing).
3. IF a future change routes a cloud plan into the executor, THEN R1 auto-repair
   SHALL cover malformed output (already true — backend-agnostic).

### Requirement 3: Flag-gated, observable, eval-gated

**User Story:** As Brad, I want this proven on evals before it changes default
behavior, like every other dev-agent change.

#### Acceptance Criteria
1. THE auto-repair loop and the cloud constraint SHALL each ship behind config,
   **default OFF**; with both unset the plan-parse path is byte-identical to today
   (silent-drop preserved only until the eval baseline confirms the repair path is
   a net win).
2. THE auto-repair re-prompt SHALL emit the existing inference span (tokens/cost
   via `cost_ledger`) so the added cost of a retry is observable.
3. THE feature SHALL NOT be made default-on until its `evals/` baseline is locked
   and shows fewer silently-dropped steps with no correctness regression.

---

## 4. Technical Design

> Hooks into the **DevAgent** plan-parse + planning loop and the **cloud backend**
> only. No `CommandExecutor`, sandbox, or `ipad_bridge` changes.

- **Entry point / pipeline boundary:** the plan-acquisition site in
  `plan_and_run`/`_plan_and_run_locked` ([inference/dev_agent.py:541](../../inference/dev_agent.py))
  and `_parse_plan_json` ([inference/dev_agent.py:234](../../inference/dev_agent.py)).
  `_parse_plan_json` returns a `PlanParseReport` (or the caller wraps it); the
  planning loop inspects `dropped`/`parsed_ok` and fires at most
  `max_repair_prompts` corrective re-prompts before the existing halt path.
- **New dataclass (never a raw dict):** `PlanParseReport(steps, dropped, parsed_ok)`
  + `DroppedStep(index, raw_action, reason)` in `inference/dev_agent.py` (local to
  the planner).
- **Corrective prompt builder:** a small helper that renders the schema reminder +
  the named failures (e.g. `step 3 used unknown verb "EDITT"; valid verbs: …` /
  `no parseable steps array`). Reuses `_PLAN_VERBS`/`_PLAN_JSON_SCHEMA` as the
  source of truth so it can't drift.
- **Cloud constraint:** add a structured-output path in `core/cloud_backend.py`
  (Bedrock) — pass the plan JSON schema via the backend's tool-use/JSON mechanism
  when the active model supports it; `ModelRouter` selects it for the plan domain.
  Falls back to free-text + R1 when unsupported.
- **Models / VRAM (AGENTS.md #6):** no new model; the repair re-prompt reuses the
  already-loaded planner. Cloud constraint is a request-shaping change, no roster
  impact.
- **60 Hz loop (AGENTS.md #2):** unaffected — async DevAgent path only.
- **Persistence:** none. No `agent.db` schema change; `PRAGMA user_version`
  unchanged (AGENTS.md #1). Dropped-step events go to the existing audit log via
  `fire_and_log`.
- **Cross-platform:** none — no `ipad_bridge` payload change (AGENTS.md #3 N/A).

### Configuration (flat YAML)

```yaml
dev_agent_plan_contract:
  auto_repair:
    enabled: false           # OFF → today's silent-drop/halt, byte-identical
    max_repair_prompts: 1     # bounded corrective re-prompts before halt
  cloud_constraint:
    enabled: false           # constrain Bedrock plan output to the schema
                              # (degrades to free-text + auto_repair if unsupported)
```

---

## 5. Behavior Verification (executable, not prose)

- **Unit tests:** `tests/test_plan_contract.py` — one assertion per criterion,
  planner model stubbed (CI-safe):
  - R1.1: a plan with an unknown verb yields a `PlanParseReport` listing the drop.
  - R1.2/R1.3: a drop / empty-but-nonempty-raw fires exactly one corrective
    re-prompt through the existing channel; the re-prompt names the offender.
  - R1.4/R1.5: repair bounded by `max_repair_prompts`; exhaustion halts and runs
    nothing (fail-safe), never a partial plan.
  - R2.2: cloud-unsupported path degrades to free-text + auto-repair, no crash.
  - R3.1: flags OFF → parse path byte-identical to the pre-feature snapshot.
- **Eval suite:** add cases under `evals/suites/dev_execution.jsonl` (or a new
  `plan_contract` mode) feeding planner outputs with intentional unknown-verb /
  truncated-JSON / empty responses; assert the repair loop recovers a valid plan
  where one turn could, and that nothing executes when it can't. Lock the baseline
  in `evals/baselines/`. **Do NOT flip defaults until it passes** (see
  `running-the-eval-harness` skill).

Each criterion in §3 maps to at least one test/eval above.

---

## 6. Tasks

- [x] 1. `PlanParseReport`/`DroppedStep` + `_parse_plan_json_report` records drops
      instead of swallowing them; `_parse_plan_json` kept as a back-compat raising
      wrapper — satisfies R1.1.
- [x] 2. `_build_plan_repair_prompt` (schema reminder + named failures) reusing
      `_PLAN_ACTIONS` as the valid-verb source — satisfies R1.3.
- [x] 3. `_acquire_plan_steps` bounded auto-repair wired into `_plan_and_run_locked`
      (structured → regex → repair → EXPLAIN fail-safe); `DA_PLAN_REPAIR` (default
      OFF) + `DA_PLAN_REPAIR_MAX` (default 1) instance flags — satisfies R1.2, R1.4,
      R1.5, R3.1, and R3.2 (the repair re-infer emits its own inference span).
- [~] 4. Cloud structured-output path — **DEFERRED (no current surface).** Verified
      the executable plan path (`plan_and_run` → `ModelRouter.infer`) is local-only
      (vLLM/Ollama, already `format=`-constrained); the cloud agent's plan output
      is advisory text (`steps: 0`), never parsed for execution. R1 is
      backend-agnostic, so a future cloud-executable plan is already covered. No
      code added (AGENTS.md #10). See R2 finding.
- [~] 5. Config plumbing — env flags done (instance attrs, test-overridable);
      config-file (`dev_agent_plan_contract.*`) mapping + audit-log drop events
      pending. R3.1/R3.2 met via env flags + inference span; WARNING surfaces drops.
- [x] 5t. `tests/test_plan_contract.py` — 13 tests over R1.1/R1.2/R1.3/R1.4/R1.5/
      R3.1 + regex-rescue + back-compat wrapper.
- [x] 6. **Model-free eval shipped + baseline locked.** `evals/plan_contract.py`
      (self-contained, like `evals/token_budget.py`) runs the REAL
      `_acquire_plan_steps` over `evals/suites/plan_contract.jsonl` (8 scripted
      scenarios: recover-unknown-verb / truncated-JSON / empty → recovery,
      unrecoverable → fail-safe-empty, regex-rescue, clean, disabled, bounded),
      scoring recovered-actions + re-prompt-count deterministically. Baseline
      `evals/baselines/plan_contract.json` locked at `exact_acc=1.0` (tol 0.0;
      deterministic — any regression is a real bug). `--check` is the gate; 6
      CI-safe scorer tests (`tests/test_evals_plan_contract.py`). Satisfies R3.3.
- [x] 7. Docs: "plan-contract auto-repair" note added to `CLAUDE.md` Known Gotchas.
