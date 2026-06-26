# Handoff — mini-coding-agent gap closure (Gaps A–D)

**Date:** 2026-06-26
**Author session:** Claude Code (Opus 4.8)
**Status:** Implemented + unit-tested + eval cases drafted. **Uncommitted** on the
working tree — PRs are Brad's to open. Baselines + default-flips pending a model run.

---

## 1. Origin

Gap analysis of the PDA dev-agent against `rasbt/mini-coding-agent`'s six
"coding-harness hygiene" components (live repo context, prompt-cache reuse,
structured tools, context reduction, transcripts/resumption, subagents). Verdict:
PDA is *ahead* on tools/validation and durability, but thinner on four axes →
Gaps **A/B/C/D**. Four specs were written, then implemented this session.

Full analysis + spec rationale: the four `specs/<slug>/requirements.md` folders
(each has §1 Background). Memory: `mini-coding-agent-gap-2026-06-26.md`.

---

## 2. What shipped (all behind default-OFF flags; byte-identical when off)

| Gap | Spec | Flag | One-line |
|-----|------|------|----------|
| **A** Repo-context ingestion | `specs/repo-context-ingestion/` | `DA_REPO_CONTEXT` | Build stable repo facts (AGENTS.md/CLAUDE.md/git/layout) once, inject into the plan prompt |
| **B** Trajectory read-dedup | `specs/trajectory-read-dedup/` | `DA_TRAJECTORY_DEDUP` | Drop superseded duplicate reads from the re-sent trajectory; independent of the held-OFF reduction flag |
| **C** Resume working-memory | `specs/resume-working-memory/` | `DA_RESUME_MEMORY` | On resume, derive a compact `{files,notes,last_failure}` from `agent_steps` and seed the replan |
| **D** Planner DELEGATE verb | `specs/dev-agent-delegate-verb/` | `DA_DELEGATE` | `[DELEGATE <q>]` spins a bounded read-only investigation sub-agent; result returns into the trajectory |

### Per-gap detail

**Gap A** — `inference/workspace_context.py::build_workspace_context()` (pure,
reads only inside repo_root, degrades on any failure). `DevAgent._workspace_context()`
memoizes it (built once) + `invalidate_workspace_context()`. Prepended to `extra_ctx`
in `_plan_and_run_locked` ahead of dynamic RAG/git.
- **Deferred:** lifting the block into a dedicated cloud `cache_control` *breakpoint*
  (it rides the existing cacheable prefix; the cloud cache is a no-op below the
  2048-token min anyway). Tracked as task 3 `[~]` in the spec.

**Gap B** — `inference/trajectory.py::_dedup_reads()` + `_stub_line()` +
`dedup_enabled()`. A pre-pass over the step list: keep-last among reads of the same
target within a write-bounded segment; a write to a path clears just that target,
an unresolvable write (RUN_TERMINAL/GIT_CHECKOUT) clears all. Wired through
`render_trajectory(dedup_reads=…)`, `build_replan_prompt`, `_reflect`.
- **Key correctness note:** only filesystem-mutating verbs
  (WRITE_FILE/EDIT_FILE/RUN_TERMINAL/GIT_CHECKOUT) clear the seen-set — EXPLAIN and
  desktop verbs are dedup no-ops (a first cut wrongly treated EXPLAIN as a write).
- **Composes** with `DA_TRAJECTORY_REDUCE` independently; never abstracts/reorders,
  so it sidesteps reduction's ~12.5pt ordering risk → expected to flip ON cleanly.

**Gap C** — `inference/working_memory.py` (`WorkingMemory`, `summarize_run`,
`render_seed`, `memory_enabled`). New read-only `AgentDB.get_steps_for_run(run_id)`
(SELECT over existing `agent_steps` — **no schema change, no user_version bump**).
`plan_and_run`/`_plan_and_run_locked` gained an optional `seed_context` param;
`DevAgent._resume_seed_context()` derives + seeds it in `resume_pending_plan`. The
voice-confirm/rollback gate is untouched.

**Gap D** — `DELEGATE` verb registered across `_PLAN_ACTIONS`, `_STEP_PATTERN`,
`model_router._PLAN_VERBS` (JSON-schema enum), `_RETRYABLE_VERBS`. New
`DevAgent._delegate_investigate()` / `_delegate_loop()` / `_journal_delegate()`.
`_execute_step` gained a `DELEGATE` branch. Teaching (`_DELEGATE_PROMPT_INSTRUCTIONS`)
injected only when on **and** at depth 0.
- **Complements PR #137** (`WorkflowRunner`, merged): #137 is breadth (voice,
  tool-less fan-out); Gap D is depth (planner, read-only *tools*, result into the
  plan loop). Reuses #137's substrate — journals to `agent_workflows`
  (`mode="delegate"`), runs under the scheduler sub-agent pool when wired.
- **Two hard invariants:** (1) the child never re-enters `plan_and_run`/`_plan_lock`
  (would deadlock — same reason #137 used fresh-context); (2) the child is
  *structurally* read-only — verb allowlist = `_PARALLEL_VERBS`, any non-read-only
  child step is **dropped, not gated**, so there is nothing to approve and no
  destructive path. Depth-capped at `MAX_DELEGATE_DEPTH=1` (no recursion).
- **Design refinement vs spec R1.3:** DELEGATE is in `_RETRYABLE_VERBS` only, NOT
  `_PARALLEL_VERBS` (that would union into `_FANOUT_SAFE_VERBS` and let the parent
  fan out delegations concurrently). It runs sequentially. Documented in the spec.

---

## 3. Tests

40 new unit tests (1 skips on Windows without symlink privilege), all green:
`tests/test_workspace_context.py` (9), `tests/test_trajectory_dedup.py` (10),
`tests/test_resume_working_memory.py` (9), `tests/test_delegate_verb.py` (12).

Broad regression at implementation time: **601 passed, 4 failed → all 4 fixed**
(see §5). Focused re-run after fixes: **197 passed, 1 skipped.**

---

## 4. Eval cases (drafted; baselines NOT locked)

Prompt-only (inject via the `TrajectoryCase.context` hook that `plan_predictor` uses):
- `evals/suites/dev_replan.jsonl` **+2** (`replan-dedup-reread-{config,schema}`) —
  verified model-free that dedup engages (superseded read stubbed, decisive re-read
  kept, 57/93 chars saved). `_run_replan` now logs the dedup flag.
- `evals/suites/repo_context.jsonl` **(new, 3)** — fixture `<workspace-context>`
  block carrying a house rule; scored `--mode trajectory`.
- `evals/suites/dev_delegate.jsonl` **(new, 3)** — 2 expect DELEGATE, 1 forbids it
  (anti-overuse); scored `DA_DELEGATE=1 … --mode trajectory`.

End-to-end (run the LIVE `plan_and_run`, real injection; read-only only because a
destructive plan fail-safe-DENYs unattended):
- `evals/suites/repo_context_exec.jsonl` **(new, 3)** — real `build_workspace_context`
  injects the actual AGENTS.md/CLAUDE.md. `DA_REPO_CONTEXT=1 … --mode execution`.
- `evals/suites/dev_delegate_exec.jsonl` **(new, 3)** — a real DELEGATE runs
  `_delegate_investigate` against the repo. `DA_DELEGATE=1 … --mode execution`.

`evals/trajectory._PLAN_ACTIONS` (the scorer's mirror) gained `DELEGATE`; the verb
extractor recognizes it (verified).

**Caveat baked into `repo_context_exec` header:** exec verb-scoring gates safety /
read-only discipline, NOT answer groundedness — the real Gap A payoff (does the
EXPLAIN cite the injected rules?) wants a judge eval (future, `explain_quality`-style).

### Lock commands (need a real `qwen3-coder:30b` via Ollama)
```bash
DA_TRAJECTORY_DEDUP=1 python -m evals.run --suite dev_replan        --mode replan      --check
                      python -m evals.run --suite repo_context       --mode trajectory  --check
DA_DELEGATE=1         python -m evals.run --suite dev_delegate       --mode trajectory  --check
DA_REPO_CONTEXT=1     python -m evals.run --suite repo_context_exec  --mode execution   --check
DA_DELEGATE=1         python -m evals.run --suite dev_delegate_exec  --mode execution   --check
```
First run with no baseline locks it; re-run with `--check` to gate. For A/B on Gap A,
also run the `repo_context_exec` suite with `DA_REPO_CONTEXT=0` and compare.

---

## 5. One pre-existing bug fixed (attributed, not from this work)

`tests/test_db_schema.py` expected **48** tables but PR #137's merged `agent_workflows`
made the live count **49** (CLAUDE.md already read 49 — only the test constant lagged).
Bumped `_EXPECTED_TABLE_COUNT` 48→49 with an attributing comment (AGENTS.md #1). My
`storage/db.py` change is read-only (the `get_steps_for_run` method; no `CREATE TABLE`).

---

## 6. File manifest (for the PRs)

**New modules/code**
- `inference/workspace_context.py` (Gap A)
- `inference/working_memory.py` (Gap C)

**Modified**
- `inference/dev_agent.py` (+305) — A: workspace ctx; B: dedup wiring; C: seed_context
  + `_resume_seed_context`; D: DELEGATE verb + `_delegate_*`
- `inference/trajectory.py` (+118) — B: `_dedup_reads`/`_stub_line`/`dedup_enabled`
- `inference/model_router.py` (+1) — D: `DELEGATE` in `_PLAN_VERBS` enum
- `storage/db.py` (+18) — C: `get_steps_for_run` (read-only)
- `evals/run.py`, `evals/trajectory.py` — eval wiring (dedup/exec flag logs, scorer mirror)
- `evals/suites/dev_replan.jsonl` (+2 cases)
- `tests/test_db_schema.py`, `tests/test_goal_queue.py` — test truth-ups (see §5 / signature)

**New tests** — `tests/test_{workspace_context,trajectory_dedup,resume_working_memory,delegate_verb}.py`

**New eval suites** — `evals/suites/{repo_context,repo_context_exec,dev_delegate,dev_delegate_exec}.jsonl`

**New specs** — `specs/{repo-context-ingestion,trajectory-read-dedup,resume-working-memory,dev-agent-delegate-verb}/requirements.md`

### Suggested PR breakdown (one feature per PR, mirrors prior gap-closure cadence)
1. **Gap A** — `workspace_context.py` + dev_agent wiring + test + `repo_context*` suites + spec.
2. **Gap B** — `trajectory.py` dedup + dev_agent wiring + test + `dev_replan` cases + eval log + spec.
3. **Gap C** — `working_memory.py` + `db.get_steps_for_run` + seed wiring + test + spec.
4. **Gap D** — DELEGATE verb across dev_agent/model_router + `_delegate_*` + test + `dev_delegate*` suites + scorer mirror + spec.
- The `test_db_schema.py` truth-up (§5) can ride with whichever PR lands first, or its own tiny PR.

---

## 7. Open items (Brad's call)

1. **Lock the 5 baselines** (§4 commands) on a box with Ollama.
2. **Default-flip decisions** per spec (each has a `[ ] DECISION (Brad)` task). Gap B
   is the strongest flip candidate (no ordering risk). The others wait on their gates.
3. **Gap A cloud-cache breakpoint** (spec task 3) — optional perf refinement, currently
   a no-op below the cache min.
4. **Gap A groundedness judge eval** — the honest measure of repo-context value.
5. **Update `CLAUDE.md` Known Gotchas** with the four new flags when these merge
   (each spec's last task notes this).

---

## 8. How to sanity-check locally (no model needed)

```bash
python -m pytest tests/test_workspace_context.py tests/test_trajectory_dedup.py \
  tests/test_resume_working_memory.py tests/test_delegate_verb.py tests/test_db_schema.py \
  tests/test_goal_queue.py -q
# Suites load + DELEGATE extraction + dedup engagement were verified model-free this session.
```
