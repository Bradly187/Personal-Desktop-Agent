# Spec: Resume Working-Memory Snapshot (Gap C)

> One feature → one folder. This spec is the source of truth; the code is the
> build artifact. Design and Tasks are kept inline (§4–§6) until they outgrow
> the file. Keep this updated as the design evolves.

---

## 1. Background — the "Why"

Gap analysis (2026-06-26) against `rasbt/mini-coding-agent` found PDA's
durability is *stronger* than the reference (real SQLite ledger —
`agent_runs`/`agent_steps`/`goal_queue` in `storage/db.py` — vs. flat session
files), but its **resume path is blind**. The reference keeps a small working
memory alongside the full transcript — `memory{task, files[≤8], notes[≤5]}` —
and replays it on `--resume` so the model knows what was already done.

PDA's `DevAgent.resume_pending_plan` (`inference/dev_agent.py` ≈L1936) instead
calls `plan_and_run(goal)` **from scratch**: it re-plans the goal with no memory
of which files the crashed run already touched, what it observed, or how it
failed. The information exists — every step is durably in `agent_steps` — it is
just never read back on resume. The result is wasted re-investigation and a risk
of redoing side effects the closed-loop controller then has to reconcile.

The fix is deliberately schema-free: **derive** a compact working-memory snapshot
from the already-persisted `agent_steps` at resume time and seed the resumed plan
with it. No new table, no `user_version` bump — the durable transcript is already
the source; we just summarize it. (A persisted incremental snapshot table is a
noted alternative, deferred — see §4.)

**Status:** Done — crash-resume (R1–R3) and cross-session (R4) both shipped.
`inference/working_memory.py` (`WorkingMemory`/`summarize_run`/`render_seed`/
`memory_enabled` + cross-session `score_relevance`/`select_related_runs`/
`render_session_seed`/`session_memory_enabled`) + read-only
`AgentDB.get_steps_for_run` & `get_recent_runs` (no schema change) + `seed_context`
param on `plan_and_run`/`_plan_and_run_locked` + `DevAgent._resume_seed_context`
(crash-resume) & `_session_seed_context` (cross-session) wired in. `DA_RESUME_MEMORY`
default **ON** (5b integration test confirms no regression); `DA_SESSION_MEMORY`
default **OFF** until verified on real runs. 21 tests green
(`tests/test_resume_working_memory.py`).
**Owner / author session:** Claude Code (Opus 4.8)
**Related:** `../accessibility-agent/` (DevAgent), `./` siblings
`../repo-context-ingestion/` + `../trajectory-read-dedup/` (the other two
gap-closure specs). Honors AGENTS.md #1 (no schema drift — derive, don't add a
table), #4 (resume already fail-safe-DENY via voice confirm — unchanged).

---

## 2. Glossary

- **WorkingMemory**: the new compact snapshot this spec introduces — a dataclass
  `{goal, files: list[str] (≤8 recent distinct paths touched), notes: list[str]
  (≤5 recent result snippets), last_failure: str | None}`.
- **`agent_steps`**: durable per-step ledger (`storage/db.py` ≈L184 — run_id,
  step_num, action, args, body, result, success, latency_ms). The source the
  snapshot is derived from. **Unchanged by this spec.**
- **Interrupted run**: a row returned by `AgentDB.get_interrupted_runs` — a plan
  left mid-flight by a crash/restart. `resume_pending_plan` offers to resume it
  (voice-gated).
- **Seed context**: the WorkingMemory rendered as a text block and injected into
  the resumed `plan_and_run`'s `extra_ctx`, ahead of the fresh RAG/git context.

---

## 3. Requirements (EARS acceptance criteria)

### Requirement 1: Derive a compact snapshot from the durable transcript

**User Story:** As Brad, when the agent resumes a task it crashed on, I want it to
remember what it already did and how it failed, so it doesn't blindly start over.

#### Acceptance Criteria
1. THE `summarize_run(steps)` SHALL derive a `WorkingMemory` deterministically from
   a run's persisted `agent_steps` — no LLM call: `files` = the ≤8 most-recent
   DISTINCT paths appearing in write/read step `args`; `notes` = ≤5 most-recent
   non-empty result snippets (clipped); `last_failure` = the result/error of the
   last step with `success is False`, else `None`.
2. THE snapshot SHALL preserve the FULL `last_failure` text within its char budget
   (it is the single most decision-relevant fact for a resumed plan).
3. THE rendered seed block SHALL be bounded (`max_chars`, default 1500) and
   deterministically ordered (files, then notes, then last failure).

### Requirement 2: Seed the resumed plan

#### Acceptance Criteria
1. WHEN `resume_pending_plan` resumes a run, IT SHALL load that run's steps and
   inject `summarize_run(...)`'s seed block into the resumed `plan_and_run`'s
   context, ahead of the dynamic RAG/git context.
2. `plan_and_run` SHALL accept an optional `seed_context: str = ""` that is
   prepended to `extra_ctx` in `_plan_and_run_locked`; WHEN empty, behavior SHALL
   be byte-identical to today (the non-resume path passes nothing).

### Requirement 3: Safe, schema-free, degrades cleanly

#### Acceptance Criteria
1. THE feature SHALL add NO `agent.db` table or column and SHALL NOT bump
   `PRAGMA user_version` (AGENTS.md #1) — the snapshot is derived from existing
   `agent_steps` at read time.
2. IF the run has no persisted steps, or the DB is unavailable, THEN
   `summarize_run` SHALL return an empty `WorkingMemory` and resume SHALL proceed
   exactly as today (blind re-plan) — never raise.
3. THE resume path's existing voice-confirm gate (`_confirm_destructive_op`) and
   decline-rollback (`_run_compensations`) SHALL be unchanged — seeding adds
   context only, it does NOT alter the fail-safe-DENY approval (AGENTS.md #4).
4. THE crash-resume feature SHALL be controlled by `DA_RESUME_MEMORY`, default
   **ON** (verified by the integration test in §5 — flag-off branch is a
   byte-identical regression guard); WHILE off, `resume_pending_plan` SHALL call
   `plan_and_run(goal)` exactly as today.

### Requirement 4: Cross-session seeding (generalize the same mechanism)

**User Story:** As Brad, when I start a *new* task related to work the agent did in
an earlier session, I want it to remember what those runs already learned about the
same files, so it builds on them instead of re-investigating from zero. This is the
most direct address to the session-level context-loss ("Codified Context") gap.

#### Acceptance Criteria
1. THE `score_relevance(new_goal, prior_goal)` SHALL return a lexical similarity in
   `[0.0, 1.0]` — Jaccard overlap over content-word sets, deterministic, no LLM /
   no embeddings (identical content → 1.0, disjoint → 0.0).
2. THE `select_related_runs(new_goal, candidate_runs, *, top_k=3, min_score=0.2)`
   SHALL keep only candidates scoring ≥ `min_score` and return the top-`top_k`
   sorted by score desc then `ts` desc (stable); `[]` when nothing clears the bar.
3. THE `render_session_seed` SHALL render the selected runs' `WorkingMemory` into a
   single bounded (`max_chars`, default 2000) `<prior-session-memory>` block — a
   tag DISTINCT from `<resumed-task-memory>` so the planner separates "the run I'm
   resuming" from "earlier related runs"; `""` when empty.
4. WHEN `plan_and_run` is called WITHOUT a caller `seed_context` (a fresh task) AND
   `DA_SESSION_MEMORY` is on, `_plan_and_run_locked` SHALL derive a session seed via
   `_session_seed_context(goal)` and prepend it to `extra_ctx`; a crash-resume
   `seed_context` takes precedence (mutually exclusive — never double-seed). WHILE
   `DA_SESSION_MEMORY` is off (default), the planner context SHALL be byte-identical
   to today, and no recent-runs DB read SHALL occur.
5. THE cross-session path SHALL add NO schema (read-only `AgentDB.get_recent_runs`
   SELECT over `agent_runs`), do bounded work once per plan (≤1 recent-runs query +
   ≤`top_k` step queries — off the 60 Hz path, AGENTS.md #2), load no new model
   (#6), and degrade to `""` on any error (never raise into planning).

---

## 4. Technical Design

- **Entry point / pipeline boundary:** `inference/dev_agent.py
  ::resume_pending_plan` (≈L1936) and `::_plan_and_run_locked` (≈L874, new
  `seed_context` param). No new verb, no gate change.
- **New component:** `inference/working_memory.py` — pure module:

  ```python
  @dataclass
  class WorkingMemory:
      goal: str
      files: list[str]
      notes: list[str]
      last_failure: str | None

  def summarize_run(goal: str, steps: list[dict], *, max_files: int = 8,
                    max_notes: int = 5, max_chars: int = 1500) -> WorkingMemory: ...

  def render_seed(mem: WorkingMemory) -> str:
      """Deterministic, bounded text block for prompt injection. '' if empty."""
  ```

- **Step loading:** `resume_pending_plan` needs the run's steps. If `AgentDB` lacks
  a `get_steps_for_run(run_id)` reader, add a thin read-only SELECT over
  `agent_steps` (read path only — no schema change). This is the only DB touch.
- **Threading:** `resume_pending_plan` → `summarize_run` → `render_seed` →
  `plan_and_run(goal, seed_context=block)` → prepended to `extra_ctx` in
  `_plan_and_run_locked`, flag-gated on `DA_RESUME_MEMORY`.
- **New `Command`/`AgentStep` fields:** none.
- **Models / VRAM:** none (AGENTS.md #6 unaffected).
- **Persistence:** **none** — derived from existing `agent_steps`; no migration,
  no `user_version` bump (AGENTS.md #1).
- **Cross-platform:** none (AGENTS.md #3 N/A).

### Deferred alternative (noted, not built)

A persisted `agent_working_memory` table updated incrementally during a run (the
reference's eager model) would survive even partial step-persistence and avoid the
resume-time scan. It is **out of scope** for v1: it is an additive schema change
(v8 → v9 migration + `user_version` bump) for a marginal gain over deriving from
`agent_steps`, which is already durable. Revisit only if step-persistence proves
lossy under crash.

### Configuration (flat YAML)

```yaml
resume_memory:
  enabled: true           # env DA_RESUME_MEMORY; default ON (crash-resume, R3.4)
  max_files: 8            # most-recent distinct paths touched
  max_notes: 5            # most-recent result snippets
  max_chars: 1500         # cap on the rendered crash-resume seed block

session_memory:
  enabled: false          # env DA_SESSION_MEMORY; default OFF until verified (R4.4)
  recent_runs: 20         # how many recent runs to scan for relevance
  top_k: 3                # most-relevant prior runs to seed from
  min_score: 0.2          # Jaccard threshold a prior goal must clear
  max_chars: 2000         # cap on the rendered cross-session seed block
```

---

## 5. Behavior Verification (executable, not prose)

- **Unit tests:** `tests/test_resume_working_memory.py`, one assertion per
  criterion:
  - `test_r1_1_derives_files_notes_failure_from_steps`
  - `test_r1_2_last_failure_preserved`
  - `test_r1_3_bounded_and_ordered`
  - `test_r2_2_empty_seed_is_byte_identical` (golden: `extra_ctx` unchanged)
  - `test_r3_2_no_steps_returns_empty_and_resumes`
  - `test_r3_1_no_schema_change` (assert `user_version` unchanged; introspect that
    no new table is created)
- **Integration test (5b, crash-resume):** persist a run with a failing step, call
  `resume_pending_plan` with the flag on (mock voice-confirm → yes), assert the
  resumed `plan_and_run` received a `seed_context` naming the touched file and the
  failure. With the flag off, assert it was called with the bare goal (regression
  guard) — `test_5b_resume_seeds_plan_when_enabled` / `test_5b_resume_bare_goal_when_disabled`.
- **Cross-session tests (R4):** pure-helper tests for `score_relevance` /
  `select_related_runs` / `render_session_seed` / `session_memory_enabled`;
  `_session_seed_context` tests for enabled/disabled/no-related/db-error; and an
  end-to-end `test_r4_plan_context_carries_session_seed` asserting the planner
  context carries `<prior-session-memory>` when on and is byte-identical when off.

Each acceptance criterion in §3 maps to ≥1 test above.

---

## 6. Tasks

- [x] 1. Add `inference/working_memory.py` (`WorkingMemory`, `summarize_run`,
      `render_seed`) — R1.
- [x] 2. Add read-only `AgentDB.get_steps_for_run(run_id)` if absent (no schema
      change) — R2.1, R3.1.
- [x] 3. Add `seed_context` param to `plan_and_run`/`_plan_and_run_locked`; prepend
      to `extra_ctx` — R2.2.
- [x] 4. Wire `resume_pending_plan` to derive + seed, flag-gated (`DA_RESUME_MEMORY`),
      leaving the voice-confirm/rollback gate untouched — R2.1, R3.3, R3.4.
- [x] 5. `tests/test_resume_working_memory.py` + resume integration test (5b) — R1–R3.
- [x] 6. **DECISION (Brad, 2026-06-26):** `DA_RESUME_MEMORY` default **ON** —
      integration test 5b confirms the flag-off path is byte-identical.
- [x] 7. Update `CLAUDE.md` Known Gotchas (both flags) + note the deferred
      `agent_working_memory` table alternative.
- [x] 8. **Cross-session (R4):** add `score_relevance`/`select_related_runs`/
      `render_session_seed`/`session_memory_enabled` + read-only
      `AgentDB.get_recent_runs` + `DevAgent._session_seed_context` wired into
      `_plan_and_run_locked` (fires only when no caller seed), flag-gated
      (`DA_SESSION_MEMORY`, default OFF) — R4.
- [~] 9. **DECISION (Brad):** flip `DA_SESSION_MEMORY` default ON after validating
      relevance quality on real back-to-back related runs. **Validation harness
      added** (`scripts/validate_session_memory.py`) — mirrors
      `DevAgent._session_seed_context` exactly (same `get_recent_runs(limit=20)` /
      `select_related_runs(top_k=3, min_score=0.2)` / `summarize_run` /
      `render_session_seed`), runs read-only over the live `agent.db`, reports the
      scored candidate field + the exact `<prior-session-memory>` block per
      new-goal scenario. **First run (2026-06-28): precondition UNMET → keep OFF.**
      (1) The live DB has 380 terminal runs but **0** with persisted multi-step
      file-touching trajectories (367/380 are `general`-domain accessibility/chat;
      the only 3 step-bearing rows are misattributed CLICKs with `args=None`), so
      every session seed derives to empty — the feature is a no-op against this
      history. (2) The `--synthetic` reference run exposed a relevance-scorer
      weakness worth fixing *before* the flip: a same-file follow-up
      ("finish the fix in core/poller.py" vs prior "fix the timeout bug in
      core/poller.py") scored **0.12 < `min_score` 0.2** and was rejected —
      `score_relevance`'s Jaccard-over-content-words dilutes shared file-path
      tokens when goal wording differs. **Next:** accumulate real related
      multi-file DevAgent runs, re-run `--from-db`; consider weighting path tokens
      in `score_relevance` (or lowering `min_score`) before flipping ON.
