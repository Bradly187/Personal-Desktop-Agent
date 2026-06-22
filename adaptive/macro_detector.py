"""MacroDetector — self-skilling rung 2 (offline macro mining).

Mines the agent's own successful trajectories (agent_runs ⨝ agent_steps) for
recurring multi-step plans and stages each as a `kind="macro"` candidate in
self_evolution_candidates (status='proposed'). A human promotes it later
(commit 4); nothing here enables anything.

It is an OFFLINE miner — it must run in a supervised background task, never on
the 60 Hz fusion loop (AGENTS.md #2) — and it skips entirely during a flare
(AGENTS.md #5). It performs NO LLM inference: detection is deterministic
signature clustering.

Algorithm (specs/self-skilling/design.md §2):
  1. Reduce each successful run to a *plan signature* — the verb sequence with
     literal arguments abstracted to typed slots (so two runs differing only in
     their arguments collapse to one shape).
  2. Cluster runs by identical signature (exact); optionally merge near-identical
     signatures (single-slot difference) when `similarity` < 1.0.
  3. A cluster with >= `min_occurrences` distinct runs and >= `min_steps` steps,
     all of whose verbs/tools currently exist, becomes a macro candidate.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import Counter
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Optional

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from adaptive.behavioral_twin_state import BehavioralTwinState
    from skills.registry import SkillRegistry
    from storage.db import AgentDB

# Typed argument slots — literals are abstracted to these so a macro shape is
# argument-independent. "*" is the generalized slot used when a fuzzy merge
# collapses positions whose slot type disagreed across runs.
_SLOT_EMPTY = "∅"
_PATH_RE = re.compile(r"[\\/]|\.\w{1,5}$")
_INT_RE = re.compile(r"^-?\d+$")


def classify_arg(args: Optional[str]) -> str:
    """Abstract a step's literal argument to a typed slot."""
    if args is None:
        return _SLOT_EMPTY
    s = str(args).strip()
    if not s:
        return _SLOT_EMPTY
    low = s.lower()
    if low.startswith(("http://", "https://", "www.")):
        return "URL"
    if _PATH_RE.search(s):
        return "PATH"
    if _INT_RE.match(s):
        return "INT"
    return "STR"


def canonicalize_step(action: str, args: Optional[str]) -> str:
    """One trajectory step → a signature token, e.g. ``OPEN(STR)``."""
    return f"{(action or '').upper()}({classify_arg(args)})"


def plan_signature(steps: list[dict]) -> str:
    """The ordered, argument-abstracted shape of a run's trajectory."""
    return " → ".join(canonicalize_step(s.get("action"), s.get("args")) for s in steps)


class MacroDetector:
    """Offline miner that stages recurring plans as macro candidates."""

    def __init__(
        self,
        agent_db: "AgentDB",
        *,
        twin_state: Optional["BehavioralTwinState"] = None,
        skill_registry: Optional["SkillRegistry"] = None,
        known_verbs: Optional[set[str]] = None,
        min_occurrences: int = 4,
        min_steps: int = 2,
        similarity: float = 0.9,
        lookback_s: float = 30 * 86400.0,
        run_limit: int = 500,
        interval_s: float = 3600.0,
    ) -> None:
        self._db = agent_db
        self._twin = twin_state
        self._skills = skill_registry
        self._known_verbs = known_verbs
        self._min_occ = max(2, int(min_occurrences))
        self._min_steps = max(2, int(min_steps))
        self._similarity = float(similarity)
        self._lookback_s = float(lookback_s)
        self._run_limit = int(run_limit)
        self._interval = float(interval_s)
        self._running = False
        self._task: Optional[asyncio.Task] = None

    # ---------------------------------------------------------------------- #
    # Lifecycle (supervised offline loop; main.py wiring is a later commit)
    # ---------------------------------------------------------------------- #

    async def start(self) -> None:
        if not getattr(self._db, "available", False):
            log.warning("MacroDetector: AgentDB unavailable — not starting.")
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        log.info("MacroDetector started (interval=%.0fs, min_occ=%d)",
                 self._interval, self._min_occ)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def _loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self._interval)
                if not self._running:
                    break
                staged = await self.run_once()
                if staged:
                    log.info("MacroDetector: staged %d macro candidate(s)", len(staged))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # never let the miner crash the agent
                log.warning("MacroDetector loop error: %s", exc)

    # ---------------------------------------------------------------------- #
    # Detection
    # ---------------------------------------------------------------------- #

    async def run_once(self) -> list[int]:
        """One offline detection pass. Returns the staged candidate ids.

        Returns [] (and stages nothing) while a flare is active — AGENTS.md #5.
        """
        if await self._flare_active():
            log.debug("MacroDetector: flare active — skipping run.")
            return []
        since = max(0.0, time.time() - self._lookback_s)
        runs = await self._db.get_successful_runs_with_steps(
            since=since, min_steps=self._min_steps, limit=self._run_limit,
        )
        if not runs:
            return []
        clusters = self._cluster(runs)
        staged: list[int] = []
        for signature, members in sorted(clusters.items()):
            if len({m["id"] for m in members}) < self._min_occ:
                continue
            if not self._tools_exist(members[0]["steps"]):
                continue  # R1.3 — never stage a macro referencing a missing tool
            cid = await self._stage(signature, members)
            if cid:
                staged.append(cid)
        return staged

    async def _flare_active(self) -> bool:
        if self._twin is None:
            return False
        try:
            snap = await self._twin.get_snapshot()
            return bool(getattr(snap, "pain_day_active", False))
        except Exception as exc:
            log.debug("MacroDetector: twin snapshot failed (%s) — assuming no flare", exc)
            return False

    def _cluster(self, runs: list[dict]) -> dict[str, list[dict]]:
        """Group runs by plan signature (exact), then optionally fuzzy-merge."""
        exact: dict[str, list[dict]] = {}
        for run in runs:
            sig = plan_signature(run["steps"])
            run["_sig"] = sig
            exact.setdefault(sig, []).append(run)
        if self._similarity >= 1.0 or len(exact) < 2:
            return exact
        return self._merge_similar(exact)

    def _merge_similar(self, exact: dict[str, list[dict]]) -> dict[str, list[dict]]:
        """Conservatively merge signatures that differ in a single slot.

        Only equal-length token sequences are merged (so step positions stay
        aligned for parameter inference at promotion time); the larger cluster's
        signature is kept as the canonical key.
        """
        # Largest clusters first so they become the merge anchors (deterministic).
        keys = sorted(exact, key=lambda k: (-len(exact[k]), k))
        merged: dict[str, list[dict]] = {}
        consumed: set[str] = set()
        for key in keys:
            if key in consumed:
                continue
            bucket = list(exact[key])
            ktoks = key.split(" → ")
            for other in keys:
                if other == key or other in consumed:
                    continue
                otoks = other.split(" → ")
                if len(otoks) != len(ktoks):
                    continue
                if SequenceMatcher(None, ktoks, otoks).ratio() >= self._similarity:
                    bucket.extend(exact[other])
                    consumed.add(other)
            consumed.add(key)
            merged[key] = bucket
        return merged

    def _verb_set(self) -> set[str]:
        if self._known_verbs is not None:
            return self._known_verbs
        try:
            from inference.dev_agent import _PLAN_ACTIONS
            self._known_verbs = set(_PLAN_ACTIONS)
        except Exception:
            self._known_verbs = set()
        return self._known_verbs

    def _tools_exist(self, steps: list[dict]) -> bool:
        verbs = self._verb_set()
        skill_tools: set[str] = set()
        if self._skills is not None:
            try:
                skill_tools = {a.get("tool") for a in self._skills.list_actions()}
            except Exception:
                skill_tools = set()
        for s in steps:
            action = (s.get("action") or "").upper()
            if action not in verbs and action not in skill_tools:
                return False
        return True

    async def _stage(self, signature: str, members: list[dict]) -> Optional[int]:
        run_ids = sorted({m["id"] for m in members})
        # Most common goal/domain across the cluster → the macro's name + domain.
        goal = Counter(m.get("goal", "") for m in members).most_common(1)[0][0]
        domain = Counter(m.get("domain", "command") for m in members).most_common(1)[0][0]
        descriptors = self._step_descriptors(members)
        source_refs = json.dumps({
            "run_ids": run_ids,
            "occurrences": len(run_ids),
            "steps": descriptors,
        })
        return await self._db.insert_evolution_candidate(
            "macro", goal or signature, signature,
            domain=domain, reason="recurring_plan", source_refs=source_refs,
        )

    @staticmethod
    def _step_descriptors(members: list[dict]) -> list[dict]:
        """Per-position action + slot + distinct literal values across the cluster.

        Promotion (later commit) uses this to decide which slots are macro
        parameters (>= 2 distinct values) vs. baked-in defaults (design §2.1).
        Aligned by step position; the shortest member bounds the length.
        """
        if not members:
            return []
        n = min(len(m["steps"]) for m in members)
        out: list[dict] = []
        for i in range(n):
            actions = {(m["steps"][i].get("action") or "").upper() for m in members}
            values = sorted({
                (m["steps"][i].get("args") or "") for m in members
            } - {""})
            out.append({
                "pos": i,
                "action": actions.pop() if len(actions) == 1 else "*",
                "slot": classify_arg(members[0]["steps"][i].get("args")),
                "values": values[:10],
            })
        return out
