# Spec: Retrieval-Quality Eval (MRR/Hit@K + synthetic ground truth)

---

## 1. Background — the "Why"

Retriever quality is currently measured only end-to-end: `rag_ablation` scores
how much grounding the RAG index adds to a live DevAgent answer. That catches
"RAG stopped helping" but cannot localize *why* — a silent embedder/index
regression (the 2026-06-07 embedder-bug class cited in the `rag_ablation`
baseline note) is indistinguishable from a model change. This spec adds a
retriever-isolating eval: given a question with a known target chunk, does the
persisted ChromaDB index rank that chunk in the top-K? Ground truth is
generated locally by sampling real indexed chunks and asking the local Ollama
model to write questions those chunks answer. This adapts the useful kernel of
an external "production agentic architecture" handoff whose infrastructure
recommendations (PostgreSQL+pgvector, SQS/Lambda, deepeval) were rejected —
see `docs/decisions.md` D028. Related: `../repo-context-ingestion/`.

**Status:** Shipped
**Approved:** Brad, 2026-07-03 (plan-mode approval of the implementing plan — both gates)
**Owner / author session:** Claude Code

---

## 2. Glossary

- **RetrievalCase**: one eval case — a question plus the chunk that should
  answer it, identified by `(target_file, target_names)` hit metadata.
- **Target chunk**: a chunk in the `codebase` (or `documents`) ChromaDB
  collection, produced by `inference/codebase_indexer.py`.
- **MRR**: mean reciprocal rank of the target across cases (1/rank; 0 when the
  target is outside the retrieval window). Windowed at top-10, so effectively
  MRR@10.
- **Hit@K**: fraction of cases whose target ranks ≤ K (default K=5 — the `n`
  DevAgent actually retrieves with in production).
- **Self-retrieval filter**: generation-time quality gate — a synthetic
  question is kept only if its own source file ranks in the top-10 for it.

---

## 3. Requirements (EARS acceptance criteria)

### Requirement 1: Retrieval-rank eval mode

**User Story:** As Brad, I want a fast, model-free gate on retriever quality,
so that a broken embedder or index shows up before it silently degrades every
dev-agent answer.

#### Acceptance Criteria
1. THE retrieval eval SHALL match the target chunk by hit metadata
   `(file, name)` — never raw chroma ids (ids hash the file mtime and change
   on every reindex). Name matching SHALL tolerate the `_(i/N)`
   oversized-chunk suffix and SHALL use `.get("name")` so documents-collection
   hits (which carry `page`, not `name`) never raise.
2. IF a case's `target_file` is absent from its collection, THEN THE runner
   SHALL record the case as an **error** (stale suite, surfaced in the report)
   — not a rank-0 miss that silently drags MRR down.
3. IF ChromaDB or the codebase collection is unavailable or empty, THEN THE
   runner SHALL exit 2 (safe skip, the shared `evals/run.py` convention) and
   SHALL NOT write a baseline.
4. THE report SHALL gate on MRR (aliased to `exact_acc` per the
   `rag_ablation`→`mean_delta` precedent) and SHALL report Hit@K, found-rate,
   p50 latency, and error count alongside.

### Requirement 2: Local synthetic ground-truth generation

**User Story:** As Brad, I want the ground-truth suite generated from my real
index by my local model, so that the eval reflects this repository and nothing
leaves the machine.

#### Acceptance Criteria
1. THE generator SHALL run entirely locally (Ollama; default `llama3.1:8b`) —
   no cloud calls.
2. THE generator SHALL sample at most one chunk per file (stratified,
   seeded), SHALL prefer function/class/method chunks, and SHALL skip chunks
   shorter than a threshold or tainted by the index-time trust classifier.
3. THE generator SHALL keep a question only if its source file self-retrieves
   in the top-10, recording the observed rank as `provenance.gen_rank`.
   Emitted synthetic cases SHALL use file-level targets (`target_names: []`)
   so the gate matches at the same granularity the filter validated —
   otherwise a question that retrieves the right file via a sibling chunk
   ships as a guaranteed miss. Name-level targets remain supported for
   hand-curated cases; the source chunk name is kept in
   `provenance.chunk_name`.
4. FOR ALL emitted cases, THE generator SHALL record provenance
   (`generator_model`, `chunk_sha16` over the stored document text,
   `generated_at`) so content drift after a reindex is detectable.

---

## 4. Technical Design

- **Entry point:** `python -m evals.run --suite retrieval_synthetic --mode retrieval`
  (new mode in `evals/run.py`; scoring in `evals/retrieval.py`).
- **New `Command` fields:** none — this feature never touches the runtime
  pipeline. No `DA_*` flag either: an eval suite + generation script is not
  runtime behavior.
- **Models / VRAM:** eval scoring is model-free (embedding lookup only, via
  the persisted index). Generation uses `llama3.1:8b` (already resident) on
  demand, plus the MiniLM embedder chroma already loads.
- **Persistence:** none (`agent.db` untouched). Suite at
  `evals/suites/retrieval_synthetic.jsonl`; baseline at
  `evals/baselines/retrieval_synthetic.json` (tolerance 0.10 — wider than the
  0.05 default because the index legitimately churns with the code; narrower
  than rag_ablation's 0.15 because scoring is deterministic).
- **Known trade-off (selection bias, by design):** the self-retrieval filter
  admits only questions the *current* retriever already answers at rank ≤ 10.
  The suite is therefore a **regression gate**, not an absolute-quality
  benchmark — it starts healthy and alarms on decay.
- **Staleness policy:** deleted/renamed files surface as errors (R1.2);
  regenerate the suite when errors exceed ~10% of n (the regeneration command
  lives in the suite's header comment).

---

## 5. Behavior Verification (executable, not prose)

- **Eval suite:** `evals/suites/retrieval_synthetic.jsonl`, baseline locked in
  `evals/baselines/retrieval_synthetic.json`; gated in `scripts/run_evals.ps1`
  Tier 2 (model-backed slot so a missing index → SKIP, never a blocked push).
- **Unit tests:** `tests/test_evals_retrieval.py` — model-free, one or more
  assertions per criterion above (R1.1 matcher incl. suffix + docs-hit cases;
  R1.2 stale-target error; R1.3 None-factory skip; R1.4 metric math and
  `exact_acc` alias; R2.x shipped-suite well-formedness).

---

## 6. Tasks

- [x] 1. `evals/retrieval.py` — cases, matcher, MRR/Hit@K scoring, loader, runner — R1.1, R1.4
- [x] 2. `evals/run.py` — `--mode retrieval`, index precondition/skip, stale-target guard — R1.2, R1.3
- [x] 3. `tests/test_evals_retrieval.py` — model-free CI tests — all R1.x
- [x] 4. `scripts/generate_retrieval_eval_data.py` — sampling, generation, self-retrieval filter — R2.x
- [x] 5. Generate suite (46 cases, llama3.1:8b), spot-review, lock baseline
       (MRR 0.7535, tolerance 0.10)
- [x] 6. Wire `scripts/run_evals.ps1` (Tier-1 test file + Tier-2 gate); D028; `evals/README.md`
