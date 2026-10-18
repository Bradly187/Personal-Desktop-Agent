# Spec: Harness Boundary Hardening

> This specification defines safety and reliability invariants for the desktop
> agent's execution, scheduling, timeout, approval, and sandbox boundaries. The
> code is the build artifact; this specification is the source of truth for the
> intended behavior.

---

## 1. Background — the "Why"

The desktop agent already separates its 60 Hz accessibility loop from model
inference and provides approval gates, writable roots, scheduler priorities,
crash recovery, and compensation sagas. An architecture review found that some
of those protections are split across parallel execution paths: DevAgent file
steps do not consistently reuse `CommandExecutor` path containment, Fusion's
route breaker cannot see normal scheduler-owned work, a timed-out worker thread
may complete after failure is reported, approval files are not bound to an exact
action, and sandbox fallback strength is not represented explicitly.

These are boundary-consistency problems, not a reason to rewrite the harness.
The goal is to make existing protections authoritative across every path while
preserving Brad's low-fatigue controls, quiet failure behavior, pain-day
adaptation, resumable sessions, and strict 60 Hz loop.

**Status:** Building
**Approved:** Brad, 2026-09-27
**Owner / author session:** Kiro, 2026-09-27
**Related:** `../hybrid-coordinator-decomposition/`,
`../dev-agent-sagas/`, `../cloud-plan-routing/`,
`../dev-agent-plan-contract/`, `../wsl-terminal-routing/`,
`../sandbox-interactive-hardening/`

---

## 2. Glossary

- **ExecutionScope**: the single runtime authority for writable roots, active
  root, canonical path resolution, and terminal working-directory containment.
- **Canonical path**: a normalized, absolute path resolved sufficiently to
  detect traversal and link/junction escape before a side effect begins.
- **StepExecutor path**: plan-generated actions dispatched through
  `inference/step_executor.py`.
- **CommandExecutor path**: command-domain and fallback actions dispatched
  through `core/command_executor.py`.
- **SchedulerLoadSnapshot**: a cheap, in-memory, immutable view of queued and
  running tasks by priority/source.
- **Fast-lane work**: `ACCESSIBILITY`, `VOICE`, or `GESTURE` scheduler work.
- **Outcome unknown**: a side-effecting action exceeded its caller's wait budget,
  but the harness cannot prove whether the underlying operation completed.
- **Action digest**: a stable SHA-256 digest of the exact destructive action or
  approved plan, including security-relevant arguments.
- **Approval nonce**: a cryptographically random, one-use request identifier.
- **Containment level**: the effective terminal isolation strength:
  `namespace_jail`, `cwd_and_resource_limits`, or `native_unconfined`.
- **Trusted manual work**: a goal explicitly initiated and approved by Brad.
- **Autonomous/untrusted work**: background execution or execution materially
  derived from retrieved content that has not been independently trusted.

---

## 3. Requirements (EARS acceptance criteria)

### Requirement 1: One authoritative execution scope

**User Story:** As Brad, I want every file and terminal path checked against the
same writable roots, so that approving a plan cannot accidentally authorize work
elsewhere on my machine.

#### Acceptance Criteria

1. THE harness SHALL maintain exactly one authoritative `ExecutionScope` instance
   for a production pipeline.
2. FOR ALL `WRITE_FILE` and `EDIT_FILE` actions, THE harness SHALL validate the
   canonical target path against `ExecutionScope` before reading for edit,
   generating a diff, creating a snapshot, creating parent directories, or
   writing bytes.
3. FOR ALL terminal actions, THE harness SHALL validate the canonical working
   directory against `ExecutionScope` before starting a process.
4. FOR ALL saga restore and delete compensations, THE harness SHALL validate the
   canonical target against the same scope before changing the filesystem.
5. WHEN an approved active directory is added during a session, THE shared scope
   SHALL make it available to both the StepExecutor and CommandExecutor paths
   without restarting the pipeline.
6. IF a path is empty, malformed, drive-relative, outside all roots, escapes by
   `..`, or escapes through a symlink/junction, THEN THE harness SHALL deny the
   action before any filesystem side effect.
7. IF canonicalization cannot prove that a path is in scope, THEN THE harness
   SHALL fail safe to DENY.
8. THE path denial result SHALL be structured, locally observable, and quiet; it
   SHALL NOT trigger an unexpected sound or modal interruption.
9. WHILE this requirement is implemented, THE default writable roots and the
   explicit human-confirmation process for adding a root SHALL remain unchanged.

### Requirement 2: Scheduler-owned load accounting and admission

**User Story:** As Brad, I want accessibility commands to remain responsive during
model stalls or sensor noise, so that background work cannot make control of the
computer unreliable.

#### Acceptance Criteria

1. THE `AccessibilityScheduler` SHALL be the authoritative owner of queued and
   running task counts for every priority.
2. THE scheduler SHALL expose a non-blocking `SchedulerLoadSnapshot` containing
   total and per-priority queued/running counts.
3. WHEN Fusion submits work through the scheduler, THE submitted work SHALL be
   represented in scheduler accounting until it reaches a terminal state.
4. WHILE the scheduler is unavailable and Fusion uses direct degraded dispatch,
   THE harness SHALL retain bounded local task accounting and SHALL NOT block the
   60 Hz tick.
5. WHEN configured capacity is approached, THE scheduler SHALL shed or defer
   DEV_AGENT/BACKGROUND work before fast-lane work.
6. WHEN duplicate or stale VOICE/GESTURE work can be identified safely, THE
   scheduler SHALL coalesce that work before dropping a distinct accessibility
   action.
7. IF capacity is exhausted despite prioritization and coalescing, THEN THE
   scheduler SHALL apply a bounded, source-aware fail-safe policy and record the
   reason; it SHALL NOT allow unbounded task growth.
8. THE overload policy SHALL reserve capacity for direct accessibility work and
   SHALL NOT use a pain-day state to reduce that reserved capacity.
9. THE scheduler SHALL consume or report terminal exceptions from ignored
   fire-and-forget futures so they do not become unobserved failures.
10. THE watchdog and metrics surfaces SHALL report the scheduler's authoritative
    counts rather than a partial duplicate maintained by Fusion.

### Requirement 3: Honest timeout outcomes

**User Story:** As Brad, I want the agent to distinguish a failed action from an
action that may still be finishing, so that it does not repeat a destructive
operation after a timeout.

#### Acceptance Criteria

1. THE harness SHALL represent action lifecycle states sufficient to distinguish
   `completed`, `failed`, and `outcome_unknown`.
2. WHEN a side-effecting worker exceeds its caller's wait budget and cannot be
   cancelled conclusively, THE harness SHALL report `outcome_unknown`, not
   `failed`.
3. WHEN an action is `outcome_unknown`, THE plan controller SHALL NOT
   automatically retry it or execute a replacement destructive step until
   reconciliation proves the prior outcome.
4. WHEN a late worker completes, THE harness SHALL record its final observation
   against the original trace, run, and step where available.
5. FOR ALL file replacements, THE harness SHALL use an atomic replacement
   strategy where the host filesystem supports it, while preserving the
   pre-write saga snapshot.
6. FOR ALL terminal subprocesses, THE harness SHALL retain a process handle or
   equivalent control needed to terminate the process tree on timeout.
7. IF process-tree termination cannot be verified, THEN THE terminal action
   SHALL remain `outcome_unknown`.
8. WHEN reconciliation is possible, THE harness SHALL use tool-specific evidence:
   file hash/existence for writes, process state/exit for terminal work, and
   screen/window observation for desktop actions.
9. THE external command response contract SHALL either remain backward-compatible
   or be versioned explicitly on every affected producer and consumer.

### Requirement 4: Action-bound, replay-resistant approval

**User Story:** As Brad, I want one clear approval to authorize only the plan I
was shown, so that a stale response cannot approve a different action.

#### Acceptance Criteria

1. FOR EVERY destructive approval request, THE harness SHALL generate a
   cryptographically random approval nonce.
2. THE request SHALL bind the nonce to the trace ID, action digest, creation time,
   expiry time, and human-readable description.
3. WHEN a response arrives, THE approval gate SHALL accept it only if its nonce
   and action digest match the active request and it has not expired.
4. IF a response is malformed, stale, mismatched, duplicated, replayed,
   ambiguous, or absent, THEN THE approval gate SHALL fail safe to DENY.
5. WHEN a valid response is consumed, THE request SHALL become unusable for any
   later approval.
6. THE request and response files SHALL be written and consumed atomically so
   concurrent readers cannot observe partial records.
7. WHILE whole-plan approval is valid, THE harness SHALL preserve one-approval
   execution for ordinary destructive plans; this hardening SHALL NOT introduce
   repetitive prompts.
8. WHEN the Critic escalates a specific edit, THE forced per-operation approval
   SHALL receive its own nonce and digest and SHALL NOT weaken whole-plan policy.
9. IF an approval field is added to a WebSocket payload, THEN the Python and Swift
   payload definitions SHALL be updated in the same change.
10. WHILE legacy approval channels are migrated, THE compatibility period SHALL
    be bounded and observable; the final state SHALL reject unbound legacy
    responses.

### Requirement 5: Explicit and enforceable containment strength

**User Story:** As Brad, I want to know when terminal work is strongly isolated
and prevent autonomous work from silently running unconfined, while keeping my
explicitly approved personal workflows available.

#### Acceptance Criteria

1. THE terminal harness SHALL classify its effective containment level as
   `namespace_jail`, `cwd_and_resource_limits`, or `native_unconfined`.
2. THE active containment level SHALL be available through runtime status and
   metrics without requiring log inspection.
3. WHEN a trusted manual goal runs below `namespace_jail`, THE harness MAY use the
   existing documented graceful fallback only after normal approval and scope
   checks succeed.
4. WHEN autonomous/untrusted work requires a configured minimum containment
   level, THE harness SHALL verify that level before process creation.
5. IF the required containment level is unavailable, THEN THE harness SHALL deny
   the autonomous/untrusted terminal action rather than silently downgrade it.
6. THE containment decision SHALL NOT weaken the Bash allowlist, writable-root
   policy, network policy, resource limits, or approval gate.
7. THE user-facing fallback or denial notice SHALL be quiet, concise, and
   non-repeating within a session unless the containment state changes.

### Requirement 6: Accessibility and resource invariants

**User Story:** As Brad, I want security hardening to preserve the controls I rely
on, so that safer execution does not make the accessibility agent tiring or
unreliable.

#### Acceptance Criteria

1. THE implementation SHALL NOT introduce synchronous I/O, heavy computation, or
   blocking inference into `core/fusion_engine.py` or the scheduler's hot path.
2. THE 60 Hz tick SHALL continue to isolate per-tick failures and use
   deadline-based scheduling.
3. WHILE model inference, terminal execution, or a background subsystem is
   stalled, direct tilt/touch control SHALL remain available through the existing
   graceful-degradation path.
4. THE implementation SHALL preserve `BehavioralTwinState.apply_pain_day()` and
   `ResourceGovernor` control over thresholds, admission, and model eviction.
5. THE implementation SHALL preserve crash recovery, durable goal/run state, and
   per-file saga compensation.
6. THE implementation SHALL prefer quiet coalescing or denial over startling
   sounds, flashing UI, abrupt modal interruption, or process failure.
7. THE implementation SHALL NOT add a new model or change the model roster.

### Requirement 7: Verification and governance alignment

**User Story:** As Brad, I want these boundaries proven and accurately documented,
so that future agents cannot reintroduce a bypass through another execution path.

#### Acceptance Criteria

1. EACH behavioral criterion in Requirements 1–5 SHALL map to a focused test or
   executable eval, with cross-layer tests for the production combinations that
   previously lacked coverage.
2. THE path suite SHALL cover direct StepExecutor and CommandExecutor actions,
   plan and replan injection, traversal, absolute escape, symlink/junction escape,
   dynamic active roots, and saga compensation.
3. THE scheduler suite SHALL cover scheduler-backed slow-route storms, accurate
   all-priority accounting, bounded growth, fast-lane reservation, and degraded
   dispatch.
4. THE timeout suite SHALL prove that a late side effect is not automatically
   retried and that its eventual outcome is correlated to the original trace.
5. THE approval suite SHALL cover mismatch, expiry, replay, malformed input, and
   concurrent response races.
6. THE containment suite SHALL cover every containment level and denial of
   autonomous/untrusted execution below its required level.
7. BEFORE each runtime phase is merged, THE affected focused tests, full pytest
   suite, and model-free eval gates SHALL pass.
8. WHEN Ollama is available, THE plan-contract and trajectory gates SHALL run;
   IF it is unavailable, THEN the skipped model-backed validation SHALL be
   reported explicitly and SHALL NOT rewrite a baseline.
9. THE implementation SHALL NOT update an eval baseline solely to make a
   regression pass.
10. BEFORE this specification moves to `Building`, THE existing status/default
    conflicts listed in Section 4 SHALL have explicit human decisions recorded.
11. IF implementation requires an `agent.db` schema change, THEN work SHALL stop
    until the schema-change procedure is approved; `storage/schema/agent.py`
    SHALL remain the schema source of truth.

---

## 4. Resolved Governance Decisions

Brad resolved these decisions when approving this specification on 2026-09-27:

- `DA_PLAN_REPAIR` remains default ON; reconcile
  `../dev-agent-plan-contract/` to the implemented, eval-gated behavior.
- CloudPlanRouter is ratified as implemented and `DA_CLOUD_PLAN` remains default
  OFF; reconcile `../cloud-plan-routing/` without enabling cloud egress by
  default.
- `../hybrid-coordinator-decomposition/` is Complete on `master`.
- Autonomous/untrusted terminal execution requires `namespace_jail`; trusted
  manual work retains the documented approved fallback.

The original alternatives and rationale are retained below as the decision
record for this spec.

1. **Plan-contract status/default — decided default ON.** The deterministic
   repair baseline is locked at 1.0 and focused tests pass; explicit
   `DA_PLAN_REPAIR=0` remains the rollback path.
2. **Cloud-plan-routing status — implementation ratified, default OFF.** The
   reviewed implementation keeps execution local and cloud egress opt-in through
   `DA_CLOUD_PLAN=1`.
3. **Coordinator-decomposition status — decided Complete.** The reviewed runtime
   structure matches the approved decomposition intent on `master`.
4. **Autonomous/untrusted containment — decided `namespace_jail`.** Trusted
   manual work may use the documented fallback after scope, allowlist, and
   approval checks; autonomous/untrusted terminal work may not.

---

## 5. Non-goals

- Rewriting `HybridCoordinator`, `DevAgent`, or the scheduler wholesale.
- Moving inference or policy I/O into the 60 Hz loop.
- Adding a new model, replacing the current router, or changing VRAM policy.
- Replacing per-file compensation with whole-tree `git stash`.
- Treating the sandbox as adversarial microVM isolation.
- Expanding writable roots automatically.
- Adding repetitive confirmations to ordinary approved plans.
- Changing iPad protocol payloads unless approval correlation requires it.
- Changing `agent.db` schema as part of the initial design.

---

## 6. Gate State

Gate 1 passed when Brad approved the requirements, design, and four governance
decisions on 2026-09-27. Gate 2 passed when Brad approved `tasks.md` on the same
date. The specification is now **Building**, and implementation proceeds in the
approved task order.
