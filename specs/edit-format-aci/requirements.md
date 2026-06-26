# Spec: Edit-Format ACI — lint gate + per-model edit format

> One feature → one folder. This spec is the source of truth; the code is the
> build artifact. Design and Tasks are kept inline (§4–§6) until they outgrow
> the file. Keep this updated as the design evolves.

---

## 1. Background — the "Why"

The DevAgent's `WRITE_FILE` verb is the agent's only file-mutation primitive, and
today it is **whole-file overwrite with no validation**: the plan-domain model
emits the complete intended file in the step `body`, and `_write_file` does
`mkdir -p` + `Path.write_text(body)` verbatim — no diff, no matching, no syntax
check ([inference/dev_agent.py:2496](../../inference/dev_agent.py),
[core/command_executor.py:834](../../core/command_executor.py)). The edit format
is hardcoded for every model; `ModelProfile`
([inference/model_router.py:298](../../inference/model_router.py)) has no
`edit_format` field.

This is the configuration that the edit-format research literature argues
*against* for a **local/weaker model executor** — exactly our case
(`qwen3-coder:30b` / `gemma4:12b` / `llama3.1:8b` planners on the RTX 5090):

- **Whole-file** is "keep only for weak models": expensive to stream and prone to
  *silent elision* — the model re-types a long file and drops lines it didn't mean
  to. Nothing catches the corruption; it persists to disk and is only discovered
  later if a `RUN_TERMINAL` test happens to exercise it, triggering a `_replan`.
- **A lint gate is the single highest-value-per-effort guardrail** in the ACI
  literature (SWE-agent: reject the edit if the result isn't syntactically valid).
  We have none.
- **"Make the edit format a per-model config knob"** — format alone can swing the
  *same* model dramatically; the highest-information early experiment is to hold
  the model fixed and A/B edit formats on a SWE-bench-Verified subset. We can't run
  that ablation because the format is hardcoded.
- **The failed-edit error message is part of the interface.** Ours is generic
  (`ERROR: <exception>` / `WRITE_FILE denied: …`) with no diagnostic or suggested
  target.

Source handoff: `agentic_swe_handoff.md` (research summary, 2026-06-20), §4–§5.

The fix, smallest-first: (a) add a **lint gate** that validates the post-edit file
before it persists and rejects with a diagnostic message on failure; (b) add an
**`edit_format` per-model knob** so we can introduce a structured format
(no-line-number unified diff or hashline) alongside whole-file and *measure* it.
The path sandbox (`_path_in_scope`, realpath-based) is already solid and is out of
scope here.

**Status:** Done — tasks 1–7 landed (lint gate + per-model `edit_format` knob +
the **hashline** structured format end-to-end: applier with layered matcher +
atomic batch, READ_FILE anchoring, plan-prompt instructions + worked example; the
`--mode edit_ab` A/B eval with easy + hard subsets, baselines locked; docs). Default
is `whole_file` everywhere — byte-identical to legacy; hashline activates only when a
model is configured for it. **Task 8 (2026-06-25) adds R5 — the `EDIT_FILE` verb**
(aider-style SEARCH/REPLACE `search_replace` format) for Claude-Code-parity surgical
edits to existing files: fail-closed on non-unique/stale SEARCH, same lint gate +
Critic + saga + Tester path as WRITE_FILE, available to every plan model. 46
deterministic unit tests in `tests/test_edit_format.py`. The A/B verdict (tasks 6) **keeps `whole_file` default**:
silent elision did not occur even on ~180-line files, whole_file led on correctness
(100% vs 80% hard subset), and hashline's gain is purely efficiency (~9–23× less
output, ~2–4× faster) — an opt-in cost play, not a correctness upgrade. Follow-ups
(out of this spec): enable hashline per-model where cost/latency dominates after the
op-format reliability improves. **Update 2026-06-25:** `udiff` is now implemented
(`EditApplier._apply_udiff` + `_parse_udiff_hunks`, layered match R4.2, atomic
bottom-up + overlap-reject R4.3, fail-closed; `UDIFF_PROMPT_INSTRUCTIONS` injected
per-model like hashline) — it is a third opt-in `edit_format` knob value, still
`whole_file` by default. Tests: `tests/test_edit_format_udiff.py` (17).
**Owner / author session:** Claude Code (Opus 4.8)
**Related:** `../trajectory-reduction/` (sibling DevAgent token-economics work;
both touch the plan→execute→replan loop), `../accessibility-agent/` (DevAgent
sits above HybridCoordinator). Honors AGENTS.md #4 (safe-by-default), #7 (path
boundaries — unchanged), #10 (function-granular regeneration only).

---

## 2. Glossary

- **WRITE_FILE**: the dev-agent verb that mutates a file. Today carries `args`
  (path) + `body` (whole-file content).
- **Edit format**: the contract by which the model expresses a file change.
  Candidates this spec scopes: `whole_file` (current), `udiff` (unified diff with
  **no** line numbers, each hunk interpreted as search/replace), `hashline`
  (lines anchored by `line:hash`; model edits by referencing the hash, never
  reproducing surrounding text).
- **EditApplier**: the new deterministic component that takes `(current_file_text,
  model_payload, edit_format)` → either a `new_file_text` or a structured
  `EditError`. Pure function over text; no I/O, no LLM call.
- **Lint gate**: a post-apply, pre-persist validation pass. For Python, `ast.parse`
  on the resulting text; reject (do not write) if it raises `SyntaxError`. Other
  languages: pluggable, no-op until a validator is registered.
- **EditError**: structured failure carrying `reason` (mismatch | syntax | scope |
  io), a human-diagnostic message, and — where derivable — the likely-correct
  target, fed back to the model on retry.
- **ModelProfile.edit_format**: new per-model field selecting which format a given
  plan-domain model is prompted for and parsed against.

---

## 3. Requirements (EARS acceptance criteria)

### Requirement 1: Lint gate before persistence

**User Story:** As Brad, I want a syntactically-broken edit rejected before it
lands on disk, so a local model's silent elision can't corrupt a working file.

#### Acceptance Criteria
1. WHEN a `WRITE_FILE` step produces resulting text for a path with a registered
   validator, THE `EditApplier` SHALL run the validator BEFORE writing to disk.
2. IF the validator reports the resulting text is invalid (e.g. Python
   `SyntaxError`), THEN THE `EditApplier` SHALL NOT write the file and SHALL return
   an `EditError(reason="syntax", …)` carrying the validator's line/message.
3. THE lint gate SHALL run for `.py` targets via `ast.parse`; for a path with no
   registered validator THE gate SHALL pass through (no-op) and write normally.
4. THE existing saga snapshot (`_snapshot_for_write` → `comp_args`) SHALL still be
   captured for an applied write, and a rejected write SHALL register no
   compensation (nothing was written).
   <!-- Safe-by-default (AGENTS.md #4): a write that fails validation fails CLOSED
        — file unchanged, error surfaced to the replan loop. -->

### Requirement 2: Diagnostic failure feedback

**User Story:** As Brad, I want a failed edit to tell the model *what* mismatched,
so a weak model can recover on the next turn instead of guessing.

#### Acceptance Criteria
1. WHEN an edit fails to apply for a structured format (search target not found),
   THE `EditApplier` SHALL return an `EditError` whose message names the missing
   target text and, where a near-match exists, the most-similar candidate region.
2. WHEN the lint gate rejects a write, THE error returned to the loop SHALL include
   the validator message and the offending line, not a bare exception string.
3. THE `EditError` message SHALL restate which format the model was expected to
   produce and instruct it to resend only the failed edit.
4. FOR ALL `EditError` paths, THE message SHALL be surfaced as the step `result`
   that `_replan`/`_reflect` already serialize — no new feedback channel.

### Requirement 3: Per-model edit-format knob

**User Story:** As Brad, I want each plan-domain model to use the edit format it
handles best, and to be able to A/B formats, so I can find the harness ceiling
without swapping models.

#### Acceptance Criteria
1. THE `ModelProfile` SHALL expose an `edit_format` field defaulting to
   `"whole_file"` (byte-identical legacy behavior when unset).
2. WHEN the plan prompt is rendered for a model, THE planner SHALL emit the
   `WRITE_FILE` instructions matching that model's `edit_format`.
3. THE `EditApplier` SHALL parse and apply the `WRITE_FILE` payload according to
   the active `edit_format`, and an unknown/misconfigured format SHALL fall back to
   `whole_file` with a logged WARNING (never crash — AGENTS.md degrade-gracefully).
4. THE feature SHALL ship behind config (default `whole_file` everywhere) so the
   eval baseline is unchanged until a format is explicitly enabled per model.

### Requirement 4: Structured edit format (at least one)

**User Story:** As Brad, I want at least one non-whole-file format implemented so
the ablation has something to compare against.

#### Acceptance Criteria
1. THE `EditApplier` SHALL implement at least one of `udiff` (no line numbers,
   hunk-as-search/replace) or `hashline`, behind the `edit_format` knob.
2. WHEN applying a structured edit, THE matcher SHALL be layered: exact →
   whitespace-normalized → fuzzy; a fuzzy match below threshold SHALL fail with an
   `EditError` (R2.1) rather than apply a wrong region.
3. FOR batched edits in one step, THE applier SHALL validate ALL hunks before
   applying ANY (atomic), apply bottom-up by descending position, and reject
   overlapping ranges with a clear `EditError`.

### Requirement 5: EDIT_FILE — a surgical edit verb (Claude-Code parity)

**User Story:** As Brad, I want a dedicated `EDIT_FILE` verb that makes a
*targeted* change to an existing file without re-emitting the whole thing, so the
planner can edit a 1000-line file safely the way Claude Code's `Edit` does —
closing the biggest correctness gap vs Claude Code.

> Note: R4's `hashline` is a per-model **WRITE_FILE** format that requires a prior
> READ_FILE to anchor on `line:hash`. R5's `EDIT_FILE` is a **separate verb**
> available to *every* plan model regardless of its WRITE_FILE knob, using
> self-describing aider-style SEARCH/REPLACE blocks (no line:hash round-trip).

#### Acceptance Criteria
1. THE planner SHALL be able to emit an `EDIT_FILE <path>` step whose body is one
   or more `<<<<<<< SEARCH / ======= / >>>>>>> REPLACE` blocks; THE `EditApplier`
   SHALL apply them via the `search_replace` format independent of the model's
   per-model WRITE_FILE `edit_format`.
2. WHEN a SEARCH block does not match the current file content EXACTLY ONCE (zero
   matches = stale, or >1 = ambiguous), THE applier SHALL fail closed with an
   `EditError(reason="mismatch", …)` naming the failure and SHALL NOT write the
   file (mirrors the hashline atomic contract).
3. THE EDIT_FILE result SHALL pass through the SAME lint gate as WRITE_FILE (R1),
   so a result that is invalid Python is rejected pre-write.
4. `EDIT_FILE` SHALL be a **destructive** verb: it routes through
   `_confirm_destructive_op`, the Critic (when ON), the `_snapshot_for_write`
   saga snapshot, and the autonomous Tester exactly as WRITE_FILE does — no new
   bypass, fail-safe-DENY on ambiguity (AGENTS.md #4).
5. AN empty SEARCH SHALL be valid only against an empty file (creation); against a
   non-empty file it SHALL fail closed (whole-file rewrites stay on WRITE_FILE).
6. THE plan prompt SHALL document EDIT_FILE and instruct the planner to prefer it
   for targeted changes to existing files, reserving WRITE_FILE for new/whole-file
   rewrites.

---

## 4. Technical Design

> Promote to `design.md` if it grows diagrams. Hooks into the **DevAgent**
> plan→execute loop only; the `CommandExecutor` whole-file path and the
> `_path_in_scope` sandbox are unchanged.

- **Entry point / pipeline boundary:** `DevAgent._execute_step` WRITE_FILE branch
  ([inference/dev_agent.py:2037](../../inference/dev_agent.py)). Insert
  `EditApplier.apply(...)` between `_confirm_destructive_op` (unchanged — still
  gates the write) and the actual `_write_file`. The applier returns either
  `new_text` (→ snapshot, write, success) or an `EditError` (→ no write, step
  result = diagnostic message, `success=False` → existing replan loop reacts).
- **New module:** `inference/edit_format.py` — `EditApplier`, `EditError`, the
  format parsers (`whole_file`, `udiff` and/or `hashline`), the layered matcher,
  and the validator registry (`{".py": ast_parse_validator}`). Pure functions,
  deterministic, no LLM call (mirrors `inference/trajectory.py`'s shape).
- **New `Command`/dataclass fields:** none on `Command`. `ModelProfile` gains
  `edit_format: str = "whole_file"`
  ([inference/model_router.py:298](../../inference/model_router.py)). `EditError`
  is a new local dataclass (`reason`, `message`, `target`, `suggestion`), never a
  raw dict across the boundary.
- **Prompt rendering:** `_PLAN_PROMPT` ([inference/model_router.py:149](../../inference/model_router.py))
  branches the `WRITE_FILE` instruction block by the active `edit_format`. Keep the
  default block byte-identical so `whole_file` models see no change.
- **Models / VRAM:** no new model loaded; pure-CPU text transform. No
  `ResourceGovernor` interaction (AGENTS.md #6 N/A).
- **Persistence:** none. No `agent.db` schema change; `PRAGMA user_version` stays
  at its current value (AGENTS.md #1). The existing `_snapshot_for_write` saga
  rollback is preserved (R1.4).
- **Cross-platform:** none — does not touch `ipad_bridge` payloads (AGENTS.md #3
  N/A).
- **60 Hz loop (AGENTS.md #2):** unaffected — this is in the async DevAgent path,
  not `FusionEngine`; apply/lint run via `asyncio.to_thread` like the current
  `_write_file`.

### Configuration (flat YAML)

```yaml
edit_format_aci:
  enabled: false              # master flag; OFF keeps whole_file everywhere
  lint_gate:
    enabled: true             # lint runs whenever a validator is registered
    validators:
      ".py": ast_parse        # Python syntax check; pass-through for others
  per_model:                  # overrides ModelProfile.edit_format at runtime
    "qwen3-coder:30b": whole_file   # flip to udiff/hashline for the A/B
    "gemma4:12b": whole_file
    "llama3.1:8b": whole_file
  matcher:
    fuzzy_threshold: 0.90     # below this → EditError, never apply wrong region
```

---

## 5. Behavior Verification (executable, not prose)

- **Unit tests:** `tests/test_edit_format.py` — one assertion per numbered
  criterion (cite it in the test name), all pure-function (no model):
  - R1.2/R1.3: a Python edit that yields `SyntaxError` is rejected, file untouched;
    a valid edit and a non-`.py` path write through.
  - R2.1/R2.2: mismatch and lint errors carry diagnostic text + suggested target.
  - R3.1/R3.3: default profile is `whole_file` byte-identical; unknown format
    warns + falls back.
  - R4.2/R4.3: layered matcher (exact/ws/fuzzy), atomic batch, overlap rejection.
- **Eval suite:** the payoff experiment. Hold the model fixed and run the existing
  `dev_execution` / `dev_replan` suites with `edit_format` set to `whole_file` vs
  the structured format on a SWE-bench-Verified subset; lock baselines in
  `evals/baselines/`. Add cases under `evals/suites/dev_execution.jsonl`. Do NOT
  flip any default until that gate passes (see `running-the-eval-harness` skill).

Each acceptance criterion in §3 maps to at least one test above.

---

## 6. Tasks

- [x] 1. `inference/edit_format.py`: `EditApplier` + `EditError` + `whole_file`
      parser + validator registry (`.py` → `ast.parse`) — satisfies R1.1–R1.3, R2.4.
- [x] 2. Wire `EditApplier.apply` into `_execute_step` WRITE_FILE branch (via
      `_apply_edit`), preserving `_confirm_destructive_op` + `_snapshot_for_write`;
      reject → diagnostic step result, no write — satisfies R1.4, R2.2, R2.4.
- [x] 3. Add `ModelProfile.edit_format` + config plumbing + per-model resolver;
      default `whole_file` byte-identical — satisfies R3.1, R3.3, R3.4 and R3.2's
      *application* path. `ModelRouter.edit_format_for(model_name)` resolves
      config-override → profile-default → `whole_file`; `DevAgent` captures the
      plan model (`_active_plan_model`) and `_apply_edit` applies its format.
      Config block `edit_format_aci.per_model` in `~/.claude/ipad_bridge/config.json`.
      *The `_PLAN_PROMPT` per-format **rendering** (swapping the WRITE_FILE
      instruction block at infer time) lands with task 4 — there is no second
      instruction block to render until a structured format exists; whole_file's
      instruction is already in `_PLAN_PROMPT`.*
- [x] 4. Implement **hashline** end-to-end — `EditApplier._apply_hashline`
      (`@@ <OP> <line>:<hash>` ops), `hash_line`/`render_hashline`, layered matcher
      (exact line:hash → fuzzy ±5 with ambiguity rejection), atomic bottom-up
      batch with overlap rejection; READ_FILE renders anchors and the plan prompt
      gets `HASHLINE_PROMPT_INSTRUCTIONS` when the model uses hashline — satisfies
      R2.1, R2.3, R4.1–R4.3. (`udiff` implemented 2026-06-25 — same R4.2/R4.3
      contract; opt-in, `whole_file` still the default.)
- [x] 5. `tests/test_edit_format.py` — 32 tests covering R1.2/R1.3, R2.2, R3.1/R3.2/
      R3.3, R1.4, the validator-injection seam, and the full hashline surface
      (render, whitespace-insensitive hash, each op, stale/fuzzy/overlap/bottom-up,
      lint-after-apply, READ_FILE rendering).
- [x] 6. Eval: A/B `whole_file` vs structured on a SWE-bench-Verified-style subset;
      lock baseline. **Gate before flipping any default.** Shipped `evals/` `--mode
      edit_ab` (`evals/edit_ab.py` scorer + `runner.run_edit_ab_suite` +
      `evals/suites/edit_format.jsonl` over `evals/fixtures/edit_format/`): holds the
      model fixed, swaps only the format, applies through the REAL `EditApplier` +
      lint gate; deterministic model-free scoring (applied/lint/intent/preserved →
      correct, with a silent-elision detector). 10 CI-safe scorer tests
      (`tests/test_evals_edit_ab.py`). **Result (qwen3-coder:30b, n=5, baseline
      locked):** whole_file 100% vs hashline 100% (Δ0) — correctness *parity* — but
      hashline emits ~9.5× less (117 vs 1117 mean chars) at ~½ latency (625 vs
      1282 ms p50), 0 elision/lint/apply errors. The subset is small/easy (no
      whole_file elision occurred), so it shows the efficiency win, not yet hashline's
      correctness edge on long files. **The eval also caught + fixed an ACI defect:**
      qwen3-coder initially scored 0% on hashline because it copied the READ_FILE
      `lineno:hash|content` *render* into the op header (`@@ REPLACE 15:a9|code`);
      adding a worked example + "display only" warning to `HASHLINE_PROMPT_INSTRUCTIONS`
      took it 0%→100% (hashline-only injection; whole_file byte-identical).
      **Default NOT flipped — parity not a correctness win; recommend a larger,
      longer-file subset before enabling hashline per-model.**
      **Hard subset added + locked** (`evals/suites/edit_format_hard.jsonl` over 5
      long ~120-180-line fixtures, small targeted edits, `preserve` naming nearly
      every def to force the elision detector). **Result (qwen3-coder:30b, n=5,
      warm, baseline locked, tol 0.25):** whole_file **100%** vs hashline **80%**
      (Δ-0.2). Decisive: **(1) silent elision — whole_file's theorized weakness —
      did NOT occur on either subset (0/0) even at ~180 lines**; the 30B model
      re-emits long files faithfully. **(2)** hashline's single miss was a format
      slip (a stray trailing `85:00` half-anchor became insert content → invalid
      Python), **caught by the lint gate (fail-closed, no corruption)** — R1
      earning its keep. **(3)** hashline still ~23× less output (112 vs 2592 chars)
      at ~4× lower latency (720 vs 3075 ms). **Net gate verdict: keep whole_file
      default for qwen3-coder:30b — hashline is an efficiency win, not a correctness
      win, and is slightly more error-prone at the op format.** Enable hashline only
      where output cost/latency dominates, and only after the format reliability
      improves (more few-shot / a lint-reject retry — production already replans).
- [x] 7. Docs: added the **WRITE_FILE lint-gate + per-model edit-format** gotcha to
      `CLAUDE.md` (lint gate fail-closed, `edit_format_for` resolution, default
      `whole_file`, hashline opt-in, the task-6 gate verdict) and an
      `inference/edit_format.py` row to `docs/file-map.md`.
- [x] 8. **EDIT_FILE verb (R5)** — added `search_replace` format to
      `inference/edit_format.py` (`_parse_search_replace_blocks` +
      `EditApplier._apply_search_replace`, fail-closed on non-unique/stale SEARCH,
      reuses the `_lint` gate) and the `EDIT_FILE` verb to `inference/dev_agent.py`
      (in `_PLAN_ACTIONS`/`_STEP_PATTERN`/`_DESTRUCTIVE_VERBS`/`_FANOUT_SAFE_VERBS`;
      the WRITE_FILE dispatch branch now handles both, passing
      `SEARCH_REPLACE` as a format override to `_apply_edit`; saga compensation +
      surfacing + DAG de-collision extended to EDIT_FILE). `SEARCH_REPLACE_PROMPT_INSTRUCTIONS`
      injected into every plan context; `_PLAN_PROMPT` documents the verb.
      Satisfies R5.1–R5.6. **No model loaded; no DB schema change** (AGENTS.md #1/#6 N/A).
- [x] 9. `tests/test_edit_format.py` — 14 EDIT_FILE tests (unique-apply, multi-block,
      not-found/ambiguous fail-closed, broken-Python rejected pre-write, non-py
      unlinted, delete, empty-search creation-only, no-parseable-blocks, lenient
      markers, `_apply_edit` override forces format, `_execute_step` writes +
      snapshots, mismatch leaves file untouched, prompt instructions). 46 total pass.
      *Live `--mode edit_ab` search_replace arm (model-gated, run in the live
      Ollama env) is a follow-up — the deterministic applier contract is fully
      unit-covered.*
```
