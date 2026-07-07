"""Retrieval-rank eval — model-free logic tests (matcher, MRR/Hit@K, runner).

The real --mode retrieval queries the persisted chroma_db; here the retriever
is a scripted fake so the harness logic is covered in CI without chroma or an
embedding model. Spec: specs/retrieval-quality-eval/ (criteria cited per test).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.retrieval import (
    RetrievalCase,
    hit_matches,
    score_retrieval,
    aggregate_retrieval,
    load_retrieval_suite,
    run_retrieval_suite,
)


def _case(**kw):
    base = dict(id="c", suite="s", question="q",
                target_file="core/scheduler.py",
                target_names=["AccessibilityScheduler"])
    base.update(kw)
    return RetrievalCase(**base)


def _hit(file="core/scheduler.py", name="AccessibilityScheduler", **kw):
    h = {"file": file, "name": name, "chunk_type": "class", "score": 0.9}
    h.update(kw)
    return h


# --------------------------------------------------------------------------- #
# hit_matches (R1.1)
# --------------------------------------------------------------------------- #

def test_hit_matches_file_and_name():
    assert hit_matches(_case(), _hit())


def test_hit_matches_file_level_when_no_names():
    assert hit_matches(_case(target_names=[]), _hit(name="anything_else"))


def test_hit_matches_tolerates_split_chunk_suffix():
    # oversized chunks are renamed "<name>_(i/N)" by _emit_chunks
    assert hit_matches(_case(), _hit(name="AccessibilityScheduler_(2/3)"))


def test_hit_matches_rejects_wrong_file_even_with_matching_name():
    assert not hit_matches(_case(), _hit(file="core/fusion_engine.py"))


def test_hit_matches_rejects_name_prefix_without_split_suffix():
    # "AccessibilitySchedulerFactory" is a different symbol, not a split chunk
    assert not hit_matches(_case(), _hit(name="AccessibilitySchedulerFactory"))


def test_hit_matches_docs_hit_without_name_field():
    # documents-collection hits carry `page`, not `name` (R1.1): named targets
    # never match them; file-level targets do.
    docs_hit = {"file": "docs/manual.pdf", "page": 3, "score": 0.8}
    assert not hit_matches(_case(target_file="docs/manual.pdf"), docs_hit)
    assert hit_matches(_case(target_file="docs/manual.pdf", target_names=[]), docs_hit)


# --------------------------------------------------------------------------- #
# rank_of_target + score_retrieval (R1.4)
# --------------------------------------------------------------------------- #

def test_rank_one_gives_rr_one():
    r = score_retrieval(_case(), [_hit()])
    assert r.rank == 1 and r.reciprocal_rank == 1.0 and r.hit_at_k


def test_rank_three_within_k():
    hits = [_hit(file="a.py"), _hit(file="b.py"), _hit()]
    r = score_retrieval(_case(k=5), hits)
    assert r.rank == 3 and r.reciprocal_rank == pytest.approx(1 / 3) and r.hit_at_k


def test_rank_beyond_k_found_but_no_hit():
    hits = [_hit(file=f"x{i}.py") for i in range(6)] + [_hit()]
    r = score_retrieval(_case(k=5), hits)
    assert r.rank == 7 and not r.hit_at_k and r.reciprocal_rank == pytest.approx(1 / 7)


def test_target_not_retrieved_scores_zero():
    r = score_retrieval(_case(), [_hit(file="other.py")])
    assert r.rank == 0 and r.reciprocal_rank == 0.0 and not r.hit_at_k


def test_error_case_carries_no_rank():
    r = score_retrieval(_case(), [_hit()], error="TimeoutError: boom")
    assert r.error and r.rank == 0 and r.reciprocal_rank == 0.0


# --------------------------------------------------------------------------- #
# aggregate_retrieval (R1.4, R1.2)
# --------------------------------------------------------------------------- #

def test_aggregate_mrr_and_hit_rate():
    results = [
        score_retrieval(_case(id="a"), [_hit()]),                         # rank 1
        score_retrieval(_case(id="b", k=5),
                        [_hit(file="x.py"), _hit()]),                     # rank 2
        score_retrieval(_case(id="c"), [_hit(file="x.py")]),              # miss
    ]
    rep = aggregate_retrieval(results)
    assert rep.n == 3
    assert rep.mrr == pytest.approx((1.0 + 0.5 + 0.0) / 3)
    assert rep.hit_rate == pytest.approx(2 / 3)
    assert rep.found_rate == pytest.approx(2 / 3)
    m = rep.metrics()
    assert m["exact_acc"] == m["mrr"]          # gated metric alias
    assert rep.exact_acc == rep.mrr


def test_aggregate_excludes_errored_from_means_but_counts_them():
    results = [
        score_retrieval(_case(id="a"), [_hit()]),                         # rank 1
        score_retrieval(_case(id="b"), [], error="stale target: gone.py"),
    ]
    rep = aggregate_retrieval(results)
    assert rep.n == 2 and rep.errors == 1
    assert rep.mrr == pytest.approx(1.0)       # errored case excluded from the mean
    assert len(rep.failures) == 1              # error is a failure; rank-1 is not


def test_aggregate_empty():
    rep = aggregate_retrieval([])
    assert rep.n == 0 and rep.mrr == 0.0 and rep.errors == 0


# --------------------------------------------------------------------------- #
# run_retrieval_suite (fake retriever — no chroma, no model)
# --------------------------------------------------------------------------- #

def _scripted_retriever(script):
    async def retrieve(question, collection, n):
        return script.get(question, [])[:n]
    return retrieve


def test_run_suite_with_fake_retriever():
    cases = [_case(id="r1", question="who schedules?"),
             _case(id="r2", question="unknown thing", target_file="nope.py")]
    script = {"who schedules?": [_hit()], "unknown thing": [_hit()]}
    rep = run_retrieval_suite(cases, lambda: _scripted_retriever(script), timeout_s=5)
    assert rep.n == 2 and rep.errors == 0
    assert rep.hit_rate == pytest.approx(0.5)
    assert rep.mrr == pytest.approx(0.5)


def test_run_suite_accepts_async_factory():
    async def build():
        return _scripted_retriever({"q": [_hit()]})
    rep = run_retrieval_suite([_case()], build, timeout_s=5)
    assert rep.n == 1 and rep.mrr == 1.0


def test_run_suite_none_factory_skips():
    # index unavailable -> None report -> CLI exit 2 (R1.3)
    assert run_retrieval_suite([_case()], lambda: None, timeout_s=5) is None


def test_run_suite_stale_target_becomes_error():
    # R1.2: file absent from the collection -> surfaced ERROR, not a rank miss
    async def file_exists(target_file, collection):
        return target_file != "gone.py"
    def build():
        return (_scripted_retriever({"q": [_hit()]}), file_exists)
    cases = [_case(id="live"), _case(id="stale", target_file="gone.py")]
    rep = run_retrieval_suite(cases, build, timeout_s=5)
    assert rep.errors == 1
    stale = next(r for r in rep.results if r.case_id == "stale")
    assert "stale target" in stale.error
    assert rep.mrr == pytest.approx(1.0)       # stale case excluded from the mean


def test_run_suite_retriever_exception_is_error_case():
    async def retrieve(question, collection, n):
        raise RuntimeError("chroma exploded")
    rep = run_retrieval_suite([_case()], lambda: retrieve, timeout_s=5)
    assert rep.n == 1 and rep.errors == 1
    assert "chroma exploded" in rep.results[0].error


# --------------------------------------------------------------------------- #
# shipped suite (R2.x — synthetic generation output stays well-formed)
# --------------------------------------------------------------------------- #

def test_shipped_retrieval_suite_valid():
    suite_path = Path(__file__).resolve().parents[1] / "evals" / "suites" / "retrieval_synthetic.jsonl"
    if not suite_path.exists():
        pytest.skip("retrieval_synthetic.jsonl not generated yet")
    cases = load_retrieval_suite("retrieval_synthetic")
    assert len(cases) >= 5
    ids = [c.id for c in cases]
    assert len(ids) == len(set(ids))
    for c in cases:
        assert c.question and c.target_file
        assert c.collection in ("codebase", "documents", "combined")
        assert c.k >= 1
        assert c.source in ("synthetic", "curated")
