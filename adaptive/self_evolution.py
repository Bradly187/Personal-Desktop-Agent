"""adaptive/self_evolution.py — SelfEvolutionPipeline (R-3, OFFLINE).

Non-parametric self-evolution: mine the agent's own history for success/failure
patterns, synthesize few-shot examples / counterexamples, and write them back so
the runtime self-corrects future generation — WITHOUT code changes.

Mirrors the proven `_learn_domain_overlay` discipline (read → stage → gate →
write-back → log → rollback), with two safety levels (locked design decision):

  * DEFAULT (approval): mine + stage candidates into `self_evolution_candidates`
    (status='proposed'). NOTHING reaches the active few-shot tables until a human
    promotes it (CLI/voice). Fail-safe.
  * AUTO (`DA_SELF_EVOLVE=1`): additionally apply staged candidates, run the
    baseline-lock eval suites, and KEEP them only if the suites pass (no regression);
    otherwise revert. Every promotion is logged to `adaptation_log` for rollback.

This is an offline batch job (scripts/run_self_evolve.ps1), never a runtime loop —
it must not run on the 60 Hz path and uses the eval harness as its guardrail.

Signals mined:
  * commands.corrected_to — user corrections: the domain-correct, runtime-consumed
    signal → command-domain example (right action) + counterexample (wrong action).
  * dev_escalations — repeated plan failures → 'plan'-domain counterexample
    candidates, STAGED for review (the planner does not yet inject few-shot, so
    these are captured for visibility rather than auto-applied).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Optional

log = logging.getLogger(__name__)

# The eval suites that guard a command-domain promotion (baseline-lock).
_GATE_SUITES = ("router_domains", "command_verbs")


class _StubCmd:
    """Minimal Command-like object for the few-shot upsert API (reads .text/.source)."""
    def __init__(self, text: str, source: str = "self_evolution") -> None:
        self.text = text
        self.source = source


class SelfEvolutionPipeline:
    def __init__(self, agent_db, *, eval_gate=None, auto_promote: Optional[bool] = None,
                 corrections_limit: int = 200, escalations_limit: int = 20) -> None:
        self._db = agent_db
        self._eval_gate = eval_gate or self._default_eval_gate
        self._auto = (auto_promote if auto_promote is not None
                      else os.getenv("DA_SELF_EVOLVE") == "1")
        self._corrections_limit = corrections_limit
        self._escalations_limit = escalations_limit

    # ── Mining ────────────────────────────────────────────────────────────────

    async def mine(self) -> list[dict]:
        """Return synthesized candidate dicts (not yet staged). Deduped within a pass."""
        out: list[dict] = []
        seen: set[tuple] = set()

        def _add(kind, text, action_or_wrong, domain, reason, refs):
            text = (text or "").strip()
            action_or_wrong = (action_or_wrong or "").strip()
            if not text or not action_or_wrong:
                return
            key = (kind, text, action_or_wrong)
            if key in seen:
                return
            seen.add(key)
            out.append({"kind": kind, "text": text, "action_or_wrong": action_or_wrong,
                        "domain": domain, "reason": reason, "source_refs": json.dumps(refs)})

        # Corrections → command-domain example + counterexample.
        for row in await self._db.get_recent_corrections(self._corrections_limit):
            text = row.get("text")
            right = (row.get("corrected_to") or "").strip()
            wrong = (row.get("action") or "").strip()
            cid = row.get("id")
            if right:
                _add("example", text, right, "command", "user_correction", {"command_ids": [cid]})
            if wrong and wrong != right:
                _add("counterexample", text, wrong, "command", "user_correction",
                     {"command_ids": [cid]})

        # Escalations → plan-domain counterexample (staged for review).
        for esc in await self._db.get_pending_escalations(self._escalations_limit):
            _add("counterexample", esc.get("goal"),
                 esc.get("failed_action") or "PLAN_FAILED", "plan", "escalation",
                 {"escalation_ids": [esc.get("id")]})
        return out

    # ── Run (mine → stage → optional auto-gate) ────────────────────────────────

    async def run(self) -> dict:
        candidates = await self.mine()
        staged: list[tuple[int, dict]] = []
        for c in candidates:
            cid = await self._db.insert_evolution_candidate(
                c["kind"], c["text"], c["action_or_wrong"],
                domain=c["domain"], reason=c["reason"], source_refs=c["source_refs"],
            )
            if cid:
                staged.append((cid, c))

        report = {"mined": len(candidates), "staged": len(staged),
                  "auto": self._auto, "promoted": [], "verdict": None}

        if not (self._auto and staged):
            log.info("SelfEvolution: staged %d candidate(s) for review (approval mode)",
                     len(staged))
            return report

        # AUTO: apply → gate → keep or revert.
        for _cid, c in staged:
            await self._apply(c)
        verdict, delta = await self._eval_gate()
        report["verdict"] = verdict
        if verdict == "pass":
            row_id = await self._db.log_adaptation(
                "self_evolve", metric_before=0.0, metric_after=float(delta or 0.0))
            for cid, c in staged:
                await self._db.set_evolution_candidate_status(cid, "promoted", delta)
            report["promoted"] = [cid for cid, _ in staged]
            report["adaptation_id"] = row_id
            log.info("SelfEvolution: gate PASS — promoted %d candidate(s)", len(staged))
        else:
            # fail → revert + reject; skip → revert + leave proposed (unverifiable).
            for cid, c in staged:
                await self._revert(c)
                await self._db.set_evolution_candidate_status(
                    cid, "rejected" if verdict == "fail" else "proposed")
            log.warning("SelfEvolution: gate %s — reverted %d candidate(s)",
                        verdict, len(staged))
        return report

    # ── Human-approval path (CLI / voice) ──────────────────────────────────────

    async def list_pending(self, limit: int = 100) -> list[dict]:
        return await self._db.get_evolution_candidates("proposed", limit)

    async def promote(self, candidate_id: int) -> bool:
        """Apply one staged candidate to the active few-shot tables + mark promoted."""
        for c in await self._db.get_evolution_candidates("proposed", 1000):
            if c["id"] == candidate_id:
                await self._apply(c)
                await self._db.set_evolution_candidate_status(candidate_id, "promoted")
                await self._db.log_adaptation("self_evolve", 0.0, 0.0)
                log.info("SelfEvolution: promoted candidate %d (manual)", candidate_id)
                return True
        return False

    async def reject(self, candidate_id: int) -> bool:
        await self._db.set_evolution_candidate_status(candidate_id, "rejected")
        return True

    # ── Apply / revert against the active few-shot tables ───────────────────────

    async def _apply(self, c: dict) -> None:
        cmd = _StubCmd(c["text"])
        if c["kind"] == "example":
            await self._db.upsert_few_shot_example(cmd, c["action_or_wrong"], c["domain"])
        else:
            await self._db.upsert_few_shot_counterexample(
                cmd, c["action_or_wrong"], domain=c["domain"],
                reason="user_correction" if c.get("reason") == "user_correction"
                else "self_evolution")

    async def _revert(self, c: dict) -> None:
        if c["kind"] == "example":
            await self._db.delete_few_shot_example(c["text"], c["action_or_wrong"])
        else:
            await self._db.delete_few_shot_counterexample(c["text"], c["action_or_wrong"])

    # ── Default eval gate (subprocess; tests inject their own) ──────────────────

    async def _default_eval_gate(self) -> tuple[str, float]:
        """Run the baseline-lock suites. Returns (verdict, delta).

        verdict: 'pass' (all suites exit 0), 'fail' (any exit 1 = regression),
        'skip' (no fail but a suite was unrunnable, e.g. model backend down → exit 2).
        delta is left 0.0 (the harness prints accuracy; not parsed here).
        """
        codes: list[int] = []
        for suite in _GATE_SUITES:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "python", "-m", "evals.run", "--suite", suite, "--check",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                await proc.communicate()
                codes.append(proc.returncode if proc.returncode is not None else 2)
            except Exception as exc:
                log.warning("SelfEvolution eval gate (%s) error: %s", suite, exc)
                codes.append(2)
        if 1 in codes:
            return "fail", 0.0
        if all(c == 0 for c in codes):
            return "pass", 0.0
        return "skip", 0.0


# ─────────────────────────────────────────────────────────────────────────────
# CLI entrypoint (scripts/run_self_evolve.ps1 calls this)
# ─────────────────────────────────────────────────────────────────────────────

async def _amain(argv) -> int:
    import argparse
    from pathlib import Path
    from storage.db import AgentDB

    p = argparse.ArgumentParser(description="Self-evolution pipeline (R-3, offline).")
    p.add_argument("--db", default=str(Path.home() / ".claude" / "agent.db"))
    p.add_argument("--list", action="store_true", help="list pending candidates and exit")
    p.add_argument("--promote", type=int, metavar="ID", help="promote one candidate")
    p.add_argument("--reject", type=int, metavar="ID", help="reject one candidate")
    p.add_argument("--auto", action="store_true",
                   help="force eval-gated auto-promote (same as DA_SELF_EVOLVE=1)")
    args = p.parse_args(argv)

    db = AgentDB()
    await db.open(args.db)
    try:
        pipe = SelfEvolutionPipeline(db, auto_promote=True if args.auto else None)
        if args.list:
            for c in await pipe.list_pending():
                print(f"[{c['id']}] {c['kind']}/{c['domain']}: "
                      f"{c['text']!r} -> {c['action_or_wrong']!r} ({c.get('reason')})")
            return 0
        if args.promote is not None:
            ok = await pipe.promote(args.promote)
            print("promoted" if ok else "not found"); return 0 if ok else 1
        if args.reject is not None:
            await pipe.reject(args.reject); print("rejected"); return 0
        report = await pipe.run()
        print(json.dumps(report, indent=2))
        return 0
    finally:
        await db.close()


def main() -> None:
    import sys
    raise SystemExit(asyncio.run(_amain(sys.argv[1:])))


if __name__ == "__main__":
    main()
