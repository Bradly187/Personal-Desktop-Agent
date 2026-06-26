# Spec: Live Repo-Context Ingestion (Gap A)

> One feature → one folder. This spec is the source of truth; the code is the
> build artifact. Design and Tasks are kept inline (§4–§6) until they outgrow
> the file. Keep this updated as the design evolves.

---

## 1. Background — the "Why"

Gap analysis (2026-06-26) of PDA's dev-agent against `rasbt/mini-coding-agent`'s
six "coding-harness hygiene" components found component **1 (Live repo context)**
the weakest, lowest-risk gap. The reference agent's `WorkspaceContext.build()`
runs once at startup: `git status`/`git log`, current+default branch, and reads
`AGENTS.md`/`README.md`/`pyproject.toml` (clipped ~1200 chars each) into a stable
block handed to the model.

PDA today injects only *dynamic* context per plan: `DevAgent._plan_and_run_locked`
(`inference/dev_agent.py` ≈L892–933) builds `extra_ctx` from `_format_context`
(recent runtime entries), `_rag_context` (goal-keyed RAG over `CodebaseIndexer`),
and `_git_context` (`git status --short --branch`). It **never** ingests
`AGENTS.md`/`CLAUDE.md` (the 10 behavioral rules + file map the planner is
supposed to obey) and has **no repo-layout snapshot**. The planner therefore
plans against the codebase blind to its own house rules and topology.

Fixing this also feeds component **2 (prompt cache reuse)**: the repo facts are
*stable for the whole session*, so they belong in the cacheable system prefix
(`inference/cloud_dev_agent.py` marks the per-domain system prompt
`cache_control: ephemeral` at ≈L263–271). A stable workspace block placed there
is a cache win, not just a quality win.

**Status:** In Progress — `inference/workspace_context.py` + DevAgent memoized
injection (`_workspace_context`/`invalidate_workspace_context`) + flag
`DA_REPO_CONTEXT` shipped; 9 unit tests green. **Quality path (R3.1) done via
`extra_ctx`.** R3.2 (cloud cache as a *separate* breakpoint) is implemented as the
stable block leading `extra_ctx` (rides the existing cacheable prefix); lifting it
into a dedicated `cache_control` system segment is deferred (the cloud cache is a
documented no-op below the 2048-token min anyway). Eval baseline (task 5) pending.
**Owner / author session:** Claude Code (Opus 4.8)
**Related:** `../accessibility-agent/` (DevAgent), `../trajectory-reduction/`
(the dynamic-context counterpart this complements), `../first-class-search-tools/`
(shares `CodebaseIndexer`). Honors AGENTS.md #4 (degrade gracefully), #7 (path
boundaries — read only inside the repo root), #9 (AGENTS.md *is* the rules being
ingested).

---

## 2. Glossary

- **WorkspaceContext**: the new component this spec introduces
  (`inference/workspace_context.py`). A pure builder that collects *stable* repo
  facts into one clipped, deterministic text block. No LLM call.
- **Stable facts**: things that do not change within a session — repo root, the
  top-level layout, the contents of `AGENTS.md`/`CLAUDE.md`/`README.md`/manifest,
  the current+default branch, recent commit subjects. (The *working-tree diff* is
  NOT stable — it stays in the dynamic `_git_context` path.)
- **`extra_ctx`**: the existing per-plan context string assembled in
  `_plan_and_run_locked` and passed as `context=` to `ModelRouter.infer`.
- **Cacheable prefix**: the system-prompt content marked `cache_control:
  ephemeral` on the Anthropic/Bedrock path (`cloud_dev_agent.py`). Local
  (Ollama/vLLM) paths concatenate flat (`model_router.py` ≈L1111) — no native
  caching, so the win there is purely quality.

---

## 3. Requirements (EARS acceptance criteria)

### Requirement 1: Collect stable repo facts

**User Story:** As Brad, I want the planner to know my repo's layout and its own
AGENTS.md/CLAUDE.md rules before it plans, so it stops violating house rules and
re-deriving topology it could have been told.

#### Acceptance Criteria
1. THE `WorkspaceContext.build(repo_root)` SHALL collect, deterministically and
   with no LLM call: (a) current branch + default branch; (b) `git log --oneline`
   for the last 5 commits; (c) `git status --short` summary count (not the full
   diff — that stays dynamic); (d) a one-level repo layout (top-level dirs + key
   manifest files); (e) clipped excerpts (≤1200 chars each) of `AGENTS.md`,
   `CLAUDE.md`, `README.md`, and the first present of
   `pyproject.toml`/`requirements.txt`/`package.json`.
2. THE builder SHALL clip every component to its char budget via the same
   `clip`-style truncation marker used elsewhere, and SHALL cap the assembled
   block at `max_chars` (default 6000).
3. THE builder SHALL read ONLY paths resolving inside `repo_root`
   (AGENTS.md #7) — a symlink or `..` escaping the root SHALL be skipped, not
   followed.

### Requirement 2: Build once, cache for the session

**User Story:** As Brad, I don't want this re-scanned on every plan — it's stable.

#### Acceptance Criteria
1. THE `WorkspaceContext` SHALL be built at most once per `DevAgent` instance and
   memoized; subsequent plans SHALL reuse the cached block.
2. THE cache SHALL expose an explicit `invalidate()` so a long-lived session can
   refresh after a branch switch or a CLAUDE.md edit; nothing SHALL auto-refresh
   on the 60 Hz path (AGENTS.md #2 — this is a dev-agent-only, off-loop concern).

### Requirement 3: Inject as a stable, cache-friendly block

#### Acceptance Criteria
1. THE workspace block SHALL be prepended to the plan context AHEAD of the
   dynamic RAG/git-status portions, so the stable text leads.
2. ON the cloud path, the workspace block SHALL be eligible to ride in the
   `cache_control: ephemeral` prefix (component 2), NOT re-sent as fresh dynamic
   content each call. WHERE the SDK supports multiple cache breakpoints, the
   workspace block MAY be its own breakpoint after the per-domain system prompt.
3. THE existing `_git_context` working-tree diff SHALL remain in the dynamic path
   and SHALL NOT be duplicated by the stable block (branch/log live in stable;
   uncommitted diff lives in dynamic).

### Requirement 4: Safe degradation

#### Acceptance Criteria
1. IF `repo_root` is not a git repo, THEN THE builder SHALL omit the git facts and
   still return the file/layout facts — never raise. (AGENTS.md #4.)
2. IF a source file is missing/unreadable, THEN THE builder SHALL skip just that
   excerpt and continue.
3. IF the whole build fails, THEN `_plan_and_run_locked` SHALL proceed with the
   existing dynamic context exactly as today (the feature is purely additive).
4. THE feature SHALL be controlled by a flag `DA_REPO_CONTEXT`, default **off**
   until the eval baseline (§5) is recorded; WHILE off, plan prompts SHALL be
   byte-identical to today.

---

## 4. Technical Design

- **Entry point / pipeline boundary:** `inference/dev_agent.py
  ::_plan_and_run_locked` (≈L892, where `extra_ctx` is assembled). No new verb, no
  coordinator/gate change.
- **New component:** `inference/workspace_context.py` — pure, no DevAgent import.

  ```python
  def build_workspace_context(
      repo_root: str,
      *,
      max_chars: int = 6000,
      per_file_chars: int = 1200,
      log_count: int = 5,
  ) -> tuple[str, dict]:
      """Return (block_text, stats). Deterministic; reads only inside repo_root;
      git/file failures degrade to omission. stats = {has_git, files_read,
      chars_out, truncated}."""
  ```

  A thin `WorkspaceContext` holder on `DevAgent` memoizes the result (R2) and
  offers `invalidate()`.
- **Injection:** prepend `block_text` to `extra_ctx` in `_plan_and_run_locked`
  BEFORE the RAG/git-status concatenation (R3.1). Gated on `DA_REPO_CONTEXT`.
- **Cache wiring (R3.2):** `cloud_dev_agent.py` already marks the system prompt
  cacheable; pass the workspace block through as a stable second segment so it is
  not billed as fresh input every call. Local path just concatenates (no caching
  available there).
- **New `Command`/`AgentStep` fields:** none.
- **Models / VRAM:** none added — this *shrinks* effective re-sent tokens on the
  cloud path and adds planner grounding locally. No `ResourceGovernor` change
  (AGENTS.md #6 unaffected).
- **Persistence:** none. No `agent.db` schema change, **no `PRAGMA user_version`
  bump** (AGENTS.md #1).
- **Cross-platform:** none — does not touch `core/ipad_bridge.py` (AGENTS.md #3
  N/A).

### Configuration (flat YAML)

```yaml
repo_context:
  enabled: false          # env DA_REPO_CONTEXT; default off until eval baseline locks
  max_chars: 6000         # cap on the whole stable workspace block
  per_file_chars: 1200    # clip per ingested doc (AGENTS/CLAUDE/README/manifest)
  log_count: 5            # recent commit subjects to include
  ingest:                 # stable docs to excerpt, in order
    - AGENTS.md
    - CLAUDE.md
    - README.md
```

---

## 5. Behavior Verification (executable, not prose)

- **Unit tests:** `tests/test_workspace_context.py`, one assertion per criterion
  (cite the criterion in the test name), e.g.:
  - `test_r1_1_collects_git_and_docs` (fixture repo with a fake `.git`)
  - `test_r1_3_skips_paths_outside_root` (symlink/`..` escape is omitted)
  - `test_r2_1_built_once_memoized`
  - `test_r4_1_non_git_dir_degrades` (no `.git` → file facts only, no raise)
  - `test_r4_4_disabled_is_byte_identical` (golden: `extra_ctx` unchanged when off)
- **Eval suite:** add ≥3 cases to an existing dev-agent plan suite
  (`evals/suites/router_domains.jsonl` or `dev_trajectory`) whose correct plan
  depends on a house rule that lives only in AGENTS.md (e.g. "a schema change
  needs a `user_version` bump") — score whether the plan honors it with the block
  present. Lock the baseline per `running-the-eval-harness`. Recommend the flag
  flip only if the gate improves or holds with no regression.

Each acceptance criterion in §3 maps to ≥1 test or eval case above.

---

## 6. Tasks

- [x] 1. Add `inference/workspace_context.py` with `build_workspace_context()` —
      satisfies R1.1–R1.3, R2, R4.1–R4.2.
- [x] 2. Memoize on `DevAgent` (`_workspace_context`) + `invalidate_workspace_context()`;
      prepend to `extra_ctx` in `_plan_and_run_locked`, flag-gated (`DA_REPO_CONTEXT`)
      — R2.1, R3.1, R3.3, R4.4.
- [~] 3. Cloud prefix: stable block leads `extra_ctx` (rides existing cacheable
      prefix). Dedicated `cache_control` system breakpoint DEFERRED (no-op below the
      2048-token cache min) — R3.2 partial.
- [x] 4. `tests/test_workspace_context.py` (9 tests; symlink-escape skips without
      privilege on Windows) — R1–R4.
- [~] 5. Eval cases ADDED — TWO suites: (a) prompt-only `evals/suites/repo_context.jsonl`
      (3 cases; fixture `<workspace-context>` block in the case `context`; scored
      `--mode trajectory`); (b) END-TO-END `evals/suites/repo_context_exec.jsonl` (3
      read-only cases run through the LIVE `plan_and_run` via `--mode execution`, so the
      REAL `build_workspace_context()` injects the actual AGENTS.md/CLAUDE.md —
      `DA_REPO_CONTEXT=1 … --mode execution`; A/B vs `=0`). `_run_execution` logs the flag.
      Both load-verified; live DevAgent confirmed to build a 6 KB block from the real repo.
      NOTE: exec verb-scoring gates SAFETY/read-only discipline, not answer groundedness
      (a judge eval is the better measure — flagged in the suite header). Baseline lock
      pending a model run — §5.
- [ ] 6. **DECISION (Brad):** flip `DA_REPO_CONTEXT` default on if the gate holds.
- [ ] 7. Update `CLAUDE.md` Known Gotchas (new flag) + Key Files if the surface changed.
