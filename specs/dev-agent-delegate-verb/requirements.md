# Spec: Planner-Driven DELEGATE Verb — bounded read-only sub-agent (Gap D)

> One feature → one folder. This spec is the source of truth; the code is the
> build artifact. Design and Tasks are kept inline (§4–§6) until they outgrow
> the file. Keep this updated as the design evolves.

---

## 1. Background — the "Why"

Gap analysis (2026-06-26) against `rasbt/mini-coding-agent` mapped component **6
(Delegation & bounded subagents)**. The reference's `tool_delegate()` is a
**planner-callable tool**: mid-plan, the agent emits a `delegate` step that spins
off a **read-only, depth-capped child agent** to investigate a scoped sub-question
(`MiniAgent(read_only=True, approval_policy="never", depth=self.depth+1)`), and
the child's finding flows **back into the parent's trajectory**.

PDA shipped a *different* delegation shape in **PR #137** (merged 2026-06-26,
`specs/workflow-orchestration/`): `inference/workflow.WorkflowRunner` +
`core/workflow_voice.py`. That is a **breadth** primitive — voice-triggered
("think hard about X"), it decomposes a goal into N angles, fans them out as
concurrent **fresh-context inference calls** (`ModelRouter.infer(context="")`,
*no tools, no file reads, no shell*), adversarially verifies, and **speaks** one
synthesized answer. It never integrates with the plan loop and the sub-agents
cannot investigate anything.

So the gap #137 left open is precisely the reference's shape: a **depth**
primitive the *planner* reaches for when it needs to read/grep its way to an
answer before continuing — and have that answer land in the trajectory. This spec
adds a `DELEGATE` plan verb that **reuses #137's orchestration substrate**
(scheduler sub-agent pool, flare-skip guard, `agent_workflows` journaling) rather
than forking a second one — the two become siblings over one substrate:
*WorkflowRunner = breadth (voice, tool-less, N angles), DELEGATE = depth (planner,
read-only tools, one scoped investigation that returns into the plan)*.

**Status:** In Progress — `DELEGATE` verb registered (`_PLAN_ACTIONS`,
`_STEP_PATTERN`, `model_router._PLAN_VERBS` enum, `_RETRYABLE_VERBS`) +
`DevAgent._delegate_investigate`/`_delegate_loop`/`_journal_delegate` + depth
threading + `_execute_step` branch + `_DELEGATE_PROMPT_INSTRUCTIONS` (taught only
when on, at depth 0) behind `DA_DELEGATE`; 12 unit tests green. Journals to the
existing `agent_workflows` ledger (`mode="delegate"`). Eval baseline (task 6)
pending. **R1.3 refinement:** DELEGATE is in `_RETRYABLE_VERBS` only — deliberately
NOT in `_PARALLEL_VERBS` (which would also pull it into `_FANOUT_SAFE_VERBS` via the
union and let the parent fan out delegations concurrently). It runs SEQUENTIALLY,
honoring the spec's primary intent; the dedup/observation-membership note in R1.3
is superseded by this (DELEGATE is a dedup no-op, which is harmless).
**Owner / author session:** Claude Code (Opus 4.8)
**Related:** `../workflow-orchestration/` (PR #137 — the substrate this builds on,
NOT replaces), `../accessibility-agent/` (DevAgent), `../trajectory-read-dedup/`
(DELEGATE is a read-only verb → its result is dedup-eligible),
`../repo-context-ingestion/`. Honors AGENTS.md #4 (child is structurally
read-only — cannot do anything destructive, so no approval gate to bypass), #6
(no new model — reuses the resident model fresh-context, like Critic/Tester), #7
(child reads only inside scope), #2 (off the 60 Hz path).

---

## 2. Glossary

- **DELEGATE**: the new read-only plan verb this spec introduces. Args: a scoped
  investigation question. Emitted by the planner mid-plan; its synthesized finding
  becomes the step `result` fed to `_replan`/`_reflect`.
- **Investigation child**: the bounded sub-agent DELEGATE spawns. Runs a small
  read-only plan→execute loop (allowlist = `_PARALLEL_VERBS` only), depth+1, own
  small step budget. Returns one concise finding string.
- **`_plan_lock`**: the per-`DevAgent` lock serializing `plan_and_run`
  (`inference/dev_agent.py`). Interleaved plans would answer each other's
  confirmations — so the child MUST NOT re-enter `plan_and_run` (it would
  deadlock on a re-entrant acquire). This is the same constraint that forced #137
  to use fresh-context inference instead of nested plan loops.
- **WorkflowRunner substrate**: the reusable pieces of `inference/workflow.py` /
  `core/scheduler.py` — `AccessibilityScheduler.fan_out` (bounded sub-agent
  semaphore), the flare-skip guard, and `AgentDB.insert_workflow(mode=…)`
  journaling to `agent_workflows`.
- **Delegation depth**: a per-run counter. Top-level plan = depth 0; a DELEGATE
  child = depth 1. DELEGATE is refused at depth ≥ `MAX_DELEGATE_DEPTH` (default 1)
  → no recursion, no fan-bomb.

---

## 3. Requirements (EARS acceptance criteria)

### Requirement 1: A read-only DELEGATE plan verb

**User Story:** As Brad, I want the planner to be able to spin off a scoped
read-only investigation and use its finding, instead of cramming every grep/read
into the main plan or guessing.

#### Acceptance Criteria
1. THE DevAgent verb vocabulary SHALL include `DELEGATE <question>` (registered in
   the verb regex and the planner prompt), and `_execute_step` SHALL dispatch it.
2. WHEN a DELEGATE step runs, THE investigation child SHALL execute a bounded
   plan→execute loop restricted to `_PARALLEL_VERBS` (READ_FILE, GREP, FETCH_URL,
   READ_SCREEN, GIT_STATUS, GIT_DIFF, SEARCH_PERSONAL) and SHALL synthesize a
   single concise finding (≤ `finding_chars`, default 1200) returned as the step
   `result`.
3. THE DELEGATE step's finding SHALL flow into the trajectory like any read-only
   observation (visible to `_replan`/`_reflect`); DELEGATE SHALL be a read-only
   verb (in `_PARALLEL_VERBS` membership for dedup/observation semantics) but
   SHALL NOT be added to the parent's concurrent fan-out set
   (`_FANOUT_SAFE_VERBS`) — delegations run sequentially (nested fan-out over one
   serialized GPU buys nothing and risks scheduler contention).

### Requirement 2: Structurally read-only (no destructive path exists)

**User Story:** As Brad, a delegated child must be unable to change anything — not
"gated", but incapable.

#### Acceptance Criteria
1. THE investigation child SHALL reject ANY verb outside `_PARALLEL_VERBS`
   (deny-by-default, AGENTS.md #4) — a child plan step naming WRITE_FILE /
   EDIT_FILE / RUN_TERMINAL / GIT_COMMIT / any approval-gated or desktop-action
   verb SHALL be dropped with a logged refusal, NEVER executed.
2. BECAUSE the child can reach no destructive or approval-gated verb, the child
   SHALL run WITHOUT prompting a voice-approval gate (there is nothing to approve)
   — the read-only guarantee is enforced by the allowlist, not by the prompt.
3. THE child's reads SHALL respect the same path scope as the parent
   (`_path_in_scope` / writable-root resolution for the read tools, AGENTS.md #7).

### Requirement 3: Bounded — depth, steps, time, and the plan-lock constraint

#### Acceptance Criteria
1. THE investigation child SHALL run at delegation depth = parent depth + 1; IF a
   DELEGATE step is emitted at depth ≥ `MAX_DELEGATE_DEPTH` (default 1), THEN it
   SHALL be refused with the observation `DELEGATE refused: max delegation depth`
   and SHALL NOT spawn a child (no recursion / fan-bomb).
2. THE child SHALL NOT re-acquire `_plan_lock` (it must not call the lock-holding
   `plan_and_run`) — it runs via a dedicated read-only investigation path under
   the scheduler's sub-agent semaphore (`AccessibilityScheduler.fan_out` / the
   existing sub-agent permit), so it cannot deadlock the parent plan.
3. THE child SHALL be bounded by its own small step budget (`max_steps`, default
   4) and step timeout; on exhaustion it SHALL synthesize from what it gathered
   and return, never hang.

### Requirement 4: Reuse #137's substrate; safe degradation; flagged

#### Acceptance Criteria
1. THE DELEGATE path SHALL journal each delegation to the EXISTING
   `agent_workflows` ledger via `insert_workflow(mode="delegate", …)` — NO new
   table, NO `PRAGMA user_version` bump (AGENTS.md #1). It SHALL reuse the
   scheduler sub-agent pool and the flare-skip guard already used by
   `WorkflowRunner`, not introduce a parallel orchestration layer.
2. WHILE a flare is active (AGENTS.md #5), a DELEGATE step SHALL short-circuit to a
   safe observation (`DELEGATE skipped: flare`) — investigation is non-essential
   heavy work — and the parent plan SHALL continue.
3. IF the child errors or the substrate is unavailable, THEN DELEGATE SHALL return
   a safe observation describing the failure (never raise out of `_execute_step`);
   the parent plan SHALL treat it as a failed read-only step (recovery signal, not
   a crash).
4. THE feature SHALL be controlled by a flag `DA_DELEGATE`, default **off** until
   the eval baseline (§5) is recorded; WHILE off, the verb SHALL be absent from the
   planner vocabulary and plan behavior SHALL be byte-identical to today.

---

## 4. Technical Design

- **Entry point / pipeline boundary:** new `if action == "DELEGATE":` branch in
  `inference/dev_agent.py::_execute_step` (≈L2368, alongside the other read-only
  verbs). New private `DevAgent._delegate_investigate(question, depth)` coroutine.
  No coordinator/gate change.
- **Why not reuse `WorkflowRunner` directly:** `WorkflowRunner._infer_one` is a
  *tool-less* fresh-context inference — it cannot read or grep, which is the whole
  point of an investigation. So DELEGATE reuses the *substrate* (scheduler pool,
  flare guard, journaling) but runs a **read-only plan→execute mini-loop**. The
  shared substrate keeps observability (`agent_workflows`) and concurrency bounds
  unified; the loop is what's new.
- **Why not reuse `plan_and_run`:** it holds `_plan_lock` (R3.2). The child runs a
  dedicated lighter path — plan via `_router.infer(domain="plan", context=<scoped
  ctx>)`, parse with the existing plan parser, then for each step run
  `_execute_step` ONLY IF `step.action.upper() in _PARALLEL_VERBS` (else drop,
  R2.1), accumulate results, synthesize a finding via one more `_router.infer`.
  All under the scheduler sub-agent semaphore, never the dev permit / plan lock.
- **Scoped context inheritance (the reference's "inherit enough to help"):** the
  child gets the delegate question + the parent goal + RAG/workspace context — NOT
  the full parent trajectory. Bounded payload by construction.
- **New `AgentStep`/run field:** a `depth: int = 0` on the run context threaded to
  the child (in-memory only; the persisted `agent_steps` schema is unchanged).
- **Verb sets:** add `DELEGATE` to the verb regex (≈L145), `_RETRYABLE_VERBS`
  (read-only, safe to retry), and `_PARALLEL_VERBS` membership for
  observation/dedup semantics; **exclude** from `_FANOUT_SAFE_VERBS` (R1.3).
- **Models / VRAM:** none added — resident model, fresh-context, like Critic/Tester
  (AGENTS.md #6 unaffected).
- **Persistence:** none beyond reusing `agent_workflows` (R4.1). No migration, no
  `user_version` bump (AGENTS.md #1).
- **Cross-platform:** none (AGENTS.md #3 N/A).

### Configuration (flat YAML)

```yaml
delegate:
  enabled: false            # env DA_DELEGATE; default off until eval baseline locks
  max_delegate_depth: 1     # a delegated child cannot itself delegate (R3.1)
  max_steps: 4              # child read-only step budget (R3.3)
  finding_chars: 1200       # cap on the synthesized finding returned to the parent
```

### Relationship to PR #137 (complement, not collide)

| | WorkflowRunner (#137) | DELEGATE (this spec) |
|---|---|---|
| Trigger | Voice ("think hard about X") | Planner emits a plan step |
| Sub-agent | Fresh-context inference, **no tools** | Read-only plan→execute (READ_FILE/GREP/…) |
| Shape | Breadth — N angles, verify, synthesize | Depth — one scoped investigation |
| Output | Spoken synthesized answer | Finding returned **into the trajectory** |
| Substrate | scheduler pool, flare guard, `agent_workflows` | **same substrate, reused** |
| Recursion | N/A (flat) | depth-capped (`MAX_DELEGATE_DEPTH`) |

---

## 5. Behavior Verification (executable, not prose)

- **Unit tests:** `tests/test_delegate_verb.py`, one assertion per criterion:
  - `test_r1_2_child_restricted_to_readonly_verbs`
  - `test_r2_1_destructive_child_step_dropped_not_executed`
  - `test_r2_2_no_approval_gate_invoked`
  - `test_r3_1_refused_at_max_depth` (no child spawned; safe observation returned)
  - `test_r3_2_does_not_reacquire_plan_lock` (delegate while a parent plan "holds"
    the lock — assert no deadlock / no re-entrant acquire)
  - `test_r3_3_step_budget_bounded`
  - `test_r4_1_journals_to_agent_workflows_mode_delegate`
  - `test_r4_2_flare_skips_safely`
  - `test_r4_3_child_error_is_safe_observation`
  - `test_r4_4_disabled_verb_absent` (byte-identical planner vocab when off)
- **Eval suite:** add ≥2 cases to `evals/suites/dev_trajectory.jsonl` (or a new
  `dev_delegate` suite) where the correct plan investigates before acting (e.g.
  "find which module defines X, then …") and score that the DELEGATE finding is
  used. Track `safe_acc` — a delegated child SHALL never cause a write
  (R2). Lock the baseline per `running-the-eval-harness`.

Each acceptance criterion in §3 maps to ≥1 test or eval case above.

---

## 6. Tasks

- [x] 1. Add `DELEGATE` to verb regex / `_PLAN_ACTIONS` / `_PLAN_VERBS` enum /
      `_RETRYABLE_VERBS` (NOT `_PARALLEL_VERBS`/`_FANOUT_SAFE_VERBS` — see R1.3
      refinement); planner-prompt guidance gated to depth-0-when-on — R1.1, R1.3, R4.4.
- [x] 2. `DevAgent._delegate_investigate`/`_delegate_loop` — read-only plan→execute
      mini-loop, allowlist-enforced, under the scheduler sub-agent semaphore (when
      wired), NOT `_plan_lock` — R1.2, R2.1, R2.3, R3.2, R3.3.
- [x] 3. Depth threading (`_delegate_depth`) + refuse at `_max_delegate_depth` — R3.1.
- [x] 4. `_execute_step` DELEGATE branch: flare-skip, journal to `agent_workflows`
      (`mode="delegate"`), safe-observation on error — R4.1, R4.2, R4.3.
- [x] 5. `tests/test_delegate_verb.py` (12 tests) — R1–R4.
- [~] 6. Eval cases ADDED — TWO suites: (a) prompt-only `evals/suites/dev_delegate.jsonl`
      (3 cases, `context` carries the `_DELEGATE_PROMPT_INSTRUCTIONS` teaching; scored
      `DA_DELEGATE=1 … --mode trajectory`); (b) END-TO-END `evals/suites/dev_delegate_exec.jsonl`
      (3 read-only cases run through the LIVE `plan_and_run` via `--mode execution`, so a real
      DELEGATE step runs `_delegate_investigate` against the repo — `DA_DELEGATE=1 … --mode
      execution`). `evals/trajectory._PLAN_ACTIONS` mirror + extractor recognize DELEGATE;
      `_run_execution` logs the flag. Both load-verified. Baseline lock pending a model run — §5.
- [ ] 7. **DECISION (Brad):** flip `DA_DELEGATE` default on if the gate holds.
- [ ] 8. Update `CLAUDE.md` Known Gotchas (new verb + flag; note it complements,
      not replaces, #137 WorkflowRunner) + the verb table in the Action Vocabulary
      section.
