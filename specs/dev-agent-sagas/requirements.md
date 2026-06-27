# Spec: DevAgent Sagas (Edit Transactions)

---

## 1. Background — the "Why"

> **Status: CLOSED — core already implemented; two enhancements shipped 2026-06-27.**
> The original draft assumed the DevAgent executed file edits "forward-only" with no
> rollback. That premise was **incorrect**: a durable, per-step compensation saga
> already exists in `inference/dev_agent.py` + `storage/db.py` (shipped as the
> E2/E3/E5/E6 durable-failure error-handling work). It satisfies every requirement
> below, and in several respects exceeds what this spec proposed. The two genuine
> gaps found at close-out (proactive rollback announcement + git-blob snapshot
> backend) were then implemented — see §7. This document is retained as
> **documentation of the mechanism**, not an open build item.

The DevAgent treats the destructive steps of a single planner goal as a saga: each
successful destructive step registers a **compensation** (its reverse action). If
the goal terminates abnormally (replan budget exhausted, step cap hit, or user
cancel), the registered compensations are unwound **in reverse order**, restoring
the repository to its pre-goal state. This guarantees a multi-file refactor never
leaves the repo half-edited when the agent gives up.

**Owner / author session:** Antigravity (draft) → closed by Claude Code 2026-06-27.

---

## 2. Glossary

- **Compensation**: The reverse action for a completed destructive step
  (`RESTORE_FILE` for a WRITE/EDIT, `DELETE_FILE` for a created file with no
  snapshot, `REVERT_TERMINAL` = human-review note for an irreversible shell effect).
- **Snapshot**: A pre-write backup of a file's original bytes, stored under
  `~/.claude/saga/` and referenced by the compensation row.
- **Rollback / unwind**: Running all pending compensations for a run in reverse on
  terminal failure (`_halt_and_compensate` / `_run_compensations`).

---

## 3. Requirements (EARS acceptance criteria) — all SATISFIED

### Requirement 1: Transaction Staging — ✅ implemented
**User Story:** As the DevAgent, I want my edits staged so they can be undone if my
goal fails.

#### Acceptance Criteria
1. THE system SHALL snapshot a file's original state before applying `WRITE_FILE`
   or `EDIT_FILE`.
   → `DevAgent._snapshot_for_write()` (`inference/dev_agent.py:1619`), captured at
   EXECUTE time into `~/.claude/saga/<name>.<uuid>.bak` for **both** WRITE_FILE and
   EDIT_FILE (`_compensation_for`, `inference/dev_agent.py:1650`).
2. WHEN a sequence of edits is performed, THE system SHALL maintain a list of all
   modified files and their pre-transaction snapshots.
   → One `saga_compensations` row per successful destructive step
   (`storage/db.py:687`, registered at `inference/dev_agent.py:1723`). Multiple
   writes to the same file keep the **earliest** snapshot (last-writer dedup,
   `inference/dev_agent.py:1440`), which is exactly transaction semantics.

### Requirement 2: Transaction Rollback — ✅ implemented
**User Story:** If I exhaust replans, I want partial edits rolled back.

#### Acceptance Criteria
1. WHEN the DevAgent exhausts `MAX_REPLANS` (or `MAX_STEPS`, or is cancelled), THE
   system SHALL restore all modified files from their snapshots.
   → `_halt_and_compensate()` on terminal step failure with no recovery plan
   (`inference/dev_agent.py:1217`, `:1774`); `_run_compensations()` on `max_steps`
   (`:1179`) and `user_cancel` (`:1233`). `RESTORE_FILE` restores from the backup,
   or deletes a file the plan created (`:2014`, `:2023`).
2. THE DevAgent SHALL log the rollback to the trace and escalate incomplete
   rollbacks to a human.
   → Compensations are traced; a compensation that FAILED or was SKIPPED
   self-escalates to the review queue with reason `compensation_failed`
   (`inference/dev_agent.py:1227`). **Note:** there is no proactive TTS
   announcement of a *successful* rollback — only failed/incomplete rollbacks reach
   a human. A user-facing "edits were reverted" notice would be a separate, narrow
   enhancement, not part of this (closed) spec.

### Requirement 3: Transaction Commit — ✅ implemented
**User Story:** On success I want my edits finalized.

#### Acceptance Criteria
1. WHEN a goal completes successfully, THE system SHALL finalize the transaction.
   → Successful completion never runs compensations; the registered rows simply go
   unused (no explicit "commit" step is needed — disk already holds the writes).

---

## 4. Deliberate non-goals (do NOT "fix" these)

- **Critic REVISE does not snapshot/roll back** — the Critic runs *before* the disk
  commit (after the lint gate), so there is nothing written to undo. Adding a saga
  here would be dead code. (CLAUDE.md, "Independent Critic + autonomous Tester".)
- **Tester failure never rolls back the good write** — a failing generated test is
  a *safe-observation* that feeds `_reflect`/replan; the valid write is kept by
  design. Implementing this spec's "Critic or Tester rejects → roll back" phrasing
  literally would **regress** this intentional behavior. (CLAUDE.md, same section.)
- **RUN_TERMINAL is not auto-reversed** — shell side effects can't be safely undone,
  so the saga records a `REVERT_TERMINAL` note for manual review instead
  (`inference/dev_agent.py:1659`, `:1978`).

---

## 5. Behavior Verification

- Core saga/compensation behavior: the durable-failure (E2/E3/E5/E6) tests
  (`tests/test_saga_compensation.py`, `tests/test_saga_integrity.py`,
  `tests/test_eh2_durable_failure.py`).
- The two 2026-06-27 enhancements: `tests/test_saga_enhancements.py` (14 tests —
  rollback-summary counts, `_rollback_notice` formatting, cancel-path announcement,
  git-blob capture/restore/large-file/fallback, flag-off byte-identity).

---

## 6. Tasks

- [x] 1. Snapshot creation before WRITE/EDIT — `_snapshot_for_write` (R1.1)
- [x] 2. Per-run transaction context — `saga_compensations` rows (R1.2)
- [x] 3. Rollback on replan/step-cap/cancel — `_halt_and_compensate` / `_run_compensations` (R2.1, R2.2)
- [x] 4. Finalize on success — no-op finalization (R3.1)
- [x] 5. Proactive rollback TTS announcement — `DA_SAGA_ANNOUNCE` (R2.2 enhancement, 2026-06-27)
- [x] 6. Git-blob snapshot backend — `DA_SAGA_GIT_BACKEND` (large-file rollback, 2026-06-27)
- [x] 7. Tests — `tests/test_saga_enhancements.py`

---

## 7. Reality check that closed this spec (2026-06-27)

The draft's premise ("forward-only, no rollback") was checked against the code and
found false:

| Draft assumption | Reality |
|---|---|
| Edits are forward-only | Per-step compensation saga unwinds on terminal failure |
| Need to *build* snapshotting | `_snapshot_for_write` already snapshots WRITE + EDIT |
| Need rollback on replan exhaustion | `_halt_and_compensate` already does this |
| Snapshots in-memory/temp files | Already DB-persisted + crash-durable (stronger) |
| (not considered) partial-write rollback | E6 already rolls back truncated/half-written files |
| (not considered) irreversible shell effects | `REVERT_TERMINAL` manual-review note already exists |

**Enhancements shipped 2026-06-27** (the two gaps the close-out identified):

1. **Proactive rollback announcement (`DA_SAGA_ANNOUNCE`, default ON).** R2.2's
   spirit: a saga rollback now speaks a short TTS summary of what it reverted
   ("Reverted N file changes.") — closing the previously-silent **user-cancel**
   path. `_run_compensations` records `{reverted, manual, incomplete, triggered_by}`
   in `self._rollback_summary`; `_rollback_notice()` renders it and
   `_speak_plan_completion` appends it on the cancel + non-escalated-failure paths.
   `=0` → byte-identical legacy completion speech. The pre-existing escalated-failure
   message ("Changes rolled back and saved to the review queue") is unchanged to
   avoid double-announcing. Code: `inference/dev_agent.py` (`_rollback_notice`,
   `_run_compensations`). Tests: `tests/test_saga_enhancements.py`.

2. **Git-blob snapshot backend (`DA_SAGA_GIT_BACKEND`, default OFF).** An
   alternative to the size-capped file-copy backend: when the target file is inside
   a git work tree, its pre-write bytes are captured as a git loose object via
   `git hash-object -w` (recorded as `git_blob`/`git_repo` in the snapshot), and
   restored via `git cat-file blob`. **No 256 KB cap** — closes the file-copy
   backend's large-file rollback gap. Touches ONLY the object store (never the
   working tree, index, or stash stack), so it's side-effect-free for the user's
   repo and composes with the new VCS/git MCP tools. Restore is self-describing
   (works regardless of the current flag) and degrades to file-copy outside a repo
   or when git is absent. `=0`/absent → byte-identical file-copy snapshots. Code:
   `_snapshot_for_write`, `_git_blob_snapshot`, `_git_cat_blob`, `_restore_file`.

> Note: a *whole-tree* `git stash` was deliberately **not** used — it would also
> stash the user's unrelated uncommitted work. The per-file blob approach is the
> safe, scoped equivalent.
