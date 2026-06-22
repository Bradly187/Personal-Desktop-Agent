# Spec: Dev-Agent Critic + Tester loop

> One feature → one folder. This spec is the source of truth; the code is the
> build artifact. Design and Tasks are kept inline (§4–§6) until they outgrow the
> file. Keep this updated as the design evolves — a stale spec is worse than none.

---

## 1. Background — the "Why"

The DevAgent runs a **plan → execute → reflect** loop
([inference/dev_agent.py:531](../../inference/dev_agent.py)), with a
failure-triggered `_replan` ([inference/dev_agent.py:1017](../../inference/dev_agent.py))
as its only recovery mechanism. What it lacks — and what every modern
agentic-coding reference architecture prescribes — is an **independent review of
generated code before it is accepted**, and an **autonomous test loop** that
generates and runs tests against new code and reacts to the failures.

A gap analysis against the standard agentic-coding pattern set (Orchestration /
Multi-Agent Topology / Code-Awareness / Sandbox) surfaced three genuine misses;
this spec closes the two highest-leverage ones:

- **No code Critic.** `_reflect` ([inference/dev_agent.py:1716](../../inference/dev_agent.py))
  is the model summarizing *its own* work — not an adversarial second opinion.
  The "Critic" pattern (one agent writes, an independent agent reviews against the
  goal / quality / security before commit) is absent for the `WRITE_FILE` path.
  The lint gate ([inference/edit_format.py](../../inference/edit_format.py),
  `../edit-format-aci/`) checks *syntax*; nothing checks *intent or correctness*.
- **No autonomous Tester.** `RUN_TERMINAL` can run `pytest` and the executor is
  already sandboxed ([inference/sandbox.py](../../inference/sandbox.py)), but
  nothing closes the loop: write code → generate a test → run it → feed the
  traceback back to `_replan`. The agent only learns a change is broken if a
  human-written test happens to exercise it later.

We do **not** invent a new framework. The precedent already exists in-tree and is
proven: **`_verify_math_with_cas`** ([inference/dev_agent.py:643](../../inference/dev_agent.py))
independently recomputes every math answer with SymPy and appends a verdict —
generation checked by an independent, deterministic-where-possible verifier. This
spec generalizes that shape from the `math` domain to the `WRITE_FILE`/code path,
reusing the existing replan loop as the feedback channel (exactly as the
edit-format lint gate does).

**Constraint that shapes the design (AGENTS.md #6):** we are single-machine,
VRAM-bounded. The Critic is **not** a second large model loaded concurrently —
that would fight Whisper for VRAM and violate the `ResourceGovernor` lifecycle.
The Critic is a **fresh-context pass on the already-loaded plan-domain model with
an adversarial reviewer role/prompt** (or, optionally, the cheaper `general`
model). "Independent" here means *independent context and role*, not *independent
weights*.

**Status:** Draft
**Owner / author session:** Claude Code (Opus 4.8)
**Related:** `../edit-format-aci/` (the lint gate this layers on top of — Critic
runs *after* the syntax gate passes), `../dev-agent-plan-contract/` (sibling
sprint: auto-repair for malformed plans — the Critic assumes a parsed plan to
review), `../sandbox-interactive-hardening/` (sibling sprint: the Tester loop
depends on its interactive-hang fix), `../trajectory-reduction/` (sibling
plan→execute→replan token-economics work), `../accessibility-agent/` (DevAgent
sits above HybridCoordinator). Honors AGENTS.md #4 (safe-by-default: a Critic
veto / timeout / ambiguity blocks the commit), #6 (no new concurrent VRAM), #7
(unchanged path boundaries), #10 (function-granular changes only).

---

## 2. Glossary

- **Critic**: a post-generation, pre-commit review pass over a `WRITE_FILE`
  edit (or a whole plan's diff). Runs on the already-loaded plan/general model
  with an adversarial reviewer prompt and a **fresh context** (it does NOT see the
  generator's reasoning — only the goal, the resulting diff, and relevant
  read-only RAG context). Emits a structured `CriticVerdict`.
- **CriticVerdict**: structured result — `decision` (`pass` | `revise` | `block`),
  `confidence` (0–1), `findings` (list of `{severity, message, target}`), and a
  `suggested_fix` hint. Never a raw dict across the boundary (a dataclass, like
  `EditError`).
- **Tester loop**: an optional, opt-in sub-flow for `code`/`plan` domains that,
  after a successful `WRITE_FILE` to a source file, generates a focused test,
  runs it through the existing sandboxed `RUN_TERMINAL`, and on failure feeds the
  traceback into `_replan` as an observation.
- **TestArtifact**: the generated test — `path` (under a writable root,
  default a temp scratch dir, NOT committed unless asked), `body`, and the
  `target` file/symbol it exercises.
- **Reviewer role-prompt**: the system/role text that turns the plan model into a
  skeptic — "you did NOT write this; find why it fails the goal; default to
  `revise` when uncertain."
- **Commit gate**: the existing `_confirm_destructive_op`
  ([inference/dev_agent.py:2265](../../inference/dev_agent.py)) voice/goal-session
  approval. The Critic runs *before* it and can *escalate* a write from
  auto-approved to must-confirm, but never *downgrades* an existing gate.

---

## 3. Requirements (EARS acceptance criteria)

### Requirement 1: Independent Critic on code edits

**User Story:** As Brad, I want generated code reviewed by an independent pass
against my goal before it is accepted, so a plausible-but-wrong edit is caught
before it lands instead of after a later test breaks.

#### Acceptance Criteria
1. WHEN a `WRITE_FILE` step targets a source file and passes the lint gate, THE
   `Critic` SHALL review the resulting diff against the step goal BEFORE the write
   is committed (after `EditApplier`, before/around `_confirm_destructive_op`).
2. THE `Critic` SHALL run on an already-loaded model (plan or general domain) with
   a fresh context and reviewer role-prompt — it SHALL NOT load a new model
   concurrently (AGENTS.md #6) and SHALL NOT receive the generator's chain of
   thought.
3. THE `Critic` SHALL return a `CriticVerdict(decision, confidence, findings,
   suggested_fix)` and SHALL NOT pass raw dicts across the pipeline boundary.
4. WHEN the verdict is `revise`, THE finding text SHALL be surfaced as the step
   `result` that `_replan` already serializes — no new feedback channel (mirrors
   the `EditError` path in `../edit-format-aci/`).
5. IF the Critic call errors, times out, or returns an unparseable verdict, THEN
   THE DevAgent SHALL fail safe: treat it as `revise` with low confidence and
   route the write through the must-confirm `_confirm_destructive_op` gate rather
   than auto-approving (AGENTS.md #4 — ambiguity never auto-commits).
6. WHEN the verdict is `block` (high-confidence correctness/security finding), THE
   write SHALL NOT be committed and the finding SHALL drive `_replan`.
7. THE Critic SHALL be **bounded**: at most `max_revisions` (config, default 1)
   Critic-driven revise cycles per step before the step is handed to the normal
   `_replan`/halt path, so the loop cannot spin.

### Requirement 2: Critic escalates, never silently weakens, the commit gate

**User Story:** As Brad, I want the Critic to be able to demand confirmation on a
risky edit, but never to bypass an approval I'd otherwise be asked for.

#### Acceptance Criteria
1. WHEN the Critic returns `pass` with high confidence for an edit that the
   existing gate would auto-approve, THE write MAY proceed unchanged (no new
   friction on the happy path).
2. WHEN the Critic returns any non-`pass` verdict OR low confidence, THE write
   SHALL be escalated to require explicit `_confirm_destructive_op` approval.
3. THE Critic SHALL NOT downgrade, skip, or satisfy any approval the goal-session
   or destructive-op gate would otherwise require (AGENTS.md #4, #7).
4. FOR ALL Critic outcomes, THE existing `_snapshot_for_write` saga
   ([../edit-format-aci/](../edit-format-aci/) R1.4) SHALL be preserved: a
   committed write is reversible; a blocked write registers no compensation.

### Requirement 3: Autonomous Tester loop (opt-in)

**User Story:** As Brad, I want the agent to write a quick test for new code and
run it, so it catches its own regressions instead of waiting for me.

#### Acceptance Criteria
1. WHILE the Tester loop is enabled AND a `WRITE_FILE` to a `.py` source file
   committed successfully, THE DevAgent SHALL generate a focused `TestArtifact`
   for the changed symbol(s) and run it via the existing sandboxed `RUN_TERMINAL`.
2. THE generated test SHALL be written under a writable root (default a scratch
   temp dir, NOT the repo `tests/` tree) and SHALL NOT be committed to git unless
   the user's goal explicitly asked for a test.
3. WHEN the generated test fails, THE traceback (output-capped, as `sandbox.py`
   already does) SHALL be fed to `_replan` as an observation, driving a fix
   attempt within the same `max_revisions` budget as R1.7.
4. WHEN the generated test passes, THE Tester SHALL record a one-line success note
   in the step result and continue the plan.
5. IF test generation or execution errors (sandbox unavailable, timeout, model
   failure), THEN THE Tester SHALL degrade gracefully — log a WARNING, skip the
   test, and NOT block the plan (AGENTS.md degrade-gracefully). A skipped test is
   never reported as a pass.
6. THE Tester loop SHALL respect the `ResourceGovernor`: on a pain-day flare or
   VRAM eviction it SHALL be skipped (test generation is a non-essential model
   call), consistent with AGENTS.md #5/#6.
7. THE Tester loop SHALL NOT assume environment persistence across `RUN_TERMINAL`
   steps. Each sandboxed invocation is **ephemeral** — fresh process,
   `--unshare-net`, no carried-over env / cwd / installed deps
   ([inference/sandbox.py](../../inference/sandbox.py)). A generated test SHALL be
   self-contained within ONE invocation (e.g. `pip install -q … && pytest <file>`
   chained), never split across steps that rely on shared state. IF a test
   genuinely needs a persistent dev server or multi-step environment, THE Tester
   SHALL skip it (R3.5 degrade) rather than emit a false pass/fail from a
   torn-down environment. <!-- The persistent-session sandbox pattern is a
   conscious non-goal (single-user, own-goals threat model); see
   `../sandbox-interactive-hardening/` §1. -->.

### Requirement 4: Flag-gated, eval-gated, default OFF

**User Story:** As Brad, I want this shipped dark and proven on evals before it
changes the agent's default behavior, exactly like the edit-format and
trajectory-reduction work.

#### Acceptance Criteria
1. THE Critic and Tester SHALL each ship behind an independent flag, **default
   OFF**; with both unset the plan→execute→reflect loop is byte-identical to
   today.
2. THE feature SHALL NOT be made default-on until its `evals/` suite baseline is
   locked and shows a net correctness gain (mirrors `../edit-format-aci/` task 6).
3. THE Critic/Tester model calls SHALL emit the existing inference spans
   (tokens/cost via `cost_ledger`) so the added latency and Bedrock/local spend
   are observable before any default flip.

---

## 4. Technical Design

> Hooks into the **DevAgent** plan→execute loop only. The `CommandExecutor`
> whole-file path, the `_path_in_scope` sandbox, and the `ipad_bridge` payloads
> are all unchanged.

- **Entry point / pipeline boundary:** `DevAgent._execute_step` WRITE_FILE branch
  ([inference/dev_agent.py:1767](../../inference/dev_agent.py)), immediately after
  `EditApplier.apply` succeeds (lint gate passed) and around
  `_confirm_destructive_op`. The Critic verdict decides: commit (pass) → existing
  path; escalate (low-conf/non-pass) → force confirm; revise/block → no write,
  diagnostic becomes the step result → existing `_replan` reacts. The Tester loop
  fires *after* a committed `WRITE_FILE` to a source file.
- **New module:** `inference/critic.py` — `Critic`, `CriticVerdict`, the reviewer
  role-prompt builder, and the verdict parser (JSON-schema-constrained like the
  planner's `_PLAN_JSON_SCHEMA`, regex fallback). Pure orchestration over one
  model call; no new framework. Mirrors the shape of `_verify_math_with_cas`
  ([inference/dev_agent.py:643](../../inference/dev_agent.py)) — the in-tree
  precedent for "independent verifier appended to a generation".
- **New module:** `inference/tester.py` — `Tester`, `TestArtifact`, test-prompt
  builder; runs generation through `ModelRouter` (code domain) and execution
  through the existing `inference/sandbox.run_sandboxed`. No new sandbox.
- **Ephemeral-sandbox constraint (R3.7):** `run_sandboxed` is one-shot per call —
  state does not carry between steps. The Tester therefore generates a
  **self-contained** test and runs it in a single chained invocation, and skips
  anything needing a persistent server (it cannot "start a dev server, edit,
  rerun in place" — that persistent-session pattern is an explicit non-goal). This
  spec assumes the interactive-hang hardening from
  `../sandbox-interactive-hardening/` has landed, so a test that prompts fails fast
  instead of burning the 60 s wall timeout.
- **Reused seams:** `ModelRouter` (no new profile/model — Critic uses the loaded
  plan/general model), `inference/sandbox.py` (Tester execution),
  `_confirm_destructive_op` (gate), `_snapshot_for_write` saga (rollback),
  `_replan` (feedback channel), `cost_ledger`/inference spans (observability).
- **New dataclasses (never raw dicts):** `CriticVerdict(decision: str, confidence:
  float, findings: list[Finding], suggested_fix: str)`, `Finding(severity, message,
  target)`, `TestArtifact(path, body, target)`. No new `Command` fields.
- **Models / VRAM (AGENTS.md #6):** **no new model loaded.** Critic = fresh-context
  call on the already-resident plan or `general` model; Tester gen = code-domain
  model already in the roster. Both are gated off on flare/eviction (R3.6). No
  `ResourceGovernor` roster change.
- **60 Hz loop (AGENTS.md #2):** unaffected — all of this is in the async DevAgent
  path, model calls already `await`/`to_thread`, never in `FusionEngine`.
- **Persistence:** none required to start. Critic verdicts and Tester outcomes are
  written to the existing **audit log** (`storage/audit_log.py`) via
  `fire_and_log`, NOT a new `agent.db` table — so **no schema change, no
  `PRAGMA user_version` bump** (AGENTS.md #1). A dedicated `critic_verdicts` table
  is a deferred follow-up only if analytics need it (would then require a migration
  + version bump per the `changing-the-db-schema` skill).
- **Cross-platform:** none — does not touch `ipad_bridge` (AGENTS.md #3 N/A).

### Configuration (flat YAML)

```yaml
dev_agent_critic:
  critic:
    enabled: false           # master flag; OFF → today's loop, byte-identical
    model_domain: plan       # reuse the loaded plan model (or "general" for cheaper)
    max_revisions: 1         # Critic-driven revise cycles per step before _replan
    confidence_floor: 0.60   # below this → escalate to must-confirm (R1.5/R2.2)
    block_on:                # finding severities that force decision=block
      - security
      - correctness
  tester:
    enabled: false           # independent flag; generate+run tests for new .py
    scratch_root: ~/AppData/Local/Temp/pda_dev_tests  # NOT the repo tests/ tree
    commit_generated_tests: false   # only true when the goal explicitly asked
    sandbox_timeout_s: 60    # inherits inference/sandbox.py ceilings
    skip_on_flare: true      # ResourceGovernor / pain-day → skip (R3.6)
```

---

## 5. Behavior Verification (executable, not prose)

- **Unit tests:** `tests/test_critic.py` and `tests/test_tester.py` — one
  assertion per numbered criterion (cite it in the test name), model calls stubbed
  so they run in CI:
  - R1.3/R1.4: a stubbed `revise` verdict yields no write + diagnostic step result;
    a `pass` writes through.
  - R1.5: Critic exception/timeout/garbage → treated as low-confidence revise →
    must-confirm path (fail-safe).
  - R1.7/R3.3: revise budget is bounded; exceeding it hands off to `_replan`.
  - R2.2/R2.3: non-pass/low-conf escalates the gate; Critic never satisfies an
    approval the goal-session gate requires.
  - R2.4: blocked write registers no saga compensation; committed write is
    reversible.
  - R3.1/R3.4/R3.5: passing generated test → success note; failing → traceback to
    `_replan`; sandbox-unavailable → skip (never a false pass).
  - R4.1: both flags OFF → `_execute_step` WRITE_FILE path byte-identical to the
    pre-feature snapshot.
- **Eval suite (the payoff, gate before any default flip):** add a
  `dev_critic` mode/cases under `evals/suites/` that run a fixed model over a set
  of "plausible-but-wrong edit" fixtures (intentional off-by-one, wrong-branch,
  dropped guard) and assert the Critic catches them; A/B Critic-on vs Critic-off
  net correctness and measure the added latency/cost (R4.3). Lock the baseline in
  `evals/baselines/` (see the `running-the-eval-harness` skill). **Do NOT flip any
  default until this passes.**

Each acceptance criterion in §3 maps to at least one test/eval above.

---

## 6. Tasks

- [x] 1. `inference/critic.py`: `Critic` + `CriticVerdict`/`Finding` + reviewer
      role-prompt + deterministic `parse_verdict` (JSON/regex, conservative-on-
      ambiguity → REVISE, security/correctness floor). Pure orchestration over one
      already-loaded-model call, fresh empty context — satisfies R1.2, R1.3.
- [x] 2. Wired into `_execute_step` WRITE_FILE branch: apply (lint) → critic →
      escalated `_confirm_destructive_op(force=…)` → write. PASS→commit (low-conf
      forces confirm), REVISE/BLOCK→no write + diagnostic step result (no snapshot/
      compensation), bounded `_critic_max_revisions` per path then escalate+allow.
      `force` bypasses the upfront-auth short-circuit (adds friction, never weakens
      a gate). Disabled path is byte-identical legacy — satisfies R1.1, R1.4–R1.7,
      R2.1–R2.4, R4.1.
- [x] 3. `inference/tester.py`: `Tester` + `TestArtifact`/`TestOutcome` +
      `is_testable_source`; generates a focused pytest test for a committed `.py`
      SOURCE write, runs it one-shot via `inference.sandbox.run_sandboxed`
      (ephemeral-aware, R3.7), degrades gracefully (R3.5), skip-on-flare hook
      (R3.6). Wired via `_maybe_run_tester` at both WRITE_FILE write-returns.
      **Safe-observation semantics (user-chosen):** a failing generated test is
      appended to the step result as an observation — the good write is NOT marked
      failed, so the saga never rolls back working code. R3.3's stronger "force a
      replan" coupling is a deliberate non-goal (would roll back a good write).
      Satisfies R3.1, R3.2, R3.4–R3.7; R3.3 met as observation (not forced replan).
- [~] 4. Config plumbing — env flags done (`DA_CRITIC`, `DA_CRITIC_FLOOR`,
      `DA_CRITIC_MAX_REVISIONS`, `DA_CRITIC_DOMAIN`; instance attrs + `set_critic()`
      for wiring/tests). Audit-log writes for verdicts + config-file mapping pending
      (Tester lands with task 3). R4.3 spans: the critic re-infer emits its own
      inference span via the router. R4.1 met (default OFF).
- [x] 5. `tests/test_critic.py` (16) + `tests/test_tester.py` (17) — `parse_verdict`
      + Critic WRITE_FILE wiring (pass-writes, revise/block-no-write, low-conf+error
      escalate, never-weakens-deny, bounded revisions, disabled byte-identical) and
      Tester (source selection, pass/fail/skip outcomes, graceful degrade, safe-
      observation wiring, flare-skip, disabled no-op). CI-safe.
- [x] 6. Eval: `dev_critic` suite over "plausible-but-wrong edit" fixtures; A/B
      on/off; lock baseline. **Gate before flipping any default** — satisfies R4.2.
      DONE 2026-06-21: 8 cases (6 correctness + 2 security), all syntactically-valid
      bugs (lint arm 0%). Critic arm 100% catch rate (qwen3-coder:30b). Baseline locked:
      `evals/baselines/dev_critic.json` (catch_rate_critic=1.0, tolerance=10%).
      Harness: `evals/dev_critic.py`, `evals/suites/dev_critic.jsonl`,
      `evals/fixtures/dev_critic/` (8 fixtures). Integrated in `evals/run.py` as
      `--mode dev_critic`.
- [x] 7. Docs: "Critic + Tester loop" gotcha added to `CLAUDE.md` Known Gotchas
      (fail-safe escalation, safe-observation Tester, no new VRAM, default OFF).
