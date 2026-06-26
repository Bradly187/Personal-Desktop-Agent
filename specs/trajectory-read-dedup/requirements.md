# Spec: Trajectory Read-Deduplication (Gap B)

> One feature → one folder. This spec is the source of truth; the code is the
> build artifact. Design and Tasks are kept inline (§4–§6) until they outgrow
> the file. Keep this updated as the design evolves.

---

## 1. Background — the "Why"

Gap analysis (2026-06-26) against `rasbt/mini-coding-agent` found that the
reference agent does one token-saving trick PDA does not: in `history_text()` it
**deduplicates repeated reads** — an older `read_file` for a path already seen is
dropped from the re-sent history, and the seen-set is **cleared on writes/patches**
so a read *after* an edit is always kept (the file changed, so the stale read is
no longer the truth).

PDA re-sends the executed trajectory on every `_replan`/`_try_replan` cycle
(`MAX_REPLANS = 2`) plus the final `_reflect` — so a long plan re-serializes most
of its history three-plus times. The compaction component
(`inference/trajectory.py::render_trajectory`) exists, but it is held **OFF by
decision** (`DA_TRAJECTORY_REDUCE`, see `../trajectory-reduction/`) because
abstracting steps caused a reproducible ~12.5pt recovery-*ordering* regression on
one long-prefix shape.

Read-dedup is a **strictly safer subset** of that work: it removes only
*superseded, duplicate, read-only observations* — it never abstracts a result, never
reorders steps, and never touches a failure or a step inside the verbatim window.
So it can ship on its **own flag**, independent of the reduction hold, and is the
"safe deterministic win that doesn't touch failure-signal ordering" called out in
the gap analysis.

**Status:** In Progress — `_dedup_reads`/`_stub_line` + `dedup_enabled()` in
`inference/trajectory.py`, wired through `render_trajectory`/`build_replan_prompt`/
`_reflect` behind `DA_TRAJECTORY_DEDUP` (independent of `DA_TRAJECTORY_REDUCE`);
10 unit tests green (compose-with-reduce + byte-identical-when-off verified).
Note: only filesystem-mutating verbs (WRITE_FILE/EDIT_FILE/RUN_TERMINAL/GIT_CHECKOUT)
clear the seen-set — EXPLAIN/desktop verbs are dedup no-ops. Eval baseline (task 4)
pending.
**Owner / author session:** Claude Code (Opus 4.8)
**Related:** `../trajectory-reduction/` (same module, complementary; this is the
conservative half held back from that spec's ordering risk),
`../accessibility-agent/` (DevAgent). Honors AGENTS.md #4 (never lose recovery
signal — failures exempt).

---

## 2. Glossary

- **Read-only verb**: a verb in `DevAgent._PARALLEL_VERBS` (`inference/dev_agent.py`
  ≈L416 — READ_FILE, GREP, FETCH_URL, READ_SCREEN, GIT_STATUS, GIT_DIFF,
  SEARCH_PERSONAL) — idempotent, no side effects.
- **Write/mutating verb**: anything not in `_PARALLEL_VERBS` that changes state
  (WRITE_FILE, EDIT_FILE, RUN_TERMINAL, OPEN, HOTKEY, …). A write **invalidates**
  the dedup memory for affected targets.
- **Read target**: a normalized key identifying *what* a read-only step observed —
  for READ_FILE the resolved path; for GREP the `(pattern, path)` pair; for a
  pathless read (READ_SCREEN) the verb name itself.
- **Verbatim window** (`keep_verbatim`): the most-recent N steps `render_trajectory`
  always renders in full — never deduped (a recent re-read is intentional signal).
- **Superseded read**: an OLDER read-only step whose target is read again later, or
  whose target is written after it — its result no longer reflects current state.

---

## 3. Requirements (EARS acceptance criteria)

### Requirement 1: Deduplicate superseded older reads

**User Story:** As Brad, I want the agent to stop re-sending stale duplicate file
reads in its replan prompt, so long plans replan faster and cost less, without
losing any decision-relevant observation.

#### Acceptance Criteria
1. WHEN two or more OLDER (outside the verbatim window) read-only steps share a
   read target, THE dedup pass SHALL keep the **last** occurrence's full result and
   collapse each EARLIER occurrence to a one-line stub
   (`N. [ok] READ_FILE foo.py → (superseded by later read)`).
2. THE dedup pass SHALL operate only on read-only verbs (`_PARALLEL_VERBS`);
   write/mutating steps SHALL always render unchanged.
3. THE most-recent read of a target SHALL always survive with its result (it
   usually carries the decisive diagnostic the agent acted on).

### Requirement 2: A write invalidates prior reads of its target

#### Acceptance Criteria
1. WHEN a write/mutating step targets a path, THE dedup pass SHALL clear that
   target from the seen-set so a read occurring AFTER the write is never treated as
   a duplicate of a read BEFORE it (the file changed — both reads are real signal).
2. WHERE a write target cannot be resolved to a specific path (e.g. RUN_TERMINAL),
   THE dedup pass SHALL conservatively clear the ENTIRE seen-set (assume anything
   may have changed) rather than risk dropping a now-stale read.

### Requirement 3: Failures and recent steps are exempt

#### Acceptance Criteria
1. FOR ALL steps with `success is False`, THE dedup pass SHALL NOT collapse the
   step — a failed read's error is recovery signal (AGENTS.md #4), exempt from R1.
2. THE dedup pass SHALL NOT touch any step inside the verbatim window
   (`keep_verbatim`) — those render in full regardless of duplication.
3. THE dedup pass SHALL preserve step ORDER and SHALL NOT abstract any surviving
   result (this is the property that distinguishes it from `DA_TRAJECTORY_REDUCE`
   and keeps it free of that spec's ordering risk).

### Requirement 4: Independent flag, byte-identical when off, deterministic

#### Acceptance Criteria
1. THE feature SHALL be controlled by `DA_TRAJECTORY_DEDUP`, default **off** until
   the eval baseline (§5) is recorded; WHILE off, `render_trajectory` output SHALL
   be byte-identical to today.
2. `DA_TRAJECTORY_DEDUP` SHALL be independent of `DA_TRAJECTORY_REDUCE` — dedup MAY
   be on while reduction stays off (the two compose; dedup runs as a pre-pass over
   the step list, reduction (if on) abstracts what survives).
3. THE dedup pass SHALL be deterministic (identical input → identical output), make
   no LLM call, perform no I/O, and NOT mutate any `AgentStep` (the durable ledger
   and saga `comp_args` must see original state — AGENTS.md, trajectory-reduction R3.3).
4. FOR ALL surviving lines, file paths in `args` SHALL remain intact (never
   truncated mid-string).

---

## 4. Technical Design

- **Entry point / pipeline boundary:** `inference/trajectory.py::render_trajectory`
  — add a dedup pre-pass over `steps` before the existing rendering. Callers
  (`DevAgent.build_replan_prompt` ≈L1405, `_reflect` ≈L2313) are unchanged except
  for passing the new flag through (they already pass `readonly_verbs` and
  `enabled`).
- **New surface (same module):**

  ```python
  def _dedup_reads(
      steps: list[AgentStep],
      *,
      keep_verbatim: int,
      readonly_verbs: frozenset[str],
  ) -> list[tuple[AgentStep, bool]]:
      """Return [(step, collapsed)] in original order. collapsed=True marks an
      older read-only step whose target was later re-read or written — render it
      as a one-line stub. Failures and verbatim-window steps are never collapsed.
      Pure; no mutation."""
  ```

  `render_trajectory` gains `dedup_reads: bool = False` (wired to
  `DA_TRAJECTORY_DEDUP`); when true it runs `_dedup_reads` first and renders a
  collapsed step as the R1.1 stub instead of its full body.
- **Target extraction:** a small `_read_target(step)` helper — READ_FILE → resolved
  path from `args`; GREP → `(pattern, path)`; pathless reads → verb name. A
  `_write_target(step)` helper returns the path a write touches, or `None`
  (→ clear-all, R2.2).
- **New `Command`/`AgentStep` fields:** none. Read-only over existing fields.
- **Models / VRAM:** none. Pure prompt shrink (AGENTS.md #6 unaffected).
- **Persistence:** none. No `agent.db` schema change, **no `PRAGMA user_version`
  bump** (AGENTS.md #1).
- **Cross-platform:** none (AGENTS.md #3 N/A).

### Configuration (flat YAML)

```yaml
trajectory_dedup:
  enabled: false          # env DA_TRAJECTORY_DEDUP; independent of DA_TRAJECTORY_REDUCE
  # keep_verbatim is shared with trajectory_reduction (same render_trajectory call)
```

### Why this can flip on when reduction can't

`../trajectory-reduction/` is held OFF because *abstraction* changed recovery
ordering on one shape. Dedup never abstracts a surviving result and never reorders
— it only removes provably-superseded duplicates and always keeps the latest read.
So its eval risk profile is different: the expectation is the `dev_replan` gate
holds at 100% with a real token saving, which (unlike reduction) would justify
flipping the default on. That is the decision §6 task 5 records.

---

## 5. Behavior Verification (executable, not prose)

- **Unit tests:** `tests/test_trajectory_dedup.py`, one assertion per criterion:
  - `test_r1_1_older_duplicate_read_collapsed_last_kept`
  - `test_r1_2_writes_never_collapsed`
  - `test_r2_1_read_after_write_not_deduped`
  - `test_r2_2_unresolvable_write_clears_all`
  - `test_r3_1_failed_read_exempt`
  - `test_r3_2_verbatim_window_untouched`
  - `test_r3_3_does_not_mutate_steps`
  - `test_r4_1_disabled_is_byte_identical` (golden vs current `render_trajectory`)
  - `test_r4_2_composes_with_reduce` (dedup on + reduce on; dedup on + reduce off)
- **Eval suite:** reuse `evals/suites/dev_replan.jsonl` (the replan mode added by
  `../trajectory-reduction/`); add ≥2 cases with repeated reads of the same file
  across a long prefix. Lock `evals/baselines/dev_replan.json` with the flag ON;
  the gate (≥85%, `safe_acc=100%`) SHALL hold. Record `chars_saved` for the daily
  review.

Each acceptance criterion in §3 maps to ≥1 test or eval case above.

---

## 6. Tasks

- [x] 1. Add `_read_key`/`_write_clear_key`/`_dedup_reads`/`_stub_line` to
      `inference/trajectory.py` — R1, R2, R3.
- [x] 2. Wire `dedup_reads` param + `dedup_enabled()`/`DA_TRAJECTORY_DEDUP` through
      `render_trajectory`, `build_replan_prompt`, `_reflect` — R4.1, R4.2.
- [x] 3. `tests/test_trajectory_dedup.py` (10 tests) + byte-identical-when-off +
      compose-with-reduce — R4.
- [~] 4. Eval cases ADDED — `replan-dedup-reread-config` + `replan-dedup-reread-schema`
      in `dev_replan.jsonl` (both verified model-free to engage dedup: superseded read
      stubbed, decisive re-read kept, chars saved). `_run_replan` logs the dedup flag.
      Baseline lock with `DA_TRAJECTORY_DEDUP=1` pending a model run — §5.
- [ ] 5. **DECISION (Brad):** flip `DA_TRAJECTORY_DEDUP` default on if the gate
      holds (expected, unlike reduction). Record token delta in `docs/daily/`.
- [ ] 6. Update `CLAUDE.md` Known Gotchas (new flag, and its independence from
      `DA_TRAJECTORY_REDUCE`).
