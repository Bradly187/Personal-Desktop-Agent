"""Retrieval-rank eval — does the ChromaDB retriever surface the right chunk?

Ranks a known-target chunk for each question directly against the persisted
index (no DevAgent, no generation), complementing the end-to-end rag_ablation
suite: ablation measures whether RAG improves the *answer* (grounding delta);
this measures whether the *retriever* puts the right chunk in front of the
model at all. A silent embedder/index regression (the 2026-06-07 embedder-bug
class) shows up here first, model-free and in seconds.

Targets are matched on hit METADATA `(file, name)` — never raw chroma ids,
which hash the file mtime (`codebase_indexer._make_id`) and therefore change
on every reindex. Name matching tolerates the `_(i/N)` suffix that oversized
chunks receive, and degrades to file-level matching when `target_names` is
empty (documents-collection hits carry `page`, not `name`).

Metrics: MRR (gated — aliased to `exact_acc`, the rag_ablation precedent) and
Hit@K reported alongside. Retrieval is windowed at `top_n` (default 10), so
the headline is effectively MRR@10: a target ranked below the window scores
0.0, same as not retrieved. NDCG is deliberately absent — with one relevant
chunk per case it is a monotone transform of MRR and adds no signal.

Spec: specs/retrieval-quality-eval/  |  Decision: docs/decisions.md D028
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median

_SUITES_DIR = Path(__file__).parent / "suites"

_VALID_COLLECTIONS = ("codebase", "documents", "combined")


@dataclass
class RetrievalCase:
    """One question + the chunk the retriever should rank highly."""
    id: str
    suite: str
    question: str
    target_file: str                                   # hit metadata "file" (rel path)
    target_names: list[str] = field(default_factory=list)  # chunk names; [] = file-level
    collection: str = "codebase"                       # codebase | documents | combined
    k: int = 5                                         # Hit@K horizon
    source: str = "synthetic"                          # synthetic | curated
    provenance: dict = field(default_factory=dict)     # generator_model, chunk_sha16, gen_rank, generated_at
    tags: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "RetrievalCase":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class RetrievalResult:
    case_id: str
    rank: int = 0                 # 1-based rank of the first matching hit; 0 = not in window
    reciprocal_rank: float = 0.0  # 1/rank, 0.0 when not found
    hit_at_k: bool = False        # rank > 0 and rank <= case.k
    n_retrieved: int = 0
    latency_ms: float = 0.0
    error: str = ""
    detail: str = ""


@dataclass
class RetrievalReport:
    n: int
    mrr: float
    hit_rate: float        # mean Hit@K over valid cases
    found_rate: float      # target anywhere in the retrieved window
    p50_latency_ms: float
    errors: int
    results: list = field(default_factory=list)

    @property
    def failures(self) -> list:
        return [r for r in self.results if r.error or not r.hit_at_k]

    # exact_acc aliases MRR so the shared --check gate guards "the retriever
    # keeps ranking targets at least baseline − tolerance" (rag_ablation precedent).
    @property
    def exact_acc(self) -> float:
        return self.mrr

    def metrics(self) -> dict:
        return {
            "n": self.n,
            "mrr": round(self.mrr, 4),
            "exact_acc": round(self.mrr, 4),   # gated metric
            "hit_rate": round(self.hit_rate, 4),
            "found_rate": round(self.found_rate, 4),
            "p50_latency_ms": round(self.p50_latency_ms, 1),
            "errors": self.errors,
        }

    def summary(self) -> str:
        # ASCII-only (Windows cp1252 consoles can't encode Greek/arrows).
        return (f"n={self.n}  MRR={self.mrr:.3f}  hit_rate={self.hit_rate:.1%}  "
                f"found={self.found_rate:.1%}  p50={self.p50_latency_ms:.0f}ms  "
                f"errors={self.errors}")


def hit_matches(case: RetrievalCase, hit: dict) -> bool:
    """Does a retrieved hit satisfy the case's target?

    File equality is mandatory. With target_names set, the hit's `name` must
    equal one of them or be an `_(i/N)` split of one (oversized-chunk suffix
    from `codebase_indexer._emit_chunks`). Documents hits have no `name`, so
    named targets never match them — use file-level targets for PDFs.
    """
    if hit.get("file") != case.target_file:
        return False
    if not case.target_names:
        return True
    name = hit.get("name")
    if not name:
        return False
    return any(name == t or name.startswith(f"{t}_(") for t in case.target_names)


def rank_of_target(case: RetrievalCase, hits: list[dict]) -> int:
    """1-based rank of the first matching hit; 0 when absent from the window."""
    for i, hit in enumerate(hits, start=1):
        if hit_matches(case, hit):
            return i
    return 0


def score_retrieval(case: RetrievalCase, hits: list[dict], *,
                    latency_ms: float = 0.0, error: str = "") -> RetrievalResult:
    if error:
        return RetrievalResult(case_id=case.id, latency_ms=latency_ms, error=error)
    rank = rank_of_target(case, hits)
    rr = 1.0 / rank if rank else 0.0
    top = hits[0].get("file", "?") if hits else "-"
    return RetrievalResult(
        case_id=case.id, rank=rank, reciprocal_rank=rr,
        hit_at_k=bool(rank and rank <= case.k), n_retrieved=len(hits),
        latency_ms=latency_ms,
        detail=f"target {case.target_file} rank={rank or 'miss'}; top hit {top}",
    )


def aggregate_retrieval(results: list) -> RetrievalReport:
    n = len(results)
    if n == 0:
        return RetrievalReport(0, 0.0, 0.0, 0.0, 0.0, 0)
    valid = [r for r in results if not r.error]
    mrr = sum(r.reciprocal_rank for r in valid) / len(valid) if valid else 0.0
    hit_rate = sum(1 for r in valid if r.hit_at_k) / len(valid) if valid else 0.0
    found = sum(1 for r in valid if r.rank > 0) / len(valid) if valid else 0.0
    lats = [r.latency_ms for r in results]
    p50 = median(lats) if lats else 0.0
    errors = sum(1 for r in results if r.error)
    return RetrievalReport(n, mrr, hit_rate, found, p50, errors, results)


def load_retrieval_suite(name_or_path: str | Path) -> list:
    """Load a retrieval suite by bare name (evals/suites/<name>.jsonl) or path."""
    path = Path(name_or_path)
    if not path.suffix:
        path = _SUITES_DIR / f"{path.name}.jsonl"
    cases = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        cases.append(RetrievalCase.from_dict(json.loads(line)))
    return cases


def run_retrieval_suite(cases: list, build_retriever, *,
                        top_n: int = 10, timeout_s: float = 30.0):
    """Rank every case's target against the live index.

    build_retriever is a zero-arg factory (sync or async) built on the eval
    loop so its asyncio primitives bind here (the run_ablation_suite
    constraint). It returns either:
      * retrieve_fn — async (question, collection, n) -> list[hit dict], or
      * (retrieve_fn, file_exists_fn) — file_exists_fn: async
        (target_file, collection) -> bool, used to pre-mark cases whose
        target file is no longer in the collection as ERRORS (stale suite,
        R1.2) instead of letting a deleted file read as a rank miss.
      * None — index unavailable; the suite returns None and the CLI exits 2.
    """
    async def _maybe(x):
        return await x if asyncio.iscoroutine(x) else x

    async def _run_all():
        built = await _maybe(build_retriever())
        if built is None:
            return None
        retrieve, file_exists = built if isinstance(built, tuple) else (built, None)
        results = []
        stale: dict[tuple, bool] = {}   # (collection, file) -> present?
        for case in cases:
            if file_exists is not None:
                key = (case.collection, case.target_file)
                if key not in stale:
                    try:
                        stale[key] = bool(await file_exists(case.target_file, case.collection))
                    except Exception:
                        stale[key] = True   # a probe failure must not invent stale errors
                if not stale[key]:
                    results.append(score_retrieval(
                        case, [], error=(f"stale target: {case.target_file} not in "
                                         f"'{case.collection}' collection — regenerate the suite")))
                    continue
            t0 = time.perf_counter()
            err, hits = "", []
            try:
                hits = await asyncio.wait_for(
                    retrieve(case.question, case.collection, max(top_n, case.k)),
                    timeout=timeout_s)
            except Exception as exc:
                err = f"{type(exc).__name__}: {exc}"
            latency = (time.perf_counter() - t0) * 1000.0
            results.append(score_retrieval(case, hits or [], latency_ms=latency, error=err))
        return aggregate_retrieval(results)

    return asyncio.run(_run_all())
