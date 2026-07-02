# Spec: DevAgent Trajectory Reduction (token economics)

> One feature → one folder. This spec is the source of truth; the code is the
> build artifact. Design and Tasks are kept inline (§4–§6) until they outgrow
> the file. Keep this updated as the design evolves.

---

## 1. Background — the "Why"

`DevAgent.plan_and_run` runs a plan → execute → observe → reflect loop. Every
time a step fails, `_replan`/`_try_replan` re-send the **entire** executed-step
trajectory (action + args + truncated result for each completed step) back to
the plan-domain model so it can recover; `_reflect` does the same once more at
the end. The trajectory grows linearly with `len(executed)` (bounded only by
`MAX_STEPS = 20`), and it is re-serialized on every `_replan` cycle
(`MAX_REPLANS = 2`) plus the final reflect — so a long plan re-sends most of its
history three-plus times.

This is the project's instance of the well-documented agent token sink: *"tool
calls and results are kept in the trajectory until task completion, causing
computational costs to snowball"* — the input side dominates agent token spend
([Trajectory Reduction for LLM Agents, arXiv 2509.23586](https://arxiv.org/pdf/2509.23586)).
The plan domain runs locally (`qwen3-coder:30b`, thinking ON), so the immediate
win is **lower prompt-processing latency and a smaller KV-cache footprint on the
RTX 5090**; when a run escalates to the cloud (`CloudDevAgent` → Bedrock/Opus),
the same reduction is **real Bedrock spend avoided** (priced in
`monitoring/cost_ledger.py`).

The fix: synthesize the trajectory before re-feeding it — keep recent steps
verbatim, abstract older ones, drop superseded read-only observations, and
always preserve failure signal. This is the *abstractive-memory / "keep recent
verbatim, summarize old"* pattern (MemGPT; [Memory for Autonomous LLM Agents,
arXiv 2603.07670](https://arxiv.org/html/2603.07670v1)) applied at the replan
boundary, done **deterministically** so it adds no inference cost of its own.

**Status:** In Progress — compactor + wiring + unit tests + replan eval all landed;
baseline locked. **Default stays OFF pending a flip decision:** the `dev_replan` gate
passes (87.5% ≥ 85% threshold) and safety is fully preserved (safe_acc 100%), but
compaction causes a *reproducible* 1-case recovery-ordering regression (OFF 100%, ON
87.5%, 3 runs each) — see §5. Flipping the default (task 7) is a trade-off call for Brad.
**Owner / author session:** Claude Code (Opus 4.8)
**Related:** `../accessibility-agent/` (DevAgent lives above HybridCoordinator);
observability that measures this lands via the logging + span-cost recs (Rec #1, #2).

---

## 2. Glossary

- **Trajectory**: the ordered `list[AgentStep]` of already-executed steps
  (`executed`) that `_replan`/`_try_replan`/`_reflect` serialize into a prompt as
  the observation signal for the model.
- **AgentStep**: dataclass in `inference/dev_agent.py` — `action`, `args`,
  `body`, `result`, `success`, `latency_ms`, `deps`, `comp_args`.
- **TrajectoryCompactor**: the new deterministic component this spec introduces.
  Pure function over `list[AgentStep]` → a token-reduced text rendering. No LLM
  call, no I/O, no mutation of the input steps.
- **Verbatim window** (`keep_verbatim`): the count of most-recent steps rendered
  in full; older steps are abstracted.
- **Failure signal**: the `result`/error text of any step with `success is
  False`. Always preserved (never abstracted away) — it is the entire reason the
  trajectory is re-sent.
- **Read-only verb**: a verb in `DevAgent._PARALLEL_VERBS` (READ_FILE, GREP,
  FETCH_URL, READ_SCREEN, GIT_STATUS, GIT_DIFF, SEARCH_PERSONAL) — idempotent,
  no side effects.
- **Critical token**: a substring that must survive compaction intact because
  dropping it breaks recovery — file paths, the failed verb, and error text.
  (Prompt-compression research shows naive compressors drop these low-perplexity
  but load-bearing tokens — [arXiv 2503.19114](https://arxiv.org/pdf/2503.19114).)

---

## 3. Requirements (EARS acceptance criteria)

### Requirement 1: Deterministic trajectory compaction

**User Story:** As Brad, I want the agent to compress its own step history before
asking the model to recover from a failure, so that long plans replan faster
locally and cost less when they escalate to the cloud — without me losing recovery
reliability.

#### Acceptance Criteria
1. THE `TrajectoryCompactor` SHALL render a `list[AgentStep]` to text
   deterministically — identical input yields byte-identical output, with no LLM
   call and no I/O.
2. THE `TrajectoryCompactor` SHALL render the most recent `keep_verbatim` steps
   (default 3) in full (current `_replan` fidelity: `action`, `args[:60]`,
   `result[:300]`).
3. WHEN the trajectory has more than `keep_verbatim` steps, THE
   `TrajectoryCompactor` SHALL abstract each older step to a single line of the
   form `N. [ok|FAILED] ACTION args → <≤80-char outcome>`.
4. FOR ALL steps with `success is False`, THE `TrajectoryCompactor` SHALL
   preserve the full failure-signal snippet (`result[:300]`) regardless of the
   step's age in the trajectory.
5. WHEN two or more consecutive older read-only steps succeeded, THE
   `TrajectoryCompactor` SHALL collapse the HEAD of that run into one summary line
   naming the targets (e.g. `1–4. [ok] 4 read-only steps: foo.py, bar.py, …`) but
   SHALL keep the **last** step of the run VERBATIM (with its full result). The
   most-recent read usually carries the decisive diagnostic the agent acted on, so
   collapsing it down to a filename loses recovery-relevant signal.
   <!-- Refined after the dev_replan eval (§5): collapsing the whole run stripped
        diagnostic results to filenames. Keeping the last read verbatim preserves it.
        (Did not eliminate the build-missing-module ordering miss — see §5 — but is
        the right design and cheap.) -->
6. WHEN exactly one older read-only step would be collapsed (a head of length 1),
   THE `TrajectoryCompactor` SHALL render it as a plain one-line abstract rather
   than a "1 read-only steps" summary.
7. FOR ALL rendered steps, THE `TrajectoryCompactor` SHALL keep every file path
   appearing in `args` intact (critical-token preservation) — paths are never
   truncated mid-string even inside an abstracted line.

### Requirement 2: Wiring into the replan / reflect boundary

**User Story:** As Brad, I want this reduction applied exactly where the
trajectory is re-sent, with the existing recovery behavior unchanged.

#### Acceptance Criteria
1. THE `_replan` and `_try_replan` paths SHALL build their "steps already
   executed (with outcomes)" block via the `TrajectoryCompactor` instead of the
   inline per-step loop.
2. THE `_reflect` path SHALL build its "steps executed" block via the same
   `TrajectoryCompactor` (its 200/600-char success/failure budget is expressed as
   compactor parameters, not a second copy of the loop).
3. THE compacted trajectory SHALL still contain enough signal that the recovery
   plan quality is **not** measurably worse — the `router_plan_recovery` /
   DevAgent replan eval baseline SHALL hold (no regression in replan-success
   rate).
4. THE feature SHALL be controlled by a single flag `DA_TRAJECTORY_REDUCE`,
   default **off**, until the eval baseline in §5 is locked. WHILE the flag is
   off, `_replan`/`_try_replan`/`_reflect` SHALL produce byte-identical prompts to
   today's code (pure refactor — the compactor in `keep_verbatim=∞` mode
   reproduces the current rendering).

### Requirement 3: Safety & observability

**User Story:** As Brad, I want reduction to never silently swallow a failure or
a destructive side effect, and I want to see how many tokens it saved.

#### Acceptance Criteria
1. IF a step's `success is False`, THEN THE `TrajectoryCompactor` SHALL NOT
   abstract or drop its failure signal — failures are exempt from R1.3/R1.5.
   (Safe-by-default, AGENTS.md #4: never lose the recovery signal.)
2. IF the input trajectory is empty or has `≤ keep_verbatim` steps, THEN THE
   `TrajectoryCompactor` SHALL behave as a pass-through (no abstraction applied).
3. THE compactor SHALL NOT mutate any `AgentStep` it reads (the durable ledger in
   `_persist_step` and the saga `comp_args` must see the original step state).
4. WHEN `DA_TRAJECTORY_REDUCE` is on, THE `_replan` inference span SHALL record
   `traj_steps_in`, `traj_steps_rendered`, and `traj_chars_saved` attrs (via the
   tracer) so `monitoring/replay.py` and the dashboard can show the reduction.
   <!-- depends on Rec #2 token-cost spans; degrade to no-op if tracer disabled -->

---

## 4. Technical Design

- **Entry point / pipeline boundary:** `inference/dev_agent.py` — the
  `_replan` (≈L1152), `_try_replan` (≈L1319), and `_reflect` (≈L1873) methods.
  No new verb, no coordinator/gate change.
- **New component:** `inference/trajectory.py` — pure module, no DevAgent import
  (avoids a cycle). Public surface:

  ```python
  def render_trajectory(
      steps: list[AgentStep],
      *,
      keep_verbatim: int = 3,
      success_chars: int = 300,
      failure_chars: int = 300,
      enabled: bool = True,        # False → legacy full rendering (R2.4)
  ) -> tuple[str, dict]:
      """Return (rendered_text, stats). stats = {steps_in, steps_rendered,
      chars_in, chars_out, chars_saved}. Deterministic; no I/O; no mutation."""
  ```

  `_reflect` calls it with `success_chars=200, failure_chars=600` to match its
  current budget; `_replan` uses the defaults.
- **Read-only verb set:** import `DevAgent._PARALLEL_VERBS` semantics by value —
  duplicate the frozenset in `trajectory.py` as `_READONLY_VERBS` with a comment
  pointing back, OR pass it in. Prefer passing it in from the caller to keep one
  source of truth.
- **New `Command`/`AgentStep` fields:** none. Compactor is read-only over existing
  fields.
- **Models / VRAM:** none added. The point is to *shrink* the prompt the existing
  `plan` profile (`qwen3-coder:30b`) processes; no new model, no `ResourceGovernor`
  change (AGENTS.md #6 unaffected).
- **Persistence:** none. No `agent.db` schema change, **no `PRAGMA user_version`
  bump** (AGENTS.md #1). Stats live only on the in-memory trace span (R3.4),
  which already persists via `command_traces` with no schema change.
- **Cross-platform:** none — does not touch `core/ipad_bridge.py` (AGENTS.md #3
  N/A).

### Configuration (flat YAML)

```yaml
trajectory_reduction:
  enabled: false            # env DA_TRAJECTORY_REDUCE; default off until evals lock
  keep_verbatim: 3          # most-recent steps rendered in full
  success_chars: 300        # per-step result budget for abstracted successes
  failure_chars: 300        # failure signal always preserved at this budget (R3.1)
  collapse_readonly_runs: true   # R1.5 — merge consecutive older read-only successes
```

### Why deterministic (not LLM summarization)

An LLM-summarized trajectory would itself cost an inference per replan — net-negative
for a 3-step plan and non-deterministic to eval. The literature's trajectory-reduction
gains come mostly from *state abstraction + observation filtering + relevance pruning*
([arXiv 2509.23586](https://arxiv.org/pdf/2509.23586)), all of which are rule-expressible
here (recent-verbatim window, drop superseded reads, keep failures). A future
LLM-summarization tier for very long trajectories can slot in behind the same flag if
evals justify it — but it is explicitly **out of scope** for v1.

---

## 5. Behavior Verification (executable, not prose)

- **Eval suite (DONE):** the existing trajectory eval scores only the *initial*
  plan (`goal → verbs`) and never exercises `_replan`, so a new **`replan` mode**
  was added (`evals/run.py --mode replan`): the predictor feeds a failure-state
  case to the production `DevAgent.build_replan_prompt` (extracted as a shared
  helper so the eval scores the *exact* prompt production sends), and
  `DA_TRAJECTORY_REDUCE` toggles whether that prompt is compacted. Suite:
  `evals/suites/dev_replan.jsonl` (8 recovery cases; 3 with >6-step prefixes so
  compaction engages; 2 read-only **safety** cases). Baseline:
  `evals/baselines/dev_replan.json` recorded with the flag **OFF** (legacy) on
  `qwen3-coder:30b`, tolerance 0.15.

  **Result (3 runs each):** OFF `exact_acc=100%` (stable); ON `exact_acc=87.5%`
  (stable), `safe_acc=100%` (compaction never made a read-only goal propose a
  write — the core safety property holds). `--check` with the flag on PASSES
  (87.5% ≥ 85% threshold). The single ON failure is reproducibly
  `replan-build-missing-module` (8-step prefix).

  **Root cause (captured raw ON vs OFF):** it is NOT lost diagnostic info. OFF the
  model fixes directly (`WRITE_FILE → RUN_TERMINAL build`); ON it opens with
  `RUN_TERMINAL "npm run build --verbose"` to **re-investigate** before writing the
  fix — a precedence violation — even though the TS2307 error already names the
  cause. The terser prompt makes the model gather more info first.

  **Mitigation applied (R1.5 refinement):** the compactor now keeps the LAST
  read-only step of a collapsed run verbatim, preserving the most-recent
  diagnostic (savings on this case 431→351 chars). This is the right design and is
  unit-tested, but it did **not** move the gate (ON still 87.5%, 2 runs): the
  residual cost is the re-investigation behavior above, not information loss, and
  fixing it further would mean either weakening the eval (dishonest) or nudging the
  production replan prompt for all replans (out of scope). So the net trade is
  ~30–40% prompt reduction on long trajectories for a measured, reproducible
  ~12.5-pt cost on recovery *ordering* of one shape, with safety fully intact —
  whether to ship it on by default is a trade-off call (task 7).
- **Unit tests:** `tests/test_trajectory_reduction.py`, one assertion per numbered
  criterion (cite the criterion in the test name), e.g.:
  - `test_r1_2_keeps_recent_verbatim`
  - `test_r1_4_failure_signal_preserved_when_old`
  - `test_r1_5_collapses_readonly_runs`
  - `test_r1_6_paths_never_truncated`
  - `test_r2_4_disabled_is_byte_identical_to_legacy` (golden: capture today's
    `_replan` prompt for a fixed trajectory; assert `enabled=False` reproduces it)
  - `test_r3_3_does_not_mutate_steps`
- **Measurement (not a gate):** A/B `chars_saved` and the `inferences.tokens_in`
  delta on a representative long plan via `python -m monitoring.cost_ledger`
  before/after, to quantify the win in the daily review.

Each acceptance criterion in §3 maps to ≥1 test or eval case above.

---

## 6. Tasks

- [x] 1. Add `inference/trajectory.py` with `render_trajectory()` — satisfies R1.1–R1.6, R3.1–R3.3.
- [x] 2. Refactor `_replan`/`_try_replan` to call it (flag-gated; `enabled=False` = legacy) — R2.1, R2.4.
- [x] 3. Refactor `_reflect` to call it with the 200/600 budget — R2.2.
- [x] 4. Record `traj_*` span attrs when enabled — R3.4 (needs Rec #2 cost spans).
- [x] 5. Add `tests/test_trajectory_reduction.py` (one test per criterion) + golden byte-identical test — R2.4. **(11 tests, green.)**
- [x] 6. Add long-prefix eval cases; lock baseline; confirm no replan-success regression — R2.3. **(New `replan` eval mode + `dev_replan` suite + baseline; gate PASSES at 87.5% with one reproducible ordering miss — see §5.)**
- [x] 6b. Compactor mitigation: keep the last read-only step of a collapsed run verbatim (R1.5 refinement) — applied + unit-tested. Preserves the diagnostic but did NOT recover the build-missing-module case (residual cause is re-investigation, not info loss — §5).
- [x] 7. **DECISION PENDING (Brad):** flip `DA_TRAJECTORY_REDUCE` default on? Gate passes (87.5% ≥ 85%, safety 100%) but there's a measured, reproducible ~12.5pt recovery-ordering cost on the long-prefix build-shape (§5), and the cheap compactor tweak did not eliminate it. Further fixes would require weakening the eval or nudging the production prompt — neither pursued. Trade-off call. Record token delta in `docs/daily/` when/if flipped. **(DECISION: Default flipped to ON by user request on 2026-07-02)**
- [x] 8. Update `CLAUDE.md` Known Gotchas (new flag) — done.
