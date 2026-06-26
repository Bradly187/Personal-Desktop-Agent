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

**Status:** In Progress — `inference/working_memory.py`
(`WorkingMemory`/`summarize_run`/`render_seed`/`memory_enabled`) + read-only
`AgentDB.get_steps_for_run` (no schema change) + `seed_context` param on
`plan_and_run`/`_plan_and_run_locked` + `DevAgent._resume_seed_context` wired into
`resume_pending_plan` behind `DA_RESUME_MEMORY`; 9 unit tests green. Integration
test (task 5b) pending.
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
4. THE feature SHALL be controlled by `DA_RESUME_MEMORY`, default **off** until
   verified; WHILE off, `resume_pending_plan` SHALL call `plan_and_run(goal)`
   exactly as today.

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
  enabled: false          # env DA_RESUME_MEMORY; default off until verified
  max_files: 8            # most-recent distinct paths touched
  max_notes: 5            # most-recent result snippets
  max_chars: 1500         # cap on the rendered seed block
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
- **Integration test:** extend the existing resume test path — persist a run with a
  failing step, call `resume_pending_plan` with the flag on (mock voice-confirm
  → yes), assert the resumed `plan_and_run` received a `seed_context` naming the
  touched file and the failure. With the flag off, assert it was called with the
  bare goal (regression guard).

Each acceptance criterion in §3 maps to ≥1 test above.

---

## 6. Tasks

- [ ] 1. Add `inference/working_memory.py` (`WorkingMemory`, `summarize_run`,
      `render_seed`) — R1.
- [ ] 2. Add read-only `AgentDB.get_steps_for_run(run_id)` if absent (no schema
      change) — R2.1, R3.1.
- [ ] 3. Add `seed_context` param to `plan_and_run`/`_plan_and_run_locked`; prepend
      to `extra_ctx` — R2.2.
- [ ] 4. Wire `resume_pending_plan` to derive + seed, flag-gated (`DA_RESUME_MEMORY`),
      leaving the voice-confirm/rollback gate untouched — R2.1, R3.3, R3.4.
- [ ] 5. `tests/test_resume_working_memory.py` + resume integration test — R1–R3.
- [ ] 6. **DECISION (Brad):** flip `DA_RESUME_MEMORY` default on after the
      integration test confirms no regression.
- [ ] 7. Update `CLAUDE.md` Known Gotchas (new flag) + note the deferred
      `agent_working_memory` table alternative.
