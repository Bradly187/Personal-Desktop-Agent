# Tasks: Harness Boundary Hardening

**Spec:** [requirements.md](requirements.md)  
**Design:** [design.md](design.md)  
**Status:** Building — Gate 2 approved; execute phases in order  
**Gate 1 approved:** Brad, 2026-09-27  
**Gate 2 approved:** Brad, 2026-09-27

> Do not execute any task in this file until Brad explicitly approves this task
> plan. Complete phases in order. Each runtime phase is a separate reviewable PR;
> do not mix the existing L515 working-tree changes into these branches or commits.

---

## Stop Conditions

Stop and request review before continuing if any phase:

- requires an `agent.db` schema change — load the `changing-the-db-schema` skill,
  read `storage/schema/agent.py`, and draft a backwards-compatible migration;
- changes an iPad bridge JSON field or message type — mirror Python and Swift in
  the same phase;
- adds blocking work, filesystem I/O, or inference to the 60 Hz Fusion loop;
- changes pain-day threshold behavior outside `BehavioralTwinState.apply_pain_day()`;
- adds a model or changes the ResourceGovernor eviction lifecycle;
- requires widening `writable_roots`, weakening fail-safe denial, or bypassing
  approval to make a test pass;
- produces a model/eval regression that would require re-baselining.

---

## Phase 0 — Baseline and branch isolation (pre-PR)

- [x] 0.1 Re-check `git status`, recent log, and open PRs; record and preserve the
      pre-existing L515 changes. Do not stage or edit them.
- [x] 0.2 Create/use a dedicated feature branch or clean worktree for harness
      hardening; do not commit unless Brad explicitly requests it.
- [x] 0.3 Run the current focused harness tests and record the baseline:
      `test_fusion_dispatch`, `test_route_task_circuit_breaker`,
      `test_executor_writable_root`, `test_approval_gate`, `test_sandbox`,
      `test_cloud_plan_router`, and `test_plan_contract`.
- [x] 0.4 Run the model-free eval gate using the repository eval-harness procedure
      and record any pre-existing failure without changing a baseline — satisfies
      R7.7–R7.9.

**Exit:** clean isolated branch/worktree and documented green baseline, or an
explicit list of pre-existing failures.

---

## Phase 1 / PR 1 — Shared execution scope (P0)

### 1A. Pin the missing boundary

- [x] 1.1 Add focused failing tests for direct StepExecutor `WRITE_FILE` and
      `EDIT_FILE` absolute/traversal escapes, asserting denial occurs before
      `apply_edit`, diff, snapshot, parent creation, or write — satisfies R1.2,
      R1.6–R1.8, R7.1–R7.2.
- [x] 1.2 Add tests for malformed/empty/drive-relative paths, symlink escape, and
      Windows junction escape where supported; uncertain canonicalization must
      deny — satisfies R1.6–R1.7, R7.2.
- [x] 1.3 Add cross-layer tests for an escaped path introduced by a replan and an
      out-of-scope saga restore/delete — satisfies R1.4, R7.2.

### 1B. Build the authority

- [x] 1.4 Add `core/execution_policy.py` with typed `ScopeDecision` and one
      `ExecutionScope` owning canonical roots and active-root state. Implement the
      canonicalization algorithm from design §3.1 — satisfies R1.1, R1.6–R1.7.
- [x] 1.5 Move existing writable-root loading and active-root mutation behind
      `ExecutionScope` without changing default roots or the human-confirmed root
      expansion flow — satisfies R1.5, R1.9.
- [x] 1.6 Inject the same `ExecutionScope` instance into CommandExecutor,
      HybridCoordinator/active-directory handling, and DevAgent/StepExecutor from
      the production composition root — satisfies R1.1, R1.5.

### 1C. Enforce every side-effect path

- [x] 1.7 Update CommandExecutor to consume the shared policy while preserving its
      current structured denial contract and in-scope behavior — satisfies R1.3,
      R1.8–R1.9.
- [x] 1.8 Update StepExecutor to use the canonical path returned by the policy and
      authorize before every read-for-edit, diff, snapshot, directory creation,
      write, and terminal cwd — satisfies R1.2–R1.3, R1.6–R1.8.
- [x] 1.9 Update saga snapshot/restore/delete to store and reauthorize canonical
      paths; preserve incomplete-rollback escalation when authorization fails —
      satisfies R1.4, R6.5.
- [x] 1.10 Verify an approved active directory becomes available immediately to
      both executors without restart — satisfies R1.5.

### 1D. Verify PR 1

- [x] 1.11 Run all Phase 1 tests plus existing writable-root, edit-format, DAG,
      saga-compensation, saga-integrity, chat-attachment, and approval suites.
- [x] 1.12 Run full pytest and model-free eval gates. Confirm no new DB schema,
      bridge payload, 60 Hz path, pain-day, or model-roster change.
- [x] 1.13 Review the PR diff specifically for any raw `Path(...).write_*`,
      `mkdir`, restore/delete, or terminal cwd path that bypasses ExecutionScope.
- [x] 1.14 Close the final proxy-auth review findings: replace bearer transport
      with instance-bound request HMAC, timestamp and bounded nonce replay
      protection; move the bootstrap token outside `PROJ_ROOT` with restrictive
      ACLs; verify readiness proofs in both launchers; scrub proxy credentials
      from all child/git/gh environments; and run focused replacement-listener,
      proxy, path, git, and sandbox tests.

**Exit:** all file/terminal/saga paths share one authority; no bypass flag.

---

## Phase 2 / PR 2A — Authoritative scheduler accounting (observe only)

- [x] 2.1 Add a regression test proving normal scheduler-backed Fusion submissions
      are counted while queued/running; retain a separate degraded-path test —
      satisfies R2.1–R2.4, R7.3.
- [x] 2.2 Add immutable `SchedulerLoadSnapshot` and internal per-task metadata;
      account for every priority through enqueue, dequeue, run, exception,
      timeout, cancellation, and shutdown using `finally` — satisfies R2.1–R2.3.
- [x] 2.3 Add safe done-callback handling for ignored scheduler futures so terminal
      exceptions are observed without changing awaitability — satisfies R2.9.
- [x] 2.4 Publish authoritative total/per-priority queue and in-flight gauges; make
      watchdog reporting consume the scheduler snapshot — satisfies R2.10.
- [x] 2.5 Change Fusion's normal path to rely on scheduler accounting while keeping
      its tracked set only for no-scheduler degraded dispatch. Do not await route
      completion in `_tick()` — satisfies R2.3–R2.4, R6.1–R6.3.
- [x] 2.6 Run focused scheduler/Fusion/supervisor/resource-governor tests, full
      pytest, and model-free evals.

**Exit:** production metrics tell the truth, but admission behavior remains
observe-only and byte-compatible.

---

## Phase 3 / PR 2B — Source-aware bounded admission

- [x] 3.1 Add a deterministic saturation harness with a deliberately slow
      coordinator and mixed DEV_AGENT/BACKGROUND/VOICE/GESTURE/ACCESSIBILITY
      submissions. Assert bounded counts rather than fragile wall-clock timing —
      satisfies R2.5–R2.8, R7.3.
- [x] 3.2 Measure the existing route/queue behavior under the harness and select
      documented configurable ceilings and accessibility reserve; record the
      evidence in the spec or PR, not an inline magic-number comment.
- [x] 3.3 Implement admission order: shed/defer BACKGROUND, then DEV_AGENT;
      coalesce only provably duplicate/stale VOICE/GESTURE work; preserve reserved
      direct-accessibility capacity; apply a hard process-safety ceiling —
      satisfies R2.5–R2.8.
- [x] 3.4 Prove coalescing keys include enough source/action identity that distinct
      user commands are never merged — satisfies R2.6–R2.7.
- [x] 3.5 Prove pain-day state may reduce heavy admission but never accessibility
      reserve — satisfies R2.8, R6.4.
- [x] 3.6 Run the deterministic saturation suite, focused scheduler/Fusion tests,
      full pytest, and model-free evals.
- [x] 3.7 Run a manual hardware soak and confirm watchdog tick rate stays above
      the existing 50 Hz warning threshold. *(Unavailable in this session; remains
      an explicit rollout gate.)*

**Exit:** normal and degraded route growth are bounded; accessibility retains
priority and reserve under storm load.

---

## Phase 4 / PR 3 — Honest timeout outcomes

### 4A. Lifecycle contract

- [x] 4.1 Add `core/action_lifecycle.py` with typed `ActionStatus` and
      `ActionOutcome`, including `outcome_unknown`, plus compatibility conversion
      at existing dict boundaries — satisfies R3.1, R3.9.
- [x] 4.2 Add a bounded tracked-work registry that retains a shielded worker handle,
      original trace/run/step identity, expiry, and reconciliation callback —
      satisfies R3.2, R3.4.
- [x] 4.3 Add tests where a side-effecting worker exceeds the caller timeout and
      later succeeds/fails; assert it is not reported as failed prematurely and
      the late observation uses the original trace — satisfies R3.2, R3.4,
      R7.4.

### 4B. Controller semantics

- [x] 4.4 Update PlanExecutor so a destructive `outcome_unknown` stops the
      destructive branch and cannot trigger retry or destructive recovery replan
      until reconciled — satisfies R3.3.
- [x] 4.5 Define a short bounded reconciliation wait and a durable interrupted/
      unknown result when evidence remains unavailable; preserve resumable context
      and quiet user feedback — satisfies R3.3–R3.4, R6.5–R6.6.
- [x] 4.6 Keep read-only retries limited to operations proven side-effect-free and
      idempotent.

### 4C. Tool-specific safety

- [x] 4.7 Add atomic same-directory temporary-write + flush/close + `os.replace`
      behavior while preserving pre-write saga snapshots and practical file
      permissions — satisfies R3.5.
- [x] 4.8 Retain process handles/groups for terminal execution; terminate the full
      process tree on timeout and mark failure only when termination is verified —
      satisfies R3.6–R3.7.
- [x] 4.9 Add reconciliation observers for file hash/existence, process state/exit,
      and supported desktop screen/window evidence — satisfies R3.8.
- [x] 4.10 Test registry saturation/expiry, atomic-write interruption, verified and
      unverified process termination, no destructive retry, and trace correlation.
- [x] 4.11 Run focused executor/plan/saga/sandbox tests, full pytest, and applicable
      eval gates.

**Exit:** timeout is never conflated with proven failure; destructive unknown work
is not repeated.

---

## Phase 5 / PR 4A — Structured approval authority

- [x] 5.1 Add typed `ApprovalRequest`, `ApprovalResponse`, and `ApprovalGrant`
      contracts plus canonical action/plan digest construction — satisfies
      R4.1–R4.3.
- [x] 5.2 Include canonical path/cwd, security-relevant args, content/body hash,
      step identity, goal, and trace in the appropriate digest. Presentation text
      is never authority — satisfies R4.2–R4.3.
- [x] 5.3 Add per-request JSON paths and atomic temp-file replacement for request
      and response writes — satisfies R4.6.
- [x] 5.4 Enforce matching nonce/digest, expiry, one-use consumption, and fail-safe
      denial for malformed, absent, stale, mismatched, duplicate, or replayed
      responses — satisfies R4.3–R4.6.
- [x] 5.5 Bind whole-plan approval to the exact ordered destructive-step digests;
      a replan-introduced or changed step must request new approval — satisfies
      R4.7.
- [x] 5.6 Preserve Critic-forced per-operation escalation with a distinct request —
      satisfies R4.8.
- [x] 5.7 Add focused mismatch/expiry/replay/malformed/concurrency tests, including
      two requests receiving crossed responses — satisfies R7.5.

**Exit:** the core approval authority is structured and replay-resistant; legacy
channels are not removed yet.

---

## Phase 6 / PR 4B — Approval channel migration and strict mode

- [x] 6.1 Update voice approval, chat approval cards/responses, local approval hook,
      and iPad/A2UI responder to carry the request ID and action digest.
- [x] 6.2 IF a WebSocket JSON payload changes, update the Swift message models and
      Python/Swift contract tests in this same phase — satisfies R4.9.
- [x] 6.3 Implement a bounded, observable dual-read migration only while a channel
      still emits legacy responses. Legacy input may be considered only when one
      unambiguous compatible request is active — satisfies R4.10.
- [x] 6.4 Add telemetry for legacy acceptance and confirm all channels use
      structured responses.
- [x] 6.5 Remove/disable legacy acceptance as the final state and add a test proving
      unbound legacy `yes` cannot authorize an action — satisfies R4.4–R4.5,
      R4.10.
- [x] 6.6 Run all approval, chat, A2UI, bridge, Swift protocol (if affected), plan,
      and Critic tests; then full pytest and model-free evals.

**Exit:** every approval channel is exact-action-bound; one normal whole-plan
approval remains the common UX.

---

## Phase 7 / PR 5 — Explicit containment and trust-sensitive enforcement

- [x] 7.1 Add typed `ContainmentLevel`, `WorkTrust`, and `ContainmentReport` to the
      sandbox boundary; detect the effective mechanism after platform, WSL,
      tool availability, config, and path translation — satisfies R5.1.
- [x] 7.2 Expose containment level/mechanism/reason through runtime status and
      metrics — satisfies R5.2.
- [x] 7.3 Map existing provenance/trust signals to `TRUSTED_MANUAL` or
      `AUTONOMOUS_OR_UNTRUSTED`; unknown provenance is conservative. Do not add a
      model — satisfies R5.3–R5.6, R6.7.
- [x] 7.4 Preserve existing scope, Bash allowlist, network policy, resource limits,
      and approval before containment admission — satisfies R5.6.
- [x] 7.5 Enforce Brad's approved policy: trusted manual work may use documented
      fallback after all other gates; autonomous/untrusted terminal work requires
      `NAMESPACE_JAIL` and denies before process creation otherwise — satisfies
      R5.3–R5.5.
- [x] 7.6 Add one quiet state-transition notification and suppress repetition until
      containment state changes — satisfies R5.7, R6.6.
- [x] 7.7 Test every containment level and mechanism, trusted fallback,
      autonomous/untrusted denial, unknown provenance, status/metrics, and
      notification deduplication — satisfies R7.6.
- [ ] 7.8 Run focused sandbox/goal-session/trust tests, full pytest, model-free
      evals, and sandbox eval mode where available.

**Exit:** effective sandbox strength is visible; autonomous/untrusted shell work
cannot silently run unconfined.

---

## Phase 8 / PR 6 — Final integration, security review, and documentation

- [ ] 8.1 Run the full pytest suite and all model-free eval gates — satisfies R7.7.
- [ ] 8.2 With Ollama available, run plan-contract and trajectory gates; report any
      unavailable model-backed gate explicitly and never rewrite a baseline merely
      to pass — satisfies R7.8–R7.9.
- [ ] 8.3 Run the repository security scan over changed code and resolve or
      explicitly document findings.
- [ ] 8.4 Run a final invariant audit:
      - no raw side-effect path bypasses ExecutionScope;
      - scheduler owns all normal-path load accounting;
      - destructive unknown outcomes cannot retry;
      - approvals are nonce/digest/expiry bound;
      - autonomous/untrusted terminal work requires namespace jail;
      - 60 Hz, pain-day, ResourceGovernor, crash recovery, and saga behavior remain.
- [ ] 8.5 Update the spec's verification evidence and residual-risk section; update
      statuses only to the state actually reached.
- [ ] 8.6 Run `/doc-update`; route history to `docs/CHANGELOG.md`, keep CLAUDE.md
      additions within the Rule 13 matrix, and avoid duplicating spec content.
- [ ] 8.7 Present the final diff and validation results. Do not commit, push, or
      create PRs unless Brad explicitly requests those actions.

**Exit / definition of done:** all requirements have executable evidence, all
runtime phases pass their gates, documentation matches code, and no known split
execution/admission policy path remains.

---

## Gate 2

Brad approved this task plan on 2026-09-27. Execute tasks in order, honor every
stop condition, and record completion only after the listed validation passes.
