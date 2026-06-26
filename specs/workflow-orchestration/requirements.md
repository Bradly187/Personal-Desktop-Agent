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
(`workflow_orchestration.enabled`). **Still deferred:** a `pipeline` (per-item
staged) mode — the `agent_workflows.mode` column already reserves `'pipeline'` —
and an MCP-tool trigger (the MCP server process has no `ModelRouter`, so a tool
there would drive Ollama outside the main process's VRAM-eviction lifecycle —
AGENTS.md #6; the voice trigger in the main pipeline is the correct home).
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
- [ ] 5. (Deferred) `pipeline` (per-item staged) mode — `agent_workflows.mode`
      already reserves `'pipeline'`. MCP-tool trigger ruled out: the MCP server
      process has no `ModelRouter` (would drive Ollama outside the VRAM lifecycle,
      AGENTS.md #6).
