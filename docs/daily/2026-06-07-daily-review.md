# Daily Review — 2026-06-07

*(Covers the 2026-06-07 work sessions — the Gemma 4 general-slot decision and the
RAG/KB audit + remediation — plus the automated housekeeping pass that produced
this document.)*

## Summary

Two work streams landed on 2026-06-07, both branching off the orchestration-
hardening line:

1. **Gemma 4 general slot** (`7076c24`) — moved the GENERAL specialist to
   `gemma4:12b` (fits resident, kills eviction churn), with `e4b-it-qat` as the
   flare-day fallback. Code/plan consolidation onto Gemma 4 was evaluated and
   rejected (thinking-tax = ~4× latency) — `qwen3-coder:30b` stays.

2. **RAG/KB audit + remediation** — a deep audit of the two-tier knowledge store
   (ChromaDB/MiniLM vector tier; AgentDB SQLite + DuckDB + append-only `audit.db`
   structured tier, unified by `MemoryManager`), followed by a 5-commit scoped
   remediation on branch **`feat/rag-kb-remediation`** (off
   `feat/gemma4-general-slot`, tip `633164d`). Full suite **761 passed**. The
   branch is **not yet pushed and has no PR**.

The headline find was a **dead pain-day signal**: the highest-weighted flare
signal in the behavioral twin was never being fed, so command failures never
influenced pain-day detection at all.

---

## Stream 1 — Gemma 4 general slot (`7076c24`, on `feat/gemma4-general-slot`)

- `feat(inference)` — GENERAL slot → `gemma4:12b`; reusable model-eval suites.
- Phase-1 verdict (see `project/gemma4_general_slot_plan.md`): general→gemma4:12b
  **GO** (resident fit, no eviction churn), `e4b-it-qat` flare fallback;
  code/plan consolidation **NO-GO** (gemma4 thinking-tax ≈ 4× latency, 8–12k
  tokens) — keep `qwen3-coder:30b`. Ollama `gemma4 think=true` validated
  (qwen3-coder rejects it).

---

## Stream 2 — RAG/KB remediation (`feat/rag-kb-remediation`, 5 commits)

### `fix(rag)` — embedder kwarg + cosine pin `5985e80`
- `CodebaseIndexer` now accepts `embedder=` and pins the ChromaDB collection to
  cosine space. The missing kwarg had been **silently disabling code-RAG** on the
  `--index-codebase` path: the `TypeError` was swallowed by `main.py`'s
  try/except, leaving `indexer=None`.

### `fix(twin,memory)` — dead pain-day fail signal + facade gaps `3707d22`
- **The pain-day fail signal was dead.** `_session_fail_count` was the
  highest-weighted pain-day signal (0.30) and was logged as `fail_ratio`, but was
  **never incremented** — failures never reached the twin. Fixed with a *separate*
  `BehavioralTwinState.record_failure()` / `ContinuousTrainer.record_failure()` /
  coordinator failure branch. **Invariant:** the failure path updates counters
  ONLY — it must never touch few-shot / `PreferenceModel` / `SemanticMemory`,
  which are success-biased.
- `gesture_conf_delta` / `cmd_rate_delta` were hard-coded `0.0` in the
  `twin_pain_day_log` write — now carry real values.
- `MemoryManager` fixes: rewired `_VALID_KEYS` (sensor_telemetry / voice_profile
  now dispatch; `session_event` removed), `read_context` namespace consistency,
  DevAgent `_db()` seam.

### `refactor(kb)` — cosine SemanticMemory + accurate labels `3124c47`
- `SemanticMemory` `behavioral_memory` collection pinned L2 → cosine to match the
  indexer; query results now carry a `score` (= 1 − distance) for caller parity.
- `TwinSnapshot.command_count_today` → `command_count_session` (it was always a
  per-session counter that resets on construct, never a per-calendar-day count;
  only tests consumed it).
- Doc fixes: `db.py` schema comment "12 tables" → "29 tables"; `SessionAnalyzer`
  docstring corrected to the real `analyze_session()` / `run_and_persist()`
  entry points (it advertised a nonexistent async `analyze()`).

### `feat(kb)` — robustness `633164d`
- **Versioned migrations**: `AgentDB._migrate` + `PRAGMA user_version` so the
  additive-ALTER batch runs at most once per DB; `except` narrowed to the
  duplicate-column case (a genuine DDL error is logged, not swallowed).
- **Chunk sub-splitting**: oversized units split into `_(i/N)` sub-chunks instead
  of hard-truncating at 4000 chars, so large functions / classes / PDF pages keep
  their tail for retrieval.
- **Watcher debounce**: per-path `WATCH_DEBOUNCE_S` coalesces a rapid save burst
  into one re-index; timers cancelled on `stop_watching()`.
- **Re-probe**: time-gated `_available` re-probe on the query path of both
  `CodebaseIndexer` and `SemanticMemory`, retried at most once per
  `REPROBE_INTERVAL_S` and gated on `_started_once` (never-started instances stay
  in Jaccard fallback — preserves existing test behaviour).
- Adds `tests/test_kb_robustness.py` (19) + a pain-day fail-ratio e2e test.

### `docs(db)` — ER diagrams + stale-claim fixes `45c0283`
- Added §11 "Entity-Relationship Diagrams" to
  `docs/architecture/database-design.md`: 9 Mermaid diagrams (all-29-table
  overview, six attributed agent.db group diagrams, the DuckDB analytics store,
  the append-only audit.db) + §12 two-tier knowledge-store note. All
  Mermaid-validated.
- Fixed stale claims: `few_shot_examples.embedding` is no longer "always NULL /
  Jaccard only" (it is MiniLM-cosine with incremental backfill); table count
  corrected to **29**.

### ChromaDB rebuilt under cosine
- The cosine pin only applies to NEW collections, so the live `./chroma_db` was
  backed up to `%TEMP%\chroma_db.bak-pre-cosine` and rebuilt. Verified
  2026-06-07: `codebase` = 1937 chunks, `documents` = 128 pages, both
  `hnsw.space = cosine`. `behavioral_memory` recreates under cosine on next
  `twin.start()`.

### Two-tier embedding gotcha (don't conflate)
The SQLite `few_shot_examples.embedding` BLOB store (MiniLM cosine + Jaccard
fallback, computed in numpy) is the **per-command prompt hot path**; the ChromaDB
`behavioral_memory` collection is the **twin context layer**. Same model, two
independent paths.

---

## Housekeeping performed (2026-06-07, automated pass)

This document was produced by the daily code-review / housekeeping scheduled task.
The previous session left `CLAUDE.md` current only through 2026-06-06, so the
following stale references were corrected:

| Location | Was | Now |
|----------|-----|-----|
| `CLAUDE.md` Current Status header | dated 2026-06-06; no 06-07 work | dated 2026-06-07; added Gemma 4 + RAG/KB entries |
| `CLAUDE.md` `db.py` ipad_logs note | "**32 AgentDB tables**" | "**29 AgentDB tables**" (the 3 `benchmark_*` tables belong to DuckDB `AnalyticsDB`) |
| `CLAUDE.md` Key Files `storage/db.py` row | "32 tables" | "29 tables; versioned via `PRAGMA user_version`" + AnalyticsDB = 3 tables |
| `CLAUDE.md` Test suite line | "673 fns / 60 files (2026-06-06)" | "714 fns / 62 files; 761 passed (2026-06-07)" |
| `CLAUDE.md` `semantic_memory.py` row | no cosine/score/re-probe | added cosine space, `score` key, `_available` re-probe |
| `CLAUDE.md` `codebase_indexer.py` row | no embedder/chunk/debounce | added `embedder=`, `_(i/N)` sub-split, debounce, re-probe |

### Verification done during the pass
- **Table count**: `storage/db.py` has 32 real `CREATE TABLE` statements; 3 are
  DuckDB `benchmark_*` (defined under `_ANALYTICS_SCHEMA` at line 2145) → **29
  agent.db tables**, confirming the recent docs and contradicting CLAUDE.md's old
  "32 AgentDB" figure.
- **Test counts**: 62 `tests/test_*.py` files, 714 `def test_` functions
  (static); 761 passed when run (incl. parametrization, per the remediation
  commit).
- **ChromaDB**: both live collections confirmed `space = cosine`
  (`codebase` 1937, `documents` 128).
- **Repo artifacts**: no stray `.bak`/`.tmp` files committed; the cosine-rebuild
  backup correctly lives in `%TEMP%`, outside the repo.

### Stale references flagged but NOT auto-edited (need a decision)
- **`.kiro/specs/behavioral-twin-state/` (design.md, requirements.md)** still
  document the `TwinSnapshot` field as `command_count_today: int  # Commands
  executed today`. The implementation was renamed to `command_count_session` in
  `3124c47` because the counter was always per-session, never per-day. This is a
  **spec-vs-implementation divergence**: the spec captured an original per-day
  *intent*; the code matches per-session *reality*. Resolving it is a product
  decision (keep per-session and update the spec, or implement a true per-day
  count), so it was left for review rather than silently rewritten.

### Follow-ups noted (not acted on)
- The cosine reindex reported `errors=28` with no captured per-file warnings
  (likely worker-pool embedding hiccups). Index queries correctly, but worth a
  glance if the indexer is re-run.
- **Deferred security (Tranche 4)**: `audit.db` has no DROP-TABLE / hash-chaining
  protection; `remote_indexer_service.py` is plaintext `0.0.0.0:9000` no-auth.
  Acceptable for the current single-user home-LAN trust boundary; revisit if that
  changes.
- **`feat/rag-kb-remediation` is unpushed with no PR** — it sits on top of
  `feat/gemma4-general-slot`. Both still need to be pushed and merged toward
  `master`.

---

## State at end of day

- **Branch**: `feat/rag-kb-remediation` (tip `633164d`, off `feat/gemma4-general-slot`).
- **Tests**: 761 passed.
- **Working tree**: clean (only this housekeeping pass touched `CLAUDE.md` and
  added this review file).
- **Open PRs**: #32 (`fix/tilt-tap-click`, orchestration hardening) remains the
  pushed line; the two 06-07 branches are local-only.
