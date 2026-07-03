# Handoff: Error-Propagation Gap Closure (2026-07-03)

> **Status: OPEN — 4 ranked gaps, none started.** Source: 2026-07-03 code audit of the
> DevAgent + WorkflowRunner pipelines against an external analysis of error propagation
> in agentic SDLC ("mistake at step N becomes trusted input to step N+1"). Three
> parallel read-only audits verified every claim against code with file:line evidence.
> This document is self-contained — no access to the source conversation is needed.

## Audit verdict (context for whoever picks this up)

The repo already implements most of the standard mitigations — do **not** rebuild these:

| Mitigation | Status in repo | Evidence |
|---|---|---|
| Diff review at stage boundaries | **SHIPPED, ON** — Critic reviews unified diff pre-disk-commit, fresh empty context (no generator CoT), REVISE/BLOCK feeds replan | `inference/critic.py:166-183`, wired `inference/dev_agent.py:2996` |
| External-ground-truth testing | **SHIPPED, ON** — Tester runs real pytest in ephemeral sandbox; failure = safe-observation (never rollback, D008) | `inference/tester.py:117-155` |
| Bounded context per stage | **SHIPPED** — every workflow worker/stage/judge/critic/tester call is `context=""`; workers never see orchestrator framing; trajectory reduction+dedup bound replan input, failures kept full-fidelity | `inference/workflow.py:293,317,338`, `inference/trajectory.py` |
| Human gates at high-leverage points | **SHIPPED** — two-gate approval (upfront plan + per-op destructive), fail-safe DENY; critic low-confidence PASS (< `DA_CRITIC_CONFIDENCE_FLOOR` 0.6) escalates to voice gate | `inference/dev_agent.py:2510-2671, 2879-2880` |
| Rollback machinery | **SHIPPED** — per-step saga snapshots + VoiceRewindHandler | `inference/dev_agent.py:1673-1710, 2047-2126` |

Three failure modes from the analysis are **confirmed live** in the code:

1. **Orchestrator → worker propagation.** `WorkflowRunner` workers receive only their
   decomposed sub-prompt (`SubTask.prompt`) — a semantically wrong `_decompose_goal`
   flows through synthesis unchecked (`core/workflow_handler.py:97-146`,
   `inference/workflow.py:130-189`). Exceptions degrade gracefully; wrong *content* does not.
2. **Reviewer blind spots (correlated errors).** Critic, verify judge, adjudicator, and
   Tester all run on the same resident local models as generation (plan/code domains).
   Fresh context ≠ independent weights. The workflow verify judge is a single YES/NO
   check, not voting (`inference/workflow.py:260-277`). Partial accidental mitigation:
   `DA_CLOUD_PLAN` (D015) routes planning to Bedrock Sonnet — a genuinely different model.
3. **Checkpointing preserves state, not correctness.** Saga rollback fires only on
   replan-budget exhaustion / max-steps / user-cancel — never on a correctness signal
   (deliberate: D008 "Tester failure never rolls back a good write"). `DA_RESUME_MEMORY`
   seeds resumed plans from `agent_steps` with **no re-validation**
   (`inference/working_memory.py:60-103`, `inference/dev_agent.py:2236-2253`). Trajectory
   observations are trusted once written — no staleness or contradiction detection. Only
   *inbound* content is taint-screened (`MCPTrustClassifier`, `inference/dev_agent.py:76-104`);
   the agent's own prior observations are never checked.

The one place the model grades its own work with no independent check: **step-failure
recovery**. `_replan` (`inference/dev_agent.py:1597-1642`) re-prompts the *same* plan
model with its own trajectory; there is no Critic-equivalent for recovery plans.
(`DA_PLAN_REPAIR` is also same-model but bounded to 1 retry and parse-errors only — fine as-is.)

---

## GAP-1: Assumption surfacing in the planner prompt

**Rank 1 — cheapest, highest leverage per line changed.**

- **Problem:** Neither the planner prompt nor executor prompts ask the model to state
  assumptions or flag uncertainty. The only confidence signal in the whole pipeline is
  the critic confidence floor. A wrong premise ("I assume the schema has column X")
  enters the trajectory silently and conditions every later step.
- **Where:** planner prompt assembly in `inference/dev_agent.py` (extra_ctx path — format
  teaching, RAG, git context, repo facts are injected there; assumptions instruction goes
  alongside). Plan JSON schema: `inference/model_router.py:250-268` (`_PLAN_JSON_SCHEMA`).
- **Approach:** add an optional `assumptions` array (strings) to `_PLAN_JSON_SCHEMA` and
  instruct the planner to list assumptions it is making about repo/system state. Surface
  them: (a) log at INFO, (b) include in the upfront plan-approval TTS summary when
  `DA_PLAN_PREVIEW` is on, (c) persist alongside the plan (e.g., in the run record) so
  replan/resume can see what was assumed.
- **Flag:** `DA_PLAN_ASSUMPTIONS`, default OFF (byte-identical prompt when OFF).
- **Effort:** small — prompt text + schema field + plumbing; no new model calls, no VRAM.
- **Risks:** schema change must keep `assumptions` optional so old-model outputs still
  parse; keep the TTS summary short (RA user — voice-first UX, don't read 10 assumptions aloud).
- **Verify:** unit test that OFF ⇒ prompt unchanged; ON ⇒ schema accepts/omits the field;
  eval sweep confirms no plan-fidelity regression (baselines in eval harness — see
  `docs/audits/2026-07-02-coding-agent-gap-analysis.md` conventions).

## GAP-2: Independent review of recovery plans (replan critic)

**Rank 2 — closes the only unreviewed self-grading loop.**

- **Problem:** `_replan` output executes without any independent check. First plans get
  the two-gate human approval; recovery plans (generated *after* something already went
  wrong — precisely when context is most likely poisoned) do not get a Critic pass.
- **Where:** `inference/dev_agent.py:1597-1642` (`_replan`); Critic infra at
  `inference/critic.py` is reusable (fresh-context, verdict parsing already exists).
- **Approach:** after a replan parses, run a bounded critic-style check over the new plan
  (goal + executed-step summary + proposed steps → PASS/REVISE), using either the plan
  domain with a fresh context (cheap) or the `DA_CLOUD_PLAN` Bedrock model when enabled
  (genuinely different weights — pairs with GAP-3). REVISE consumes the existing replan
  budget — do **not** add a new unbounded loop.
- **Flag:** `DA_REPLAN_CRITIC`, default OFF.
- **Effort:** medium — one new inference call per replan, prompt + verdict parse reuse.
- **Risks:** latency on the recovery path (each replan gains one inference); must respect
  `_critic_max_revisions`-style bounding; do not let a REVISE verdict trigger saga
  rollback (preserve D008/D009 semantics — replan only).
- **Verify:** test that OFF ⇒ replan path byte-identical; ON ⇒ REVISE verdict decrements
  replan budget and re-plans; budget exhaustion still routes to `_halt_and_compensate`.

## GAP-3: Cross-model verify judge for workflow fan-out

**Rank 3 — addresses correlated reviewer blind spots with a seam that already exists.**

- **Problem:** `_maybe_verify` judges worker outputs with the same resident model that
  produced them (`inference/workflow.py:260-277, 329-344`). Correlated blind spots pass
  verification. Own prior analysis flagged this ("fan-out + single-judge, not voting" —
  MAAD gap analysis).
- **Where:** `inference/workflow.py` verify path; cloud seam: `core/cloud_backend.py` +
  `CloudPlanRouter` (`specs/cloud-plan-routing/`, D015, Bedrock-only — no direct Anthropic).
- **Approach:** route the verify judge through the Bedrock cloud backend when available,
  falling back to local judge (current behavior) if cloud is down/disabled. Keep the
  single-judge shape — voting was considered and is not worth the token cost at this
  scale; a different-model judge captures most of the benefit.
- **Flag:** `DA_WORKFLOW_VERIFY_CLOUD`, default OFF. Note `workflow_orchestration.enabled`
  is config-gated (`~/.claude/ipad_bridge/config.json`) and verify only runs when a
  `verify_criterion` is passed — this flag is a third gate on top.
- **Effort:** small-medium — the cloud call pattern exists in `CloudPlanRouter`; mostly
  routing + fallback + tests.
- **Risks:** cost per verify call (Bedrock); privacy — worker output text leaves the
  machine (project is local-first; that is exactly why default is OFF and Bedrock-only
  per the established cloud decision); fail-safe must remain "any error ⇒ verified=False"
  (`inference/workflow.py:274` semantics preserved).
- **Verify:** tests mirroring `tests/test_workflow.py` fail-safe cases with a mocked
  cloud backend: cloud error ⇒ not-verified, flag OFF ⇒ zero cloud calls.

## GAP-4: Staleness check on resume seed and replayed reads

**Rank 4 — narrow, mechanical, closes the "checkpoints preserve state not correctness" hole where it is cheapest.**

- **Problem:** `resume_pending_plan` injects `WorkingMemory` (files touched, notes,
  last_failure) derived from `agent_steps` with no check that the filesystem still
  matches — a file modified between crash and resume is silently misrepresented.
  Similarly, superseded-read dedup (`DA_TRAJECTORY_DEDUP`) keeps the *last* read as truth
  with no freshness check.
- **Where:** `inference/working_memory.py:60-103` (`summarize_run`),
  `inference/dev_agent.py:2236-2253` (`_resume_seed_context`).
- **Approach:** at resume, `stat` (mtime/size — cheap; hash optional) each path in
  `WorkingMemory.files`; annotate entries changed since the step timestamp as
  `[STALE — modified after run]` in the seed context, and drop stale `notes` derived
  from those files. Do not re-execute reads — just label, and let the planner decide to
  re-read. Trajectory-replay staleness (mid-run) is explicitly out of scope for the first
  cut: replan happens seconds after the read; resume happens after a crash, where drift
  is plausible.
- **Flag:** `DA_RESUME_STALENESS`, default OFF (per convention), candidate to flip ON
  after a soak — degradation path already exists (any error ⇒ empty seed, byte-identical
  blind resume, `inference/dev_agent.py:2251-2253`).
- **Effort:** small — needs step timestamps (present in `agent_steps`) + `os.stat` per
  path (≤8 paths by design).
- **Risks:** none destructive; be careful that stat failures (deleted file) mark the
  entry stale rather than raising — preserve the silent-degrade contract.
- **Verify:** unit test: touch a file after the recorded step ts ⇒ seed contains STALE
  marker; DB error ⇒ empty seed unchanged.

---

## Conventions checklist (apply to every gap)

- New spec dir per feature under `specs/` (SDD — AGENTS.md rules 9/10), Status lifecycle
  per `specs/TEMPLATE.md`.
- Every new `DA_*` flag registered in `core/flags.py` (D021) — enforced by
  `tests/test_flags_registry.py` — and added to the CLAUDE.md flag table. Default OFF,
  OFF ⇒ byte-identical legacy behavior.
- Decision log entry in `docs/decisions.md` for anything with a rejected alternative
  (e.g., GAP-3 rejects voting-judge). **Next free number: D024** (D023 = vendored
  markdown, chat-workbench-parity) — re-verify against `docs/decisions.md` before use;
  numbering collisions have happened before (D018 → D022 renumber).
- Preserve documented non-goals: Tester failure never rolls back a good write (D008);
  Critic REVISE never snapshots (D007); saga stays per-step compensation, not whole-tree
  stash (D009); voice approval gates keep fail-safe-DENY semantics.
- Changelog entry in `docs/CHANGELOG.md`; daily note in `docs/daily/`.

## Side finding (doc drift, fix opportunistically)

`specs/dev-agent-plan-contract/requirements.md:42` still says **Status: Draft**, but the
feature shipped (PR #130, 2026-06-21; `DA_PLAN_REPAIR` ON, implemented at
`inference/dev_agent.py:917-963`). Flip the Status line when touching that spec.
