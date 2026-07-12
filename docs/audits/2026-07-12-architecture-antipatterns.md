# Architecture Antipattern Audit — 2026-07-12

**Scope:** whole-system architecture review (core/, inference/, storage/, mcp_server/, main.py, evals/, desktop_app/), tree audited **as on disk** including the uncommitted workflow-removal working-tree changes. Follows the OOP anti-pattern audit merged as PR #177; does not rehash findings that PR fixed. Cross-checked against `docs/decisions.md` (D001–D029) — accepted trade-offs are not re-flagged.

**Method:** three parallel review agents (core pipeline / inference layer / cross-cutting) + full test run + lint.

---

## Current system state

| Signal | Status |
|---|---|
| Branch | `claude/architecture-antipatterns-review-acc726` — **zero commits beyond master** (c11e2e1) |
| Working tree | ~1,744 lines of **uncommitted** deletions/edits carried over from the 07-10 OOP-audit session (not part of PR #177) |
| Tests | **2,730 passed, 3 skipped, 0 failed** (7m15s, local, Py3.14) |
| Lint | 3 × F401 in `core/workflow_voice.py:39-44` — **fails the D029 CI ruff gate** as-is |
| Dangling imports of deleted modules | none (verified: no live references to `inference.workflow` / `subagent_delegator`) |

### What the uncommitted diff is

**Provenance (confirmed 2026-07-12):** two Antigravity (Gemini) tasks executed 2026-07-10 15:24–15:52, immediately after the Claude PR #177 session went idle. Plans preserved at `C:\Users\bradt\.gemini\antigravity\brain\45e8cbe7-…\implementation_plan.md` ("Remove Subagent Feature Creep" — remove premature multi-agent orchestration per "Start Monolithic" / "Delegate strictly on demand" principles) and `…\8bc2144b-…\implementation_plan.md` ("4-Boundary Persona Architecture" prompt refactor — Builder vs. Scrutinizer adversarial setup). Both tasks skipped their own pytest verification (task 1's checkbox left unchecked; task 2's task.md: "aborted due to time constraints, relied on syntax check"). The stray `uv.lock` is from the same tool. Note: the removal plan promised the voice trigger would become "a single inference call against the *general* domain"; the implementation instead wired the full action-capable `dev_agent.plan_and_run()` — the direct cause of finding 1.

A removal of the multi-agent workflow subsystem, **plus an unrelated prompt overhaul bundled in**:

- Deleted: `inference/workflow.py` (WorkflowRunner fan-out), `inference/executors/subagent_delegator.py` (DA_DELEGATE), 4 test files (~765 test lines).
- Flags removed from `core/flags.py`: `DA_DELEGATE` (+3 tuning knobs), `DA_WORKFLOW_VERIFY_CLOUD` (D026).
- `core/workflow_handler.py` rewired: voice "think hard / research / brainstorm" now runs a single `dev_agent.plan_and_run()` instead of decompose→fan-out→synthesize.
- Bundled prompt rewrites: command system prompt (`backends/base.py`), all 5 specialist prompts (`model_router.py`), both critic prompts (`critic.py` — pass criterion tightened to "flawlessly… absolutely no issues", persona now "pessimistic Scrutinizer").

The removal is ~90% complete and statically clean, but **not landable as-is** (findings 1, 2, 5, 6 below).

---

## Findings (deduplicated, most severe first)

### A. Introduced by the uncommitted removal

**1. Voice workflow trigger lost its enable gate AND its "pure inference" contract — HIGH.**
`core/event_dispatcher.py:46-49` removed the `workflow_runner.enabled` guard (feature was default-OFF); `core/workflow_handler.py:64-97` now routes the goal into the full action-capable `dev_agent.plan_and_run()` (WRITE_FILE / RUN_TERMINAL / GIT_* verbs, approval-gated but present). It also enters DevAgent **ahead of** the dev pre-gate path, skipping that path's Gate-0 / cloud-scrub / `_record_dev_command` / personal-query guards (`event_dispatcher.py:56-172`). Any utterance starting with a trigger word ("research shows that…") now unconditionally launches a dev-agent plan. CLAUDE.md still promises "Pure inference — no desktop/file/shell actions"; `workflow_orchestration.enabled` is now dead config read by nothing. A default-OFF feature became always-on with expanded privileges and no decision-log entry. If downgrade-to-monolithic is intentional, the gate should move, not vanish.

**2. Dangling DELEGATE dispatch branch — live crash path — HIGH (latent).**
`inference/step_executor.py:115-119` still dispatches `action == "DELEGATE"` to `agent._delegate_investigate(...)` / `agent._delegate_depth` — both deleted. Fresh plans can't produce DELEGATE (removed from the grammar), but **crash-resume (`DA_RESUME_MEMORY` ON) replays persisted `agent_steps`** — a pre-removal plan containing DELEGATE resumes into `AttributeError` mid-plan with a misleading `ERROR:` diagnostic. Delete the branch (and the empty "Gap D" banner crater at `dev_agent.py:678-685`).

**3. Bundled prompt rewrites invalidate locked eval baselines — MEDIUM-HIGH.**
`evals/baselines/` untouched while critic/command/specialist prompts were all rewritten. dev_critic (locked 1.0), dev_trajectory (0.6364), command_verbs, plan_contract were locked against the old prompts (D018). The critic criterion change alone plausibly shifts PASS/REVISE rates on the live write path. Baselines need re-running before merge — and mixing subsystem removal with a prompt overhaul in one changeset makes either regression unbisectable. **Recommend splitting into two commits/PRs.**

**4. Eval verb-grammar mirror drifted — the exact D018 defect class — MEDIUM-HIGH.**
`evals/trajectory.py:35-43` ("Mirror of dev_agent._PLAN_ACTIONS, keep in sync") still contains `DELEGATE` and is **missing `EDIT_FILE`**, vs production `inference/plan_parser.py:10-24`. Trajectory evals can't credit EDIT_FILE steps and would credit a verb production can't parse. Fix: import production's grammar, never copy.

**5. Docs / config / artifact drift left by the removal — MEDIUM.**
- `CLAUDE.md`: flag table still lists `DA_DELEGATE` (ON) and `DA_WORKFLOW_VERIFY_CLOUD`; workflow gotcha ("OFF by default… pure inference… pipeline mode shipped") now false on all counts.
- Stale docstrings narrating deleted code: `core/workflow_voice.py:1-35`, `core/workflow_handler.py:5-17`, `core/hybrid_coordinator.py:357-369`, `docs/file-map.md:20`, `storage/db.py:148-152`, `storage/schema/agent.py:87-91`.
- Orphans: `storage/repositories/workflows_repo.py` (`insert_workflow` has zero callers), `evals/suites/dev_delegate*.jsonl` + `evals/baselines/dev_delegate.json` (instructions reference removed flag), specs (`workflow-orchestration/`, `dev-agent-delegate-verb/`, `gap-3-verify-cloud.md`) need Status flipped per AGENTS.md Rule 13.
- 3 F401 unused imports in `workflow_voice.py` fail CI ruff.
- D026 decision entry goes stale if this ships.

### B. Pre-existing architecture debt (present on master)

**6. The PR #177 god-object split was mechanical, not architectural — HIGH.**
All state stayed on `DevAgent`; extracted modules are method bags over its privates (`executors/plan_executor.py`: **110 `agent._*` touches**; `step_executor.py` reaches two levels: `agent._saga_manager._snapshot_for_write`, `agent._coordinator._executor.execute`). Glue is three `__getattr__` reflection bridges (`dev_agent.py:1008-1032`) that forward missing attributes to SagaManager, ContextBuilder, and *any module-level callable* in plan_executor/evaluation_manager — the documented primary entry point `plan_and_run` **is not defined on the class at all**; it exists only via reflection. Renames break callers at runtime with zero static signal (ruff/mypy/IDE all blind). Also: `__import__`-based method grafts in the class body (`dev_agent.py:949-951`) and a duplicated `_SAGA_SNAPSHOT_MAX_BYTES` where the `DevAgent` copy is a decoy knob (overriding it changes nothing — the live one is `saga_manager.py:41`). Highest-leverage fix order: delete DELEGATE branch → replace `__getattr__` bridges with explicit methods/imports → kill the grafts + duplicate constant → retire the `local_inference.py` / `dev_agent.py:74-88` re-export facades (`_DEPS_PATTERN` is defined in both dev_agent (dead) and plan_parser (live)).

**7. Split-brain storage layer: two complete `AgentDB` implementations + two DDL copies — HIGH.**
`storage/db.py` (live, 1,299 lines: inline schema, version 9, migrations, AgentDB + AnalyticsDB) vs `storage/agent_db.py` + `storage/schema/agent.py` (complete second implementation — **nothing imports it**). Byte-identical today (diffed), but hand-synced: one edit applied to only one copy is silent, permanent schema drift (`user_version` gates make it stick per DB file). Leftover scaffolding from the PR #164 repair. Either make `db.py` consume `storage/schema/agent.py`, or delete the orphans.

**8. `EventDispatcher(self)` violates the codebase's own accessor-DI rule; `hybrid_coordinator` is a circular-import hub — MEDIUM-HIGH.**
The R3/D022 decomposition pattern (named accessor callables, honored by `VoiceSystemControl`/`WorkflowHandler`) is broken by `EventDispatcher` holding the whole coordinator and touching ~25 privates. Cycles: `hybrid_coordinator` imports `EventDispatcher` at module level while `event_dispatcher.py:207` runtime-imports back; `gate_evaluator.py:106` runtime-imports private constants from `hybrid_coordinator` even though `routing_constants.py` exists as the single source; private ContextVars are set across module boundaries (`gate_evaluator._EFFECTIVE_CFG`, `inference_runner._PENDING_INFERENCE_IDS`). Misnamed: it is the command router, not an event dispatcher.

**9. Verb vocabulary and prompt library are shotgun-surgery hotspots — MEDIUM.**
The planner verb list lives in ≥3 data copies (`model_router.py:259-266` schema enum, `plan_parser.py` `_PLAN_ACTIONS` + `_STEP_PATTERN` regex) plus prose + the step_executor if-chain — the DELEGATE removal touched 6 files and *still* missed step_executor (finding 2). The 5 domain prompts are duplicated between `model_router.py` and `cloud_dev_agent.py:76-166` and have already drifted in persona/constraints. `model_router.py` remains a 1,459-line multi-responsibility module (prompts, plan schema, edit-format config loader, VRAM probe, VLLMSpecialistPool, router).

**10. Latent wiring bugs and convention violations — LOW-MEDIUM.**
- `hybrid_coordinator._target_cache` never initialized in `__init__` (exists only after `set_target_cache()`); `action_executor.py:403` calls it before its flag check → `AttributeError` on any coordinator not wired by full `main.py`.
- `_VOICE_CORRECTIONS` module-level mutable dict (`hybrid_coordinator.py:156-178`) violates the "no global state" convention.
- `ipad_bridge.py:532-572` pokes `coordinator._twin`/`._calibrator` behind `hasattr` guards — a rename makes **pain-day override silently no-op** (accessibility-critical path failing invisibly).
- Raw SQL through private `_conn` from `main.py:601-608` and `saga_manager.py:564`, bypassing repositories.
- CWD-relative `agent.db` default; a stray `storage/agent.db` (516 KB, Jun 18) proves the failure mode already happened.
- `desktop_app/main.js:100-119` is a second, unlocked writer to `approval_config.json` (torn-read risk vs `approval_hook.py` per-call reads).
- One real core↔inference module-level import cycle, resolvable by extracting `Command`/`Priority` from `core/command_executor.py` into a leaf types module.

---

## What is healthy (verified, not assumed)

- `main.py::_run_pipeline` is a genuine explicit composition root; modules do not self-wire (few documented singleton exceptions).
- Command DTO discipline holds at every checked boundary; typed DTOs (`AgentStep`, `RouterResult`, `CriticVerdict`) cross the main seams.
- Async hygiene: every checked blocking site is `asyncio.to_thread`'d; the D001 sync-MOUSEDOWN exception is intact; no import-time side effects (AST-verified).
- `core/cloud_backend.py` is a clean single cloud seam (D002) shared by both cloud routers; `core/flags.py` registry disciplined (D021).
- Storage: single writer via `storage/db.py` repos; monitoring/chat read via documented `mode=ro` connections; mcp_server has zero DB access.
- Full test suite green on the dirty tree.

## Recommended sequence before this branch merges

1. **Decide the workflow-removal question explicitly** (new D-number): keep the fan-out subsystem, or ship the removal. If shipping: restore an enable gate (or consciously re-decide always-on), delete the DELEGATE branch, fix the eval verb mirror, purge orphans/docs/flags-table, clear the 3 F401s, retire specs, update D026.
2. **Split the prompt overhaul into its own commit/PR** and re-run + re-lock eval baselines against it.
3. Then schedule the pre-existing debt: storage split-brain (7), `__getattr__` bridges (6), EventDispatcher DI (8), verb-list single-sourcing (9).
