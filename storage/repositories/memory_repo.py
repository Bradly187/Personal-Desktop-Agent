from __future__ import annotations
import json
import logging
from storage.repositories.common import _GOAL_LEASE_TTL_S, _pid_alive
import math
import os
import time
import hashlib
import asyncio
from typing import Optional, TYPE_CHECKING

from storage.embeddings import _get_encoder, _encode_sync, _cosine, _tokens, _jaccard, _recency_weight, _fse_score

if TYPE_CHECKING:
    from core.command_executor import Command

log = logging.getLogger(__name__)

class MemoryRepo:
    _COUNTEREXAMPLE_MIN_SIM = 0.30

    def __init__(self, conn):
        self._conn = conn

    async def search_lecture_notes(
        self,
        query: str,
        session_id: int | None = None,
        limit: int = 20,
    ) -> list[dict]:
        """Full-text search over ambient_transcripts (lecture mode captures).

        Uses SQLite LIKE for simple substring matching. Returns rows ordered
        by timestamp descending so most recent results come first.

        Args:
            query:      Search term — matched as %query% substring.
            session_id: Restrict to a specific session (None = all sessions).
            limit:      Max rows to return.
        """
        if not self._conn:
            return []
        try:
            like = f"%{query}%"
            if session_id is not None:
                rows = await (await self._conn.execute(
                    """SELECT ts, text, logprob, duration_s
                       FROM ambient_transcripts
                       WHERE session_id = ? AND text LIKE ?
                       ORDER BY ts DESC LIMIT ?""",
                    (session_id, like, limit),
                )).fetchall()
            else:
                rows = await (await self._conn.execute(
                    """SELECT ts, text, logprob, duration_s
                       FROM ambient_transcripts
                       WHERE text LIKE ?
                       ORDER BY ts DESC LIMIT ?""",
                    (like, limit),
                )).fetchall()
            return [
                {"ts": r[0], "text": r[1], "logprob": r[2], "duration_s": r[3]}
                for r in rows
            ]
        except Exception as exc:
            log.warning("AgentDB.search_lecture_notes failed: %s", exc)
            return []

    async def upsert_few_shot_example(
        self,
        cmd: "Command",
        action_str: str,
        domain: str = "command",
        command_id: Optional[int] = None,
    ) -> None:
        if not self._conn:
            return
        now = time.time()
        try:
            await self._conn.execute(
                """INSERT INTO few_shot_examples
                   (command_id, text, action, source, domain, ts, usage_count)
                   VALUES (?, ?, ?, ?, ?, ?, 1)
                   ON CONFLICT(text, action) DO UPDATE
                     SET usage_count = usage_count + 1, ts = excluded.ts""",
                (
                    command_id if (command_id and command_id > 0) else None,
                    cmd.text, action_str, cmd.source, domain, now,
                ),
            )
            # Word count tracking for hotword promotion
            for word in _tokens(cmd.text):
                if len(word) >= 3:
                    await self._conn.execute(
                        """INSERT INTO word_counts (word, count) VALUES (?, 1)
                           ON CONFLICT(word) DO UPDATE SET count = count + 1""",
                        (word,),
                    )
            await self._conn.commit()
        except Exception as exc:
            log.warning("AgentDB.upsert_few_shot_example failed: %s", exc)
            return

        # Compute and store embedding if MiniLM is available and row has none yet
        try:
            encoder = await _get_encoder()
            if encoder is None:
                return
            emb = await asyncio.to_thread(_encode_sync, cmd.text, encoder)
            await self._conn.execute(
                """UPDATE few_shot_examples SET embedding = ?
                   WHERE text = ? AND action = ? AND embedding IS NULL""",
                (emb, cmd.text, action_str),
            )
            await self._conn.commit()
        except Exception as exc:
            log.debug("Embedding update failed (non-fatal): %s", exc)

    async def get_few_shot_examples(
        self,
        cmd: "Command",
        n: int = 5,
        domain: str = "command",
    ) -> list[dict]:
        """Return up to n examples ranked by (cosine | Jaccard) × recency × usage.

        Uses cosine similarity when both the query and the stored row have
        embeddings; falls back to Jaccard word-overlap otherwise. Mixed rows
        (some with embeddings, some without) are handled per-row.
        """
        if not self._conn:
            return []
        try:
            now = time.time()
            query_tokens = _tokens(cmd.text)

            # Try to get a query embedding — non-blocking, falls back gracefully
            encoder = await _get_encoder()
            query_emb: Optional[bytes] = None
            if encoder is not None:
                try:
                    query_emb = await asyncio.to_thread(_encode_sync, cmd.text, encoder)
                except Exception:
                    pass

            async with self._conn.execute(
                """SELECT text, action, ts, usage_count, embedding
                   FROM few_shot_examples
                   WHERE domain = ?
                   ORDER BY ts DESC LIMIT 1000""",
                (domain,),
            ) as cur:
                rows = [dict(r) for r in await cur.fetchall()]

            def _score(row: dict) -> float:
                recency = _recency_weight(row["ts"], now)
                usage   = math.log1p(row["usage_count"])
                if query_emb is not None and row.get("embedding"):
                    sim = _cosine(query_emb, row["embedding"])
                else:
                    sim = _jaccard(query_tokens, _tokens(row["text"]))
                return sim * recency * usage

            scored = sorted(rows, key=_score, reverse=True)
            return [
                {"command_text": r["text"], "action_text": r["action"]}
                for r in scored[:n]
                if _score(r) > 0.0
            ]
        except Exception as exc:
            log.warning("AgentDB.get_few_shot_examples failed: %s", exc)
            return []

    async def upsert_few_shot_counterexample(
        self,
        cmd: "Command",
        wrong_action: str,
        domain: str = "command",
        reason: str = "pipeline_failure",
        command_id: Optional[int] = None,
    ) -> None:
        if not self._conn:
            return
        now = time.time()
        try:
            # reason upgrades to user_correction (direct evidence the mapping is
            # wrong) but never downgrades back to pipeline_failure — the gate in
            # get_few_shot_counterexamples() trusts user_correction immediately.
            await self._conn.execute(
                """INSERT INTO few_shot_counterexamples
                   (command_id, text, wrong_action, reason, source, domain, ts, usage_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                   ON CONFLICT(text, wrong_action) DO UPDATE
                     SET usage_count = usage_count + 1, ts = excluded.ts,
                         reason = CASE WHEN excluded.reason = 'user_correction'
                                       THEN 'user_correction' ELSE reason END""",
                (
                    command_id if (command_id and command_id > 0) else None,
                    cmd.text, wrong_action, reason, cmd.source, domain, now,
                ),
            )
            await self._conn.commit()
        except Exception as exc:
            log.warning("AgentDB.upsert_few_shot_counterexample failed: %s", exc)
            return
        try:
            encoder = await _get_encoder()
            if encoder is None:
                return
            emb = await asyncio.to_thread(_encode_sync, cmd.text, encoder)
            await self._conn.execute(
                """UPDATE few_shot_counterexamples SET embedding = ?
                   WHERE text = ? AND wrong_action = ? AND embedding IS NULL""",
                (emb, cmd.text, wrong_action),
            )
            await self._conn.commit()
        except Exception as exc:
            log.debug("Counterexample embedding update failed (non-fatal): %s", exc)

    async def get_few_shot_counterexamples(
        self,
        cmd: "Command",
        n: int = 3,
        domain: str = "command",
    ) -> list[dict]:
        """Return up to n counterexamples ranked by (cosine | Jaccard) × recency × usage.

        Poisoning guards — an execution failure is NOT proof the LLM mapping
        was wrong (the app may have been slow, the window missing), so:
        - pipeline_failure rows need usage_count >= 2 before they inject; a
          single transient failure stays recorded but never reaches a prompt.
          user_correction rows inject immediately — the user said it was wrong.
        - pairs that also exist as a positive few-shot example are excluded:
          success evidence supersedes failure evidence, and a prompt must never
          contain the same pair as both example and counterexample.
        - a similarity floor keeps unrelated counterexamples out of prompts.
        """
        if not self._conn:
            return []
        try:
            now = time.time()
            query_tokens = _tokens(cmd.text)
            encoder = await _get_encoder()
            query_emb: Optional[bytes] = None
            if encoder is not None:
                try:
                    query_emb = await asyncio.to_thread(_encode_sync, cmd.text, encoder)
                except Exception:
                    pass
            async with self._conn.execute(
                """SELECT ce.text, ce.wrong_action, ce.ts, ce.usage_count, ce.embedding
                   FROM few_shot_counterexamples ce
                   WHERE ce.domain = ?
                     AND (ce.reason = 'user_correction' OR ce.usage_count >= 2)
                     AND NOT EXISTS (
                         SELECT 1 FROM few_shot_examples fe
                         WHERE fe.text = ce.text AND fe.action = ce.wrong_action
                     )
                   ORDER BY ce.ts DESC LIMIT 500""",
                (domain,),
            ) as cur:
                rows = [dict(r) for r in await cur.fetchall()]

            scored: list[tuple[float, dict]] = []
            for row in rows:
                if query_emb is not None and row.get("embedding"):
                    sim = _cosine(query_emb, row["embedding"])
                else:
                    sim = _jaccard(query_tokens, _tokens(row["text"]))
                if sim < self._COUNTEREXAMPLE_MIN_SIM:
                    continue
                recency = _recency_weight(row["ts"], now)
                usage   = math.log1p(row["usage_count"])
                scored.append((sim * recency * usage, row))

            scored.sort(key=lambda pair: pair[0], reverse=True)
            return [
                {"command_text": r["text"], "wrong_action": r["wrong_action"]}
                for score, r in scored[:n]
                if score > 0.0
            ]
        except Exception as exc:
            log.warning("AgentDB.get_few_shot_counterexamples failed: %s", exc)
            return []

    async def delete_few_shot_example(self, text: str, action: str) -> None:
        """Remove a (text, action) positive example the user has rejected.

        Called by record_correction: a user correction is direct evidence the
        old mapping is wrong, so a stale positive example for the same pair
        must not keep reinforcing it — and must not suppress the correction's
        counterexample via the contradiction guard in
        get_few_shot_counterexamples. No-op when the pair was never recorded.
        """
        if not self._conn:
            return
        try:
            await self._conn.execute(
                "DELETE FROM few_shot_examples WHERE text = ? AND action = ?",
                (text, action),
            )
            await self._conn.commit()
        except Exception as exc:
            log.warning("AgentDB.delete_few_shot_example failed: %s", exc)

    async def delete_few_shot_counterexample(
        self, text: str, action: str
    ) -> None:
        """Remove a (text, action) counterexample after the same pair succeeds.

        A later success supersedes earlier failure evidence: the mapping is
        demonstrably right, so keeping it as a counterexample would re-poison
        prompts the next time the pair fails transiently. Exact-match on the
        UNIQUE(text, wrong_action) key; no-op when the pair was never recorded.
        """
        if not self._conn:
            return
        try:
            await self._conn.execute(
                "DELETE FROM few_shot_counterexamples"
                " WHERE text = ? AND wrong_action = ?",
                (text, action),
            )
            await self._conn.commit()
        except Exception as exc:
            log.warning("AgentDB.delete_few_shot_counterexample failed: %s", exc)

    async def insert_episodic_memory(
        self,
        kind: str,
        goal: str,
        summary: str,
        *,
        domain: str = "general",
        source_run_id: Optional[int] = None,
        source_command_id: Optional[int] = None,
        pain_day_active: bool = False,
        pain_day_score: float = 0.0,
        salience: float = 1.0,
    ) -> Optional[int]:
        """Persist one episodic memory note. Returns its row id (or None).

        The embedding is computed from `summary` (the recall key) when MiniLM is
        available; recall falls back to Jaccard otherwise — same model as few-shot.
        """
        if not self._conn:
            return None
        now = time.time()
        try:
            cur = await self._conn.execute(
                """INSERT INTO episodic_memory
                   (ts, kind, goal, summary, source_run_id, source_command_id,
                    domain, pain_day_active, pain_day_score, salience, usage_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
                (
                    now, kind, goal, summary,
                    source_run_id if (source_run_id and source_run_id > 0) else None,
                    source_command_id if (source_command_id and source_command_id > 0) else None,
                    domain, 1 if pain_day_active else 0, float(pain_day_score),
                    float(salience),
                ),
            )
            row_id = cur.lastrowid
            await self._conn.commit()
        except Exception as exc:
            log.warning("AgentDB.insert_episodic_memory failed: %s", exc)
            return None
        # Best-effort embedding of the summary (non-fatal if MiniLM absent).
        try:
            encoder = await _get_encoder()
            if encoder is not None:
                emb = await asyncio.to_thread(_encode_sync, summary, encoder)
                await self._conn.execute(
                    "UPDATE episodic_memory SET embedding = ? WHERE id = ?",
                    (emb, row_id),
                )
                await self._conn.commit()
        except Exception as exc:
            log.debug("Episodic embedding update failed (non-fatal): %s", exc)
        return row_id

    async def query_episodic_memory(
        self,
        query: str,
        n: int = 5,
        *,
        kind: Optional[str] = None,
        domain: Optional[str] = None,
        pain_day: Optional[bool] = None,
    ) -> list[dict]:
        """Recall up to n episodic notes ranked by (cosine | Jaccard) × recency × salience.

        Optional filters narrow by kind/domain/physical-state. Mirrors
        get_few_shot_examples scoring; the SQLite row is the source of truth.
        """
        if not self._conn:
            return []
        try:
            now = time.time()
            query_tokens = _tokens(query)
            encoder = await _get_encoder()
            query_emb: Optional[bytes] = None
            if encoder is not None:
                try:
                    query_emb = await asyncio.to_thread(_encode_sync, query, encoder)
                except Exception:
                    pass

            where = ["1=1"]
            params: list = []
            if kind is not None:
                where.append("kind = ?")
                params.append(kind)
            if domain is not None:
                where.append("domain = ?")
                params.append(domain)
            if pain_day is not None:
                where.append("pain_day_active = ?")
                params.append(1 if pain_day else 0)

            async with self._conn.execute(
                f"""SELECT id, kind, goal, summary, domain, pain_day_active,
                          pain_day_score, ts, salience, usage_count, embedding
                   FROM episodic_memory
                   WHERE {' AND '.join(where)}
                   ORDER BY ts DESC LIMIT 1000""",
                tuple(params),
            ) as cur:
                rows = [dict(r) for r in await cur.fetchall()]

            def _score(row: dict) -> float:
                recency = _recency_weight(row["ts"], now)
                salience = max(0.1, float(row.get("salience", 1.0)))
                if query_emb is not None and row.get("embedding"):
                    sim = _cosine(query_emb, row["embedding"])
                else:
                    sim = _jaccard(query_tokens, _tokens(row["summary"]))
                return sim * recency * salience

            scored = sorted(rows, key=_score, reverse=True)
            out = []
            for r in scored[:n]:
                if _score(r) <= 0.0:
                    continue
                out.append({
                    "id": r["id"],
                    "kind": r["kind"],
                    "goal": r["goal"],
                    "summary": r["summary"],
                    "domain": r["domain"],
                    "pain_day_active": bool(r["pain_day_active"]),
                    "pain_day_score": r["pain_day_score"],
                    "ts": r["ts"],
                    "score": round(_score(r), 4),
                })
            return out
        except Exception as exc:
            log.warning("AgentDB.query_episodic_memory failed: %s", exc)
            return []

    async def touch_episodic_memory(self, mem_id: int) -> None:
        """Bump usage_count + last_recalled_ts after a note is recalled (salience signal)."""
        if not self._conn:
            return
        try:
            await self._conn.execute(
                "UPDATE episodic_memory SET usage_count = usage_count + 1, "
                "last_recalled_ts = ? WHERE id = ?",
                (time.time(), mem_id),
            )
            await self._conn.commit()
        except Exception as exc:
            log.debug("AgentDB.touch_episodic_memory failed (non-fatal): %s", exc)

    async def get_few_shot_texts_by_domain(self, limit: int = 2000) -> dict:
        """Return {domain: [text, …]} from few_shot_examples — the confirmed-correct
        per-domain vocabulary the overlay learner samples (E2)."""
        if not self._conn:
            return {}
        try:
            async with self._conn.execute(
                "SELECT domain, text FROM few_shot_examples ORDER BY ts DESC LIMIT ?",
                (limit,),
            ) as cur:
                out: dict = {}
                for r in await cur.fetchall():
                    out.setdefault(r["domain"], []).append(r["text"])
                return out
        except Exception as exc:
            log.warning("AgentDB.get_few_shot_texts_by_domain failed: %s", exc)
            return {}

