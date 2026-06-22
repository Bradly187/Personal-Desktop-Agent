"""MacroStore — self-skilling rung 2 (promoted-macro registry + safe replay).

A *macro* is a human-approved recurring plan (staged by MacroDetector, promoted
by the approval flow). MacroStore loads promoted macros, routes an utterance to
one (keyword match, mirroring SkillRegistry.match_intent), and replays it by
dispatching each constituent step through the **normal CommandExecutor** — no new
execution mechanism, no gate bypass (spec R5.2).

Safety (spec R5.3): before executing ANY step, replay verifies every step's
verb/tool still exists (a skill tool may have been disabled since promotion). If
one is missing — or a required parameter is unsupplied — replay fails safe with a
CLARIFY and runs nothing. A mid-sequence step error stops replay; it never
silently completes a partial macro.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class Macro:
    name: str
    signature: str
    steps: list[dict]                       # [{action, arg, param: bool}, ...]
    keywords: list[str] = field(default_factory=list)
    domain: str = "command"
    candidate_id: Optional[int] = None

    @property
    def param_positions(self) -> list[int]:
        return [i for i, s in enumerate(self.steps) if s.get("param")]


def _clarify(reason: str) -> dict:
    return {"status": "clarify", "action": "CLARIFY", "reason": reason}


class MacroStore:
    """Registry of promoted macros + their safe replay."""

    def __init__(self, *, skill_registry=None, known_verbs: Optional[set[str]] = None):
        self._skills = skill_registry
        self._known_verbs = known_verbs
        self._macros: dict[str, Macro] = {}

    # ------------------------------------------------------------------ #
    # Registration
    # ------------------------------------------------------------------ #

    async def load_promoted(self, db) -> int:
        """Load every promoted macro candidate. Returns how many registered."""
        try:
            rows = await db.get_evolution_candidates(status="promoted", kind="macro")
        except Exception as exc:
            log.warning("MacroStore.load_promoted failed: %s", exc)
            return 0
        n = 0
        for row in rows:
            if self.register(row) is not None:
                n += 1
        return n

    def register(self, candidate: dict, *, name: Optional[str] = None,
                 keywords: Optional[list[str]] = None) -> Optional[Macro]:
        """Build + register a Macro from a promoted candidate row.

        Returns None (registers nothing) if the candidate's shape is ambiguous —
        a "*" action means two clustered runs disagreed at that position, so the
        sequence isn't safely replayable (R5.3 conservative).
        """
        macro = self._build(candidate, name=name, keywords=keywords)
        if macro is None:
            return None
        self._macros[macro.name] = macro
        return macro

    @staticmethod
    def _build(candidate: dict, *, name: Optional[str], keywords: Optional[list[str]]) -> Optional[Macro]:
        try:
            refs = json.loads(candidate.get("source_refs") or "{}")
        except (ValueError, TypeError):
            refs = {}
        descriptors = refs.get("steps", [])
        if not descriptors:
            return None
        steps: list[dict] = []
        for d in descriptors:
            action = (d.get("action") or "").upper()
            if action in ("", "*"):
                return None  # ambiguous → not safely replayable
            values = d.get("values", []) or []
            steps.append({
                "action": action,
                "arg": values[0] if len(values) == 1 else None,
                "param": len(values) > 1,
            })
        macro_name = (name or candidate.get("text") or candidate.get("action_or_wrong") or "macro").strip()
        return Macro(
            name=macro_name,
            signature=candidate.get("action_or_wrong", ""),
            steps=steps,
            keywords=[k.lower() for k in (keywords or [macro_name.lower()])],
            domain=candidate.get("domain", "command"),
            candidate_id=candidate.get("id"),
        )

    # ------------------------------------------------------------------ #
    # Routing
    # ------------------------------------------------------------------ #

    def match(self, text: str) -> Optional[Macro]:
        """Resolve an utterance to a macro by keyword (longest keyword wins)."""
        tl = (text or "").lower()
        best: Optional[Macro] = None
        best_len = 0
        for macro in self._macros.values():
            for kw in macro.keywords:
                if kw and kw in tl and len(kw) > best_len:
                    best, best_len = macro, len(kw)
        return best

    def list_macros(self) -> list[Macro]:
        return list(self._macros.values())

    # ------------------------------------------------------------------ #
    # Replay
    # ------------------------------------------------------------------ #

    def _current_tools(self) -> set[str]:
        verbs = set(self._known_verbs) if self._known_verbs is not None else set()
        if not verbs:
            try:
                from inference.dev_agent import _PLAN_ACTIONS
                verbs = set(_PLAN_ACTIONS)
            except Exception:
                verbs = set()
        if self._skills is not None:
            try:
                verbs |= {a.get("tool") for a in self._skills.list_actions()}
            except Exception:
                pass
        return verbs

    async def replay(self, macro, executor, params: Optional[dict] = None) -> dict:
        """Replay a macro through `executor`. Fail-safe per R5.3.

        `macro` may be a Macro or a registered name. `params` maps a step position
        (int) to a concrete argument for parameterized slots.
        """
        if isinstance(macro, str):
            macro = self._macros.get(macro)
        if macro is None:
            return _clarify("unknown macro")
        params = params or {}

        # R5.3 — verify every tool exists BEFORE executing any step.
        current = self._current_tools()
        for s in macro.steps:
            if s["action"] not in current:
                return _clarify(
                    f"the macro step '{s['action']}' is no longer available")
        # Required parameters must be supplied up front (no partial run).
        for i in macro.param_positions:
            if i not in params:
                return _clarify(f"the macro needs a value for step {i + 1}")

        from core.command_executor import Command
        for i, s in enumerate(macro.steps):
            arg = params.get(i) if s["param"] else s["arg"]
            cmd = Command(
                text=arg if arg is not None else macro.name,
                action=s["action"],
                source="macro",
                params={"arg": arg} if arg is not None else {},
            )
            result = await executor.execute(cmd)
            if (result or {}).get("status") == "error":
                return {"status": "error", "step": i, "action": s["action"],
                        "result": result, "steps_run": i}
        return {"status": "ok", "macro": macro.name, "steps_run": len(macro.steps)}
