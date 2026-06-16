"""storage/memory_compactor.py — MemoryCompactor (R-2 write side).

Compresses a finished DevAgent run (agent_runs + agent_steps) into a short,
permanent episodic "memory note" via the LOCAL model, so future agents can recall
"how I solved / failed at X under state Y" without re-reading raw execution logs.

Design constraints:
- LOCAL only (ModelRouter, domain="general"/gemma4) — zero egress; notes are never
  spoken raw and never leave the machine.
- Off the hot path — invoked at run finalization (fire-and-forget), never in the
  60 Hz loop.
- Flare-aware — skips synthesis while a pain-day flare is active (the GPU/voice
  pipeline is prioritized then; rule 6).
"""

from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)


class MemoryCompactor:
    _MAX_STEPS_IN_PROMPT = 12
    _MAX_SUMMARY_CHARS = 600

    def __init__(self, memory, router, agent_db) -> None:
        self._memory = memory       # MemoryManager (write_memory_note + pain-day flag)
        self._router = router        # ModelRouter (local inference)
        self._db = agent_db          # AgentDB (run/step reads)

    async def summarize_run(self, run_id: int) -> Optional[int]:
        """Summarize one finished run into an episodic note. Returns the note id, or
        None when skipped (flare, missing run, empty summary, no memory wired)."""
        if self._memory is None or self._db is None or self._router is None:
            return None
        # Flare-skip: don't spend GPU on compaction during a flare.
        try:
            if self._memory.get_pain_day_active():
                log.debug("MemoryCompactor: pain-day active — skipping run %s", run_id)
                return None
        except Exception:
            pass

        run = await self._db.get_agent_run(run_id)
        if not run:
            return None
        steps = await self._db.get_agent_steps(run_id)

        summary = await self._summarize(run, steps)
        if not summary:
            return None

        succeeded = bool(run.get("success"))
        status = run.get("status", "")
        kind = "recovery" if (not succeeded or status in ("failed", "interrupted")) else "note"
        return await self._memory.write_memory_note(
            kind=kind,
            goal=run.get("goal", ""),
            summary=summary,
            domain=run.get("domain", "general"),
            source_run_id=run_id,
        )

    async def _summarize(self, run: dict, steps: list[dict]) -> Optional[str]:
        lines = [
            f"Goal: {run.get('goal', '')}",
            f"Outcome: {'success' if run.get('success') else 'failure'} "
            f"(status={run.get('status', '?')}, {len(steps)} steps)",
            "Steps:",
        ]
        for s in steps[: self._MAX_STEPS_IN_PROMPT]:
            args = (s.get("args") or "")[:60]
            if s.get("success"):
                outcome = "ok"
            else:
                outcome = "FAILED: " + (s.get("result") or "")[:80]
            lines.append(f"- {s.get('action', '?')} {args} -> {outcome}")
        transcript = "\n".join(lines)
        prompt = (
            "You are writing a concise memory note for a developer's personal agent. "
            "In 2-3 sentences, capture what the goal was, what worked or failed, and "
            "the key recovery step or lesson — so the agent can reuse it next time. "
            "Do not include code blocks.\n\n" + transcript
        )
        try:
            res = await self._router.infer(domain="general", user_text=prompt)
            if res is not None and getattr(res, "ok", False):
                text = (res.text or "").strip()
                if text:
                    return text[: self._MAX_SUMMARY_CHARS]
        except Exception as exc:
            log.debug("MemoryCompactor: local summarize failed: %s", exc)
        return None
