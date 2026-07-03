"""Generate synthetic ground truth for the retrieval-rank eval (--mode retrieval).

Samples real chunks from the persisted ChromaDB `codebase` collection
(stratified: at most one chunk per file), asks the LOCAL Ollama model to write
natural developer questions each chunk uniquely answers, then keeps only the
questions that pass a self-retrieval filter — the source chunk's file must
appear in the top-10 for its own question. The observed rank is recorded as
`provenance.gen_rank`; filtering at 10 while the eval gates Hit@5/MRR leaves
headroom so the suite starts healthy but not saturated.

Local-only by design (specs/retrieval-quality-eval R2, docs/decisions.md D028):
no cloud calls — the generator is the same Ollama the agent already runs.

Usage:
    python scripts/generate_retrieval_eval_data.py                 # 50 cases
    python scripts/generate_retrieval_eval_data.py --n-cases 5 --dry-run
    python scripts/generate_retrieval_eval_data.py --model llama3.1:8b --seed 7

Output:
    evals/suites/retrieval_synthetic.jsonl  (overwritten — a suite is a
    snapshot of one generation run; use --append to accumulate instead)

Afterwards: spot-review the questions, then lock the baseline:
    python -m evals.run --suite retrieval_synthetic --mode retrieval --update-baseline --tolerance 0.10
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
import re
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

_DEFAULT_OUT = _ROOT / "evals" / "suites" / "retrieval_synthetic.jsonl"

_SPLIT_SUFFIX = re.compile(r"_\(\d+/\d+\)$")

_PREFERRED_CHUNK_TYPES = {"function", "class", "method"}

_SYSTEM_PROMPT = (
    "You write evaluation questions for a code-retrieval system. Given one "
    "chunk of source code, write {n} natural questions a developer working on "
    "this repository might ask that THIS chunk (and ideally only this chunk) "
    "answers.\n\n"
    "Rules:\n"
    "- Ask about behavior, purpose, or mechanism — the way a person phrases a "
    "question before they know where the answer lives.\n"
    "- Do NOT quote code lines verbatim and do NOT mention the file path.\n"
    "- Naming the key function/class/concept is fine; copying its signature is not.\n"
    "- One sentence per question."
)

_QUESTIONS_SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
        }
    },
    "required": ["questions"],
}


def _chunk_sha16(document: str) -> str:
    """First 16 hex of sha256 over the STORED document text (taint prefix and
    all) — lets tooling detect content drift after a reindex."""
    return hashlib.sha256(document.encode("utf-8")).hexdigest()[:16]


def _slug(rel_path: str) -> str:
    stem = Path(rel_path).stem.lower()
    return re.sub(r"[^a-z0-9]+", "-", stem).strip("-") or "chunk"


def _sample_chunks(col, *, n_cases: int, min_chars: int, seed: int) -> list[dict]:
    """Stratified sample: seeded shuffle of files, at most one chunk per file.

    Prefers function/class/method chunks; skips short chunks and anything the
    trust classifier tainted at index time (a [TAINT] chunk is adversarial
    input, not ground truth).
    """
    got = col.get(include=["documents", "metadatas"])
    by_file: dict[str, list[tuple[str, dict]]] = {}
    for doc, meta in zip(got.get("documents") or [], got.get("metadatas") or []):
        if not doc or len(doc) < min_chars:
            continue
        if meta.get("taint") or doc.startswith("[TAINT] "):
            continue
        by_file.setdefault(meta.get("file", ""), []).append((doc, meta))

    rng = random.Random(seed)
    files = sorted(f for f in by_file if f)
    rng.shuffle(files)

    picked = []
    for f in files:
        if len(picked) >= n_cases:
            break
        candidates = by_file[f]
        preferred = [c for c in candidates
                     if c[1].get("chunk_type") in _PREFERRED_CHUNK_TYPES]
        doc, meta = rng.choice(preferred or candidates)
        picked.append({"document": doc, "meta": meta})
    return picked


async def _generate_questions(infer, doc: str, meta: dict, n: int) -> list[str]:
    user = (f"Chunk type: {meta.get('chunk_type', '?')}   "
            f"Symbol: {meta.get('name', '?')}\n\n```\n{doc[:3500]}\n```")
    raw = await infer(_SYSTEM_PROMPT.format(n=n), user, format=_QUESTIONS_SCHEMA)
    try:
        parsed = json.loads(raw)
        qs = parsed.get("questions", [])
    except (json.JSONDecodeError, AttributeError):
        return []
    return [q.strip() for q in qs if isinstance(q, str) and len(q.strip()) >= 15][:n]


async def _self_retrieval_rank(idx, question: str, target_file: str, *,
                               window: int = 10) -> int:
    """1-based rank of the target FILE for its own question; 0 = not in top-N."""
    hits = await idx.query_codebase(question, n=window)
    for i, hit in enumerate(hits, start=1):
        if hit.get("file") == target_file:
            return i
    return 0


async def _run(args) -> int:
    from inference.codebase_indexer import CodebaseIndexer
    from inference.local_inference import OllamaInference

    idx = CodebaseIndexer(project_root=str(_ROOT), embedder=None)
    if not await idx.start():
        print("ERROR: CodebaseIndexer unavailable (chromadb not importable or "
              "chroma_db unreadable).", file=sys.stderr)
        return 1
    status = await idx.get_status()
    if not status.get("codebase_chunks"):
        print("ERROR: codebase collection is empty — build the index first:\n"
              "  python inference/codebase_indexer.py", file=sys.stderr)
        return 1

    chunks = _sample_chunks(idx._codebase_col,  # private handle: the indexer has
                            # no bulk-dump API and adding one for a script isn't
                            # warranted (same rationale as evals/run.py's guard)
                            n_cases=args.n_cases,
                            min_chars=args.min_chunk_chars,
                            seed=args.seed)
    print(f"index: {status['codebase_chunks']} chunks / "
          f"{status['indexed_files']} files -> sampled {len(chunks)} "
          f"(1 per file, seed={args.seed})")
    if not chunks:
        print("ERROR: nothing to sample after filters.", file=sys.stderr)
        return 1

    if args.dry_run:
        for c in chunks:
            m = c["meta"]
            print(f"  would generate {args.questions_per_chunk} question(s) for "
                  f"{m.get('file')} :: {m.get('name')} [{m.get('chunk_type')}]")
        print("DRY RUN — no model calls made, nothing written")
        return 0

    oi = OllamaInference(model=args.model, timeout=120)

    async def infer(system: str, user: str, format: dict | None = None) -> str:
        resp = await oi._chat([
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ], format=format)
        msg = (resp or {}).get("message", {})
        return msg.get("content", "") if isinstance(msg, dict) else ""

    today = time.strftime("%Y-%m-%d")
    cases, kept, rejected, parse_failures = [], 0, 0, 0
    counter: dict[str, int] = {}

    for c in chunks:
        doc, meta = c["document"], c["meta"]
        file, name = meta.get("file", ""), meta.get("name", "")
        base_name = _SPLIT_SUFFIX.sub("", name)
        try:
            questions = await _generate_questions(infer, doc, meta,
                                                  args.questions_per_chunk)
        except Exception as exc:
            print(f"  WARN {file}: generation failed ({type(exc).__name__}: {exc})",
                  file=sys.stderr)
            parse_failures += 1
            continue
        if not questions:
            parse_failures += 1
            continue
        for q in questions:
            rank = await _self_retrieval_rank(idx, q, file)
            if rank == 0:
                rejected += 1
                print(f"  reject (not self-retrieved in top-10): {q[:70]}")
                continue
            kept += 1
            slug = _slug(file)
            counter[slug] = counter.get(slug, 0) + 1
            cases.append({
                "id": f"ret-{slug}-{counter[slug]:02d}",
                "suite": "retrieval_synthetic",
                "question": q,
                # File-level target: the self-retrieval filter validates at file
                # granularity, so the gate must match at the same granularity —
                # otherwise a question that retrieves the right file via a
                # sibling chunk ships as a guaranteed miss. Name-level targets
                # stay supported for hand-curated cases; the chunk name is kept
                # in provenance for reference.
                "target_file": file,
                "target_names": [],
                "collection": "codebase",
                "k": 5,
                "source": "synthetic",
                "provenance": {
                    "generator_model": args.model,
                    "chunk_name": base_name,
                    "chunk_sha16": _chunk_sha16(doc),
                    "gen_rank": rank,
                    "generated_at": today,
                },
                "tags": ["synthetic", "retrieval", meta.get("chunk_type", "chunk")],
            })
            print(f"  keep  (rank {rank}) [{file} :: {base_name}] {q[:70]}")

    if not cases:
        print("ERROR: no cases survived the self-retrieval filter — is the model "
              "up and the index healthy?", file=sys.stderr)
        return 1

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    header = (f"# retrieval_synthetic — synthetic question -> target-chunk ground truth\n"
              f"# generated {today} by scripts/generate_retrieval_eval_data.py "
              f"(model={args.model}, seed={args.seed}, self-retrieval filter @10)\n"
              f"# regenerate: python scripts/generate_retrieval_eval_data.py "
              f"--n-cases {args.n_cases} --seed {args.seed}\n")
    mode = "a" if args.append else "w"
    with open(out, mode, encoding="utf-8") as f:
        if not args.append:
            f.write(header)
        for case in cases:
            f.write(json.dumps(case, ensure_ascii=False) + "\n")

    print(f"\nwrote {len(cases)} case(s) -> {out}")
    print(f"kept={kept} rejected={rejected} generation-failures={parse_failures}")
    print("NEXT: spot-review the questions, then lock the baseline:\n"
          "  python -m evals.run --suite retrieval_synthetic --mode retrieval "
          "--update-baseline --tolerance 0.10")
    return 0


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--n-cases", type=int, default=50,
                   help="max chunks to sample (1 per file; default 50)")
    p.add_argument("--questions-per-chunk", type=int, default=1,
                   help="questions to generate per sampled chunk (default 1)")
    p.add_argument("--model", default="llama3.1:8b",
                   help="local Ollama model for question generation")
    p.add_argument("--min-chunk-chars", type=int, default=200,
                   help="skip chunks shorter than this (default 200)")
    p.add_argument("--seed", type=int, default=7,
                   help="sampling seed — same seed + same index = same sample")
    p.add_argument("--out", default=str(_DEFAULT_OUT),
                   help="output JSONL suite path")
    p.add_argument("--dry-run", action="store_true",
                   help="show what would be sampled without calling the model")
    p.add_argument("--append", action="store_true",
                   help="append to the suite instead of overwriting it")
    return p.parse_args()


def main() -> int:
    return asyncio.run(_run(_parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
