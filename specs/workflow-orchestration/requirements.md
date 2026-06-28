# Spec: Multi-Agent Workflow Orchestration

> One feature → one folder. This spec is the source of truth; the code is the
> build artifact. Keep this updated as the design evolves.

---

## 1. Background — the "Why"

Phase 4 (final) of the Claude-Code capability-gap closure (plan approved
2026-06-25). Claude Code's `Workflow` tool fans work out to N concurrent
sub-agents with `pipeline`/`parallel` primitives, adversarial verify passes, and
structured output — the agent had only a single, strictly **sequential** DevAgent
plan loop. This adds a bounded orchestration layer and is the highest
research-value gap for the AIOS framing.

**Key design constraint (discovered during build):** `DevAgent.plan_and_run` is
serialized by an instance `_plan_lock` (interleaved plans would answer each
other's confirmations), and the single RTX-5090 model serializes inference at the
Ollama layer regardless. So fanning out *full plan loops* would neither
parallelize nor stay safe. The realistic, safe primitive is to orchestrate
**fresh-context sub-agent inference calls** — the exact Critic/Tester pattern
(`ModelRouter.infer(domain, context="")`, AGENTS.md #6) — concurrently via the
`AccessibilityScheduler`'s deadlock-free `fan_out` (its own sub-agent semaphore).
**No new model is loaded.**

**Status:** Done (core primitive + durability + **live voice trigger** + tests).
`inference/workflow.py` `WorkflowRunner` with `fan_out` + adversarial `verify`;
`agent_workflows` ledger table; `core/workflow_voice.py` parses the spoken
trigger and `HybridCoordinator._maybe_handle_workflow` drives
decompose→fan_out→synthesize→speak. **Experimental + OFF by default**
(`workflow_orchestration.enabled`). The **`pipeline` (per-item staged) mode is now
specced** (R7 + tasks 5a–5e, 2026-06-28) but **not yet built** — it is purely
additive (the `agent_workflows.mode` column already reserves `'pipeline'`, no
schema/`user_version` change). A live voice trigger for it and an MCP-tool trigger
remain out of scope (the MCP server process has no `ModelRouter`, so a tool there
would drive Ollama outside the main process's VRAM-eviction lifecycle — AGENTS.md
#6; the main-pipeline voice path is the correct home).
**Owner / author session:** Claude Code (Opus 4.8).
**Related:** `../dev-agent-critic/` (the fresh-context reviewer pattern reused
here), `../first-class-search-tools/`, `../browser-ui-testing/` (sibling phases).
Honors AGENTS.md #1 (schema source of truth), #2 (never on the 60 Hz path), #5
(skip-on-flare), #6 (VRAM — no new model).

---

## 2. Glossary

- **SubTask / SubResult:** one fan-out unit (a focused prompt) and its outcome.
- **WorkflowRunner.fan_out:** run N SubTasks concurrently as fresh-context
  inference calls; optionally run an adversarial YES/NO verify pass per result.
- **agent_workflows:** the durability ledger — one row per `fan_out` run
  (`storage/db.py`).

---

## 3. Requirements (EARS acceptance criteria)

### Requirement 1: Fan-out over fresh-context sub-agents
1. `WorkflowRunner.fan_out(subtasks)` SHALL run each SubTask as a
   `ModelRouter.infer(domain, context="")` call (fresh context — AGENTS.md #6) and
   return results in input order. It SHALL load **no** new model.
2. Concurrency SHALL use the `AccessibilityScheduler.fan_out` sub-agent pool when
   a scheduler is wired, else a plain `asyncio.gather`. A failed sub-task SHALL
   isolate to its own `SubResult(ok=False)` and SHALL NOT abort the batch.
3. The runner SHALL execute **no** desktop actions / file writes / shell — it is
   pure inference orchestration (no new bypass of the command-pipeline gates).

### Requirement 2: Adversarial verify (fail-safe)
1. WHEN `verify_criterion` is given, EACH successful sub-result SHALL get a
   second fresh-context reviewer pass (strict YES/NO judge).
2. Any reviewer error, non-`ok`, or non-`YES` first line SHALL count as
   **NOT verified** (fail-safe — never a false-positive confirmation, AGENTS.md #4).

### Requirement 3: Experimental, OFF by default
1. The feature SHALL be OFF unless `workflow_orchestration.enabled` is set in
   `~/.claude/ipad_bridge/config.json` (an explicit `enabled=` arg overrides for
   tests). WHEN off, `fan_out` SHALL return a `disabled` result and issue no
   inference.

### Requirement 4: Skip-on-flare + never on the hot path
1. A wired flare check returning truthy (or itself erroring) SHALL short-circuit
   to a `skipped_flare` no-op (AGENTS.md #5). The runner SHALL NOT be invoked on
   the 60 Hz sensor path (AGENTS.md #2) — it is an offline/heavy primitive.

### Requirement 5: Durable, additive ledger
1. THE `agent_workflows` table SHALL be added via `CREATE TABLE IF NOT EXISTS`
   (additive, backward-compatible) and SHALL NOT require a `PRAGMA user_version`
   bump (no column ALTER). Existing DBs SHALL keep working (#1).
2. EACH run SHALL be journaled best-effort; a DB failure SHALL NOT break
   orchestration.

### Requirement 7: `pipeline` (per-item staged) mode

**User Story:** As Brad, for a goal that benefits from *staged refinement* rather
than independent angles — draft → adversarial critique → revise, or research →
deep-read → synthesize — I want each item carried through an ordered chain of
stages where each stage builds on the previous stage's output, instead of N
one-shot answers I have to reconcile myself.

**How it differs from `fan_out` (and why it's a distinct primitive).** `fan_out`
issues N *independent* fresh-context prompts and (optionally) one verify pass each
— breadth. `pipeline` issues, *per item*, an *ordered* chain of stages where
stage k's prompt is built from the original item **plus stage k−1's output text** —
depth. The single RTX-5090 serializes inference at the Ollama layer regardless, so
pipeline's win is **structural** (staged quality: a later stage can critique/refine
an earlier one), not wall-clock. Crucially, the prior stage's *output text* is
threaded into the next stage's **prompt** — NOT its model context: every stage is
still a fresh-context `infer(domain, context="")` call, so the no-new-VRAM /
no-CoT-carryover invariant (AGENTS.md #6) holds exactly as in `fan_out`.

#### Acceptance Criteria
1. `WorkflowRunner.pipeline(items, stages, *, name, goal, ...)` SHALL run each
   item through ALL `stages` in order, where each `Stage(name, build_prompt)`
   produces stage k's prompt from the original item and the accumulated prior
   stage outputs, and each stage is a fresh-context `ModelRouter.infer(domain,
   context="")` call (AGENTS.md #6 — **no** new model loaded). The final stage's
   text SHALL be the item's `SubResult.text`; per-stage texts MAY be retained for
   journaling/debug but the public result aligns 1:1 with `items` in input order.
2. Items SHALL be processed with **no barrier between stages** (per-item chains
   are independent — item A may be at stage 3 while item B is at stage 1),
   scheduled via the same `AccessibilityScheduler.fan_out` sub-agent pool when
   wired, else `asyncio.gather`. (Within one item the stages are necessarily
   sequential — stage k needs stage k−1's text.)
3. A stage that errors or returns non-`ok` SHALL **fail that item closed**:
   drop it to `SubResult(ok=False, error=…)`, **skip its remaining stages**, and
   SHALL NOT abort the batch or any other item (per-item isolation, mirroring
   `fan_out` R1.2 / AGENTS.md #4). An empty `items` or empty `stages` list SHALL
   return a `completed` no-op.
4. `pipeline` SHALL inherit ALL of `fan_out`'s guardrails unchanged:
   OFF-by-default (`workflow_orchestration.enabled`; explicit `enabled=` overrides
   for tests) → `disabled` no-op (R3); skip-on-flare → `skipped_flare` no-op
   (R4 / AGENTS.md #5); **no** desktop actions / file writes / shell (R1.3); never
   on the 60 Hz path (#2); never raises out of orchestration (degrades to
   `status="error"`).
5. EACH `pipeline` run SHALL be journaled best-effort to `agent_workflows` with
   `mode="pipeline"` — a value the schema **already reserves** (`storage/db.py`
   `mode TEXT NOT NULL DEFAULT 'fan_out' -- fan_out | pipeline`), so this adds
   **NO** table, column, or `PRAGMA user_version` bump (AGENTS.md #1). A DB
   failure SHALL NOT break orchestration (R5.2).
6. (Optional, may land separately) An adversarial `verify_criterion` MAY run a
   final fresh-context YES/NO judge over each item's last-stage text, reusing
   `_verify_one` with the same fail-safe-to-NOT-verified semantics (R2). When
   omitted, `verified_count` SHALL be `None`.

### Requirement 6: Live voice trigger (decompose → fan_out → synthesize → speak)
1. WHEN the feature is ON and a **voice** utterance begins with a trigger phrase
   (`"think hard about …"` / `"research …"` / `"brainstorm …"`; parsed by
   `core/workflow_voice.parse_workflow_request`, anchored + non-empty goal,
   deterministic) followed by a goal, `HybridCoordinator` SHALL decompose the
   goal into N sub-angles (general model), run `WorkflowRunner.fan_out`, then
   synthesize the sub-answers into one concise reply and speak it. It is
   intercepted **before** the dev pre-gate and bypasses the command/dev pipeline
   for a handled request (pure talk — **no desktop actions**).
2. The reply SHALL be spoken with the **mic-feedback suppress guard**
   (`_speak_and_suppress`) so the agent never transcribes its own TTS.
3. Fail-safe (AGENTS.md #4): a non-match, an **active flare** (#5), a missing
   router, or **any** handler error SHALL return `None` → ordinary routing (the
   utterance is never stranded). Decomposition failure SHALL degrade to a single
   sub-agent on the raw goal rather than going silent.
4. The trigger SHALL gate on the **same** `workflow_orchestration.enabled` flag
   as the runner — byte-identical legacy voice path when unset.

---

## 4. Behavior Verification (executable)

- `tests/test_workflow.py` (12) — disabled-status; fan-out runs all / isolates a
  failure / empty no-op; **fresh-context + no model loaded**; verify counts only
  confirmed + **fail-safe on reviewer error**; **skip-on-flare** (+ flare-check
  error → skip); scheduler `fan_out` used when present; **agent_workflows
  migration additive + user_version unchanged**; run journaled to DB.
- `tests/test_workflow_voice.py` (38) — trigger parse (positive/negative,
  longest-phrase, filler-strip, bare-trigger reject); decomposition parse
  (numbering/bullets/blank/dedupe/cap); prompt builders; config + fan-out clamp
  + verify-default-OFF — R6.
- `tests/test_workflow_voice_coordinator.py` (7) — `_maybe_handle_workflow`
  end-to-end with a fake router + real runner: happy path (decompose+fan_out+
  synthesize, 5 infer calls); non-trigger / disabled / missing-router → None;
  **flare → fall-through, no inference**; decompose-failure → single-angle;
  all-subagents-fail → spoken apology — R6.
- `tests/test_workflow_pipeline.py` (new) — R7: stages run **in order** and stage
  k's prompt **contains** stage k−1's output text (assert via a fake router that
  records prompts); the **final** stage text is the item's result; **per-item
  isolation** — a stage error fails ONE item closed (skips its remaining stages)
  while sibling items complete; **fresh-context** assertion (every infer called
  with `context=""`, no new model); disabled → `disabled` no-op (no inference);
  **skip-on-flare** (+ flare-check error → skip); empty items / empty stages →
  `completed` no-op; scheduler `fan_out` used when present; run journaled with
  **`mode="pipeline"`** and `user_version` unchanged (additive — column already
  exists); optional `verify_criterion` reuses `_verify_one` fail-safe.

---

## 5. Tasks

- [x] 1. `storage/db.py` — `agent_workflows` table (CREATE TABLE IF NOT EXISTS,
      additive, no version bump) + `insert_workflow` (best-effort) — R5.
- [x] 2. `inference/workflow.py` — `WorkflowRunner.fan_out` (fresh-context
      sub-agents via scheduler), adversarial `verify`, OFF-by-default flag,
      skip-on-flare, DB journaling — R1–R4.
- [x] 3. `tests/test_workflow.py` (12).
- [x] 4. Live **voice** trigger — `core/workflow_voice.py` (parse + prompts) +
      `HybridCoordinator._maybe_handle_workflow` (decompose→fan_out→synthesize→
      speak) + `main.py` runner construction; `tests/test_workflow_voice.py` (38)
      + `tests/test_workflow_voice_coordinator.py` (7) — R6.
- [~] 5. `pipeline` (per-item staged) mode — R7. The `agent_workflows.mode` column
      already reserves `'pipeline'`, so this is **purely additive** (no schema, no
      `user_version` bump). Primitive shipped 2026-06-28 (tasks 5a–5d); only the
      on-merge doc flip (5e) remains.
  - [x] 5a. `inference/workflow.py` — added `Stage` dataclass
        (`name`, `build_prompt: (item, prior_stage_texts) -> prompt`) and
        `WorkflowRunner.pipeline(items, stages, *, name, goal, verify_criterion,
        verify_domain)`. Reuses `_run_concurrent` (scheduler-or-gather),
        `_should_skip_flare`, the OFF-default/`disabled`/`error`-never-raise
        envelopes, and `_journal(mode="pipeline")`. Per item, `_run_pipeline_one`
        folds stages left-to-right, threading each stage's output **text** into the
        next stage's **prompt** (fresh-context `infer(context="")` — never the
        model context); fail-closed per item on the first stage error — R7.1–R7.4.
  - [x] 5b. Journals each run with `mode="pipeline"` via the existing
        `insert_workflow`; no schema change, `user_version` unchanged — R7.5.
  - [x] 5c. `tests/test_workflow_pipeline.py` (14 tests, all green; full workflow
        suite 71 green incl. the `_maybe_verify` refactor on `fan_out`) — R7.1–R7.6.
  - [x] 5d. Adversarial `verify_criterion` final-stage pass reuses `_verify_one`
        via a shared `_maybe_verify` helper (fan_out + pipeline) — R7.6.
  - [ ] 5e. Update `CLAUDE.md` Known Gotchas (the workflow-orchestration bullet
        currently says "Still deferred: pipeline mode" — flip to "shipped") and
        this spec's §1 Status + §3 R7 once merged.
- [~] 6. **Live trigger for `pipeline`** — *intentionally left to a follow-up.* A
      voice phrase (e.g. `"refine …"` / `"draft and check …"`) routing to
      `pipeline` instead of `fan_out` is a natural extension of
      `_maybe_handle_workflow`, but the **primitive ships first** (tasks 5a–5c) so
      the staged runner is test-covered before any live surface drives it. The
      MCP-tool trigger stays **ruled out**: the MCP server process has no
      `ModelRouter` and would drive Ollama outside the main process's VRAM-eviction
      lifecycle (AGENTS.md #6) — the main-pipeline voice path is the correct home.
