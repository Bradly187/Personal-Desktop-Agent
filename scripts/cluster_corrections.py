#!/usr/bin/env python3
"""Cluster harvested user corrections into candidate failure modes (GAP-9).

Every confirmed "no, not like that" correction is labeled failure data. The
coordinator harvests them into agent.db `user_corrections` (GAP-9); this offline
script embeds them with MiniLM (the same all-MiniLM-L6-v2 the rest of the agent
uses) and k-means-clusters them, printing the top clusters as candidate new eval
cases / systematic agent failure modes.

Degrades gracefully: with sentence-transformers / scikit-learn absent it falls
back to a token-Jaccard agglomerative grouping so it still produces clusters on
a bare install. Read-only against the DB.

Usage:
    python scripts/cluster_corrections.py [--db agent.db] [--k 8] [--limit 1000]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from storage.db import AgentDB, _tokens, _jaccard


async def _load_corrections(db_path: str, limit: int) -> list[dict]:
    db = AgentDB()
    await db.open(db_path)
    if not db.available:
        print("AgentDB unavailable (aiosqlite missing?) — nothing to cluster",
              file=sys.stderr)
        return []
    rows = await db.get_corrections(limit=limit)
    await db.close()
    return rows


def _cluster_embeddings(texts: list[str], k: int) -> list[int]:
    """k-means over MiniLM embeddings. Returns a cluster label per text."""
    from sentence_transformers import SentenceTransformer  # type: ignore
    from sklearn.cluster import KMeans  # type: ignore

    model = SentenceTransformer("all-MiniLM-L6-v2")
    embs = model.encode(texts, normalize_embeddings=True)
    k = max(1, min(k, len(texts)))
    km = KMeans(n_clusters=k, n_init=10, random_state=0)
    return list(km.fit_predict(embs))


def _cluster_jaccard(texts: list[str], threshold: float = 0.4) -> list[int]:
    """Dependency-free fallback: greedy agglomeration by token-overlap."""
    labels = [-1] * len(texts)
    toks = [_tokens(t) for t in texts]
    next_label = 0
    for i in range(len(texts)):
        if labels[i] != -1:
            continue
        labels[i] = next_label
        for j in range(i + 1, len(texts)):
            if labels[j] == -1 and _jaccard(toks[i], toks[j]) >= threshold:
                labels[j] = next_label
        next_label += 1
    return labels


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Cluster harvested user corrections.")
    ap.add_argument("--db", default="agent.db", help="path to agent.db")
    ap.add_argument("--k", type=int, default=8, help="target cluster count (k-means)")
    ap.add_argument("--limit", type=int, default=1000, help="max corrections to load")
    args = ap.parse_args(argv)

    rows = asyncio.run(_load_corrections(args.db, args.limit))
    if not rows:
        print("no corrections harvested yet")
        return 0

    texts = [r["correction_text"] for r in rows]
    try:
        labels = _cluster_embeddings(texts, args.k)
        method = "minilm+kmeans"
    except Exception as exc:
        print(f"(embedding path unavailable: {exc} — using Jaccard fallback)",
              file=sys.stderr)
        labels = _cluster_jaccard(texts)
        method = "jaccard"

    clusters: dict[int, list[dict]] = defaultdict(list)
    for row, lab in zip(rows, labels):
        clusters[lab].append(row)

    print(f"{len(rows)} corrections → {len(clusters)} clusters ({method})\n")
    for lab, members in sorted(clusters.items(), key=lambda kv: -len(kv[1])):
        doms = {m.get("domain") or "?" for m in members}
        print(f"── cluster {lab}  (n={len(members)}, domains={sorted(doms)})")
        for m in members[:6]:
            prior = m.get("prior_action") or "?"
            print(f"     • {m['correction_text'][:80]!r}  (was: {prior[:40]})")
        if len(members) > 6:
            print(f"     … +{len(members) - 6} more")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
