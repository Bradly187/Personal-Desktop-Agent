# Tasks — B3: Align `DA_REPO_CONTEXT` / `DA_DELEGATE` defaults (Path B — ON)

> **Gate 2 — awaiting Brad's approval before any task executes.**
> Reference spec: `specs/bugfix-b3-flag-defaults/requirements.md`
> Decision: Path B — both features intentionally ON; align `flags.py` + docs to match.

---

## Tasks

- [x] 1. **`core/flags.py:60`** — change `DA_REPO_CONTEXT` registered default `"0"` → `"1"`:
  ```python
  # Before:
  _S("DA_REPO_CONTEXT", "bool", "0", "inject stable repo facts ahead of RAG"),
  # After:
  _S("DA_REPO_CONTEXT", "bool", "1", "inject stable repo facts ahead of RAG"),
  ```
  Satisfies R2.1, R3.1

- [x] 2. **`core/flags.py:61`** — change `DA_DELEGATE` registered default `"0"` → `"1"`:
  ```python
  # Before:
  _S("DA_DELEGATE", "bool", "0", "planner [DELEGATE q] read-only sub-agent"),
  # After:
  _S("DA_DELEGATE", "bool", "1", "planner [DELEGATE q] read-only sub-agent"),
  ```
  Satisfies R2.1, R3.1

- [x] 3. **`inference/dev_agent.py:285-292`** — update comment from "Default OFF … until eval baseline locks" to "Default ON":
  - Remove: `"Default OFF (DA_REPO_CONTEXT) until the eval baseline locks; off == byte-identical."`
  - Replace with: `"Default ON (DA_REPO_CONTEXT). Set to 0 to disable for debugging."`
  Satisfies R1.4

- [x] 4. **`inference/dev_agent.py:299-307`** — update comment from "Default OFF … until eval baseline locks" to "Default ON":
  - Remove: `"Default OFF (DA_DELEGATE) until the eval baseline locks; off == the verb is absent from the planner vocabulary and a stray DELEGATE is a safe no-op."`
  - Replace with: `"Default ON (DA_DELEGATE). Set to 0 to disable. A stray DELEGATE with flag OFF is a safe no-op."`
  Satisfies R1.4

- [x] 5. **`CLAUDE.md:157`** — update `DA_REPO_CONTEXT` row from `OFF` → `ON`:
  ```
  | `DA_REPO_CONTEXT` | ON | Inject stable repo facts (AGENTS.md/CLAUDE.md, layout, git) ahead of RAG | - | `specs/repo-context-ingestion/` |
  ```
  Satisfies R1.5

- [x] 6. **`CLAUDE.md:161`** — update `DA_DELEGATE` row from `OFF` → `ON`:
  ```
  | `DA_DELEGATE` | ON | Planner `[DELEGATE q]`: bounded read-only sub-agent investigation | - | `specs/dev-agent-delegate-verb/` |
  ```
  Satisfies R1.5

- [x] 7. **Run eval baseline locks** — per `running-the-eval-harness` skill, run and lock baselines for:
  - `evals/suites/` suite covering repo-context injection (if exists)
  - `evals/suites/` suite covering delegate verb routing (if exists)
  - If no suites exist yet, record that baseline lock is deferred pending suite creation.
  Satisfies R2.2

  **Result (2026-07-10, DA_DELEGATE=1, qwen3-coder:30b, Ollama live):**
  - **Delegate verb routing — LOCKED.** `dev_delegate` (`--mode trajectory`)
    baseline written to `evals/baselines/dev_delegate.json`: n=3,
    `exact_acc=0.3333`, `mean_score=0.6667`, **`safe_acc=1.0`** (the safety gate),
    errors=0. `--check` passes (exit 0). Finding: under the constrained plan
    grammar (`_PLAN_JSON_SCHEMA`) the 30B planner under-uses the DELEGATE verb
    (1/3 cases emit it) — a model-behavior signal, not a B3 regression; the flag
    flip only makes the teaching available. Tracked as a separate planner-quality
    item, not a blocker on the ON decision.
  - **Repo-context injection — DEFERRED (no dedicated suite).** `DA_REPO_CONTEXT`
    injects static repo facts inside the live agent's `_plan_and_run_locked`, which
    the prompt-only trajectory eval never reaches; its grounding effect is already
    covered by the existing `rag_ablation` gate (`mean_delta`) and `--mode
    execution`. A dedicated DA_REPO_CONTEXT A/B suite is deferred pending suite
    creation, per this task's own escape hatch.

- [x] 8. **Unit tests** — assert that with env-var unset, both booleans are `True`.

- [x] 9. **Run full test suite** — `pytest -x`; confirm green.
