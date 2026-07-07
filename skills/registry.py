"""SkillRegistry — the agent's MCP-client skill system (N+1 keystone).

A skill is an external MCP server declared by a JSON manifest
(skills/manifests/*.json). At startup the registry launches each enabled server
(stdio), discovers its tools, and exposes them to the DevAgent via two universal
verbs — SKILL_QUERY (read) and SKILL_CALL (egress/"send", gated like a
destructive op). Adding a capability is a manifest + credential; it never edits
core/command_executor.py or any per-feature LLM prompt.

Trust model:
  - Inbound results from a skill are UNTRUSTED data — the DevAgent taint-checks
    every SKILL_QUERY result before it touches a prompt.
  - Outbound "send" payloads are scrubbed (ContentFilter) and require the
    fail-safe-DENY voice gate (DevAgent._confirm_destructive_op).
The registry itself owns the sessions and per-skill serialization; the security
flow lives in DevAgent._execute_skill_step.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_MANIFEST_DIR = Path(__file__).resolve().parent / "manifests"


@dataclass
class _Skill:
    manifest: dict
    session: Any                 # mcp.ClientSession
    tools: dict                  # tool_name -> discovered Tool schema
    lock: asyncio.Lock


class SkillRegistry:
    """Owns the MCP client sessions for every enabled skill manifest."""

    def __init__(
        self,
        *,
        manifest_dir: "str | Path | None" = None,
        content_filter=None,
        trust_classifier=None,
        audit_log=None,
        agent_db=None,
    ) -> None:
        self._manifest_dir = Path(manifest_dir) if manifest_dir else _DEFAULT_MANIFEST_DIR
        self._content_filter = content_filter
        self._trust_classifier = trust_classifier
        self._audit = audit_log
        self._agent_db = agent_db
        self._skills: dict[str, _Skill] = {}
        self._stack: Optional[AsyncExitStack] = None
        self._started = False

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        """Launch every enabled skill's MCP server and discover its tools.

        A skill that fails to start is logged and skipped — it never blocks the
        agent. Idempotent.
        """
        if self._started:
            return
        self._stack = AsyncExitStack()
        for manifest in self._load_manifests():
            if not manifest.get("enabled", False):
                continue
            sid = manifest.get("skill_id", "?")
            try:
                await self._start_skill(manifest)
                log.info("SkillRegistry: started skill %s (%d tools)",
                         sid, len(self._skills[sid].tools))
            except Exception as exc:
                log.warning("SkillRegistry: skill %s failed to start: %s", sid, exc)
        self._started = True
        # Make skill intents routable by registering their keywords into the
        # shared (class-level) DomainClassifier vocabulary.
        try:
            from core.domain_classifier import DomainClassifier
            DomainClassifier.register_skill_keywords(self.skill_keywords())
        except Exception as exc:
            log.debug("SkillRegistry: keyword registration skipped: %s", exc)

    async def _start_skill(self, manifest: dict) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        server = manifest["server"]
        # Launch the skill server under the SAME interpreter that runs the agent
        # (sys.executable = the venv), not whatever bare "python" resolves to on
        # PATH. Relying on PATH split the runtime across two interpreters (the
        # venv that runs main.py vs. the system Python that "python" resolved to),
        # so a dependency installed for the agent was missing for the skill
        # subprocess — and vice-versa. One interpreter, one dependency set.
        command = server["command"]
        if command in ("python", "python3", "python.exe", "python3.exe"):
            command = sys.executable
        params = StdioServerParameters(
            command=command,
            args=server.get("args", []),
            env=dict(os.environ),
            cwd=server.get("cwd") or str(_REPO_ROOT),
        )
        assert self._stack is not None
        read, write = await self._stack.enter_async_context(stdio_client(params))
        session = await self._stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        result = await session.list_tools()
        allow = set(manifest.get("tools", {}).get("allow", []))
        tools = {t.name: t for t in result.tools if (not allow or t.name in allow)}
        self._skills[manifest["skill_id"]] = _Skill(
            manifest=manifest, session=session, tools=tools, lock=asyncio.Lock()
        )

    async def start_skill(self, skill_id: str) -> bool:
        """Hot-start one skill after the registry is already running.

        Used by the voice "connect Google" flow: after a successful OAuth the
        skill becomes usable WITHOUT restarting the agent. Re-reads the
        manifest (so a fresh enabled override applies), starts the server, and
        re-registers the merged intent keywords. True on success or if already
        running.
        """
        if skill_id in self._skills:
            return True
        if self._stack is None:
            self._stack = AsyncExitStack()
        manifest = next((m for m in self._load_manifests()
                         if m.get("skill_id") == skill_id), None)
        if manifest is None or not manifest.get("enabled", False):
            log.warning("SkillRegistry.start_skill: %s missing or disabled", skill_id)
            return False
        try:
            await self._start_skill(manifest)
        except Exception as exc:
            log.warning("SkillRegistry.start_skill: %s failed: %s", skill_id, exc)
            return False
        try:
            from core.domain_classifier import DomainClassifier
            DomainClassifier.register_skill_keywords(self.skill_keywords())
        except Exception as exc:
            log.debug("SkillRegistry.start_skill: keyword refresh skipped: %s", exc)
        log.info("SkillRegistry: hot-started skill %s (%d tools)",
                 skill_id, len(self._skills[skill_id].tools))
        return True

    async def stop(self) -> None:
        """Tear down every skill session and its stdio subprocess."""
        if self._stack is not None:
            try:
                await self._stack.aclose()
            except Exception as exc:
                log.debug("SkillRegistry: stop error: %s", exc)
        self._skills.clear()
        self._stack = None
        self._started = False

    # ------------------------------------------------------------------ #
    # Invocation
    # ------------------------------------------------------------------ #

    async def call(self, skill_id: str, tool_name: str,
                   args: Optional[dict] = None) -> dict:
        """Invoke a discovered tool. Returns {status, text}.

        Serialised per skill (one session, one in-flight call). Forbidden/unknown
        tools and call errors return a structured error rather than raising.
        """
        skill = self._skills.get(skill_id)
        if skill is None:
            return {"status": "error", "error": f"unknown skill: {skill_id}"}
        if tool_name not in skill.tools:
            return {"status": "error",
                    "error": f"unknown or forbidden tool: {skill_id}.{tool_name}"}
        try:
            async with skill.lock:
                result = await skill.session.call_tool(tool_name, args or {})
        except Exception as exc:
            log.warning("SkillRegistry.call %s.%s failed: %s", skill_id, tool_name, exc)
            return {"status": "error", "error": str(exc)}
        return {
            "status": "error" if getattr(result, "isError", False) else "ok",
            "text": _extract_text(result),
        }

    # ------------------------------------------------------------------ #
    # Routing / introspection
    # ------------------------------------------------------------------ #

    def match_intent(self, text: str) -> Optional[dict]:
        """Resolve an utterance to the best (skill_id, tool, send) via manifest
        intent keywords. Longest keyword wins. None if nothing matches."""
        tl = (text or "").lower()
        best: Optional[dict] = None
        for sid, skill in self._skills.items():
            for iname, intent in skill.manifest.get("intents", {}).items():
                tool = intent.get("tool")
                if tool not in skill.tools:
                    continue
                for kw in intent.get("keywords", []):
                    if kw.lower() in tl:
                        score = len(kw)
                        if best is None or score > best["score"]:
                            best = {"skill_id": sid, "tool": tool,
                                    "send": bool(intent.get("send", False)),
                                    "summarize": bool(intent.get("summarize", False)),
                                    # plan=true: the tool needs LLM-generated
                                    # input (e.g. diagram source), so the intent
                                    # routes to plan_and_run instead of a direct
                                    # single-turn call.
                                    "plan": bool(intent.get("plan", False)),
                                    "intent": iname, "keyword": kw, "score": score}
        return best

    def tool_schema(self, skill_id: str, tool_name: str) -> dict:
        """The discovered JSON input schema for a tool (empty dict if unknown)."""
        skill = self._skills.get(skill_id)
        if skill is None or tool_name not in skill.tools:
            return {}
        return getattr(skill.tools[tool_name], "inputSchema", {}) or {}

    def is_send_tool(self, skill_id: str, tool_name: str) -> bool:
        """True if the tool is an egress/mutation tool (gated). Fail-safe: an
        unknown skill/tool is treated as a send (so it gets gated)."""
        skill = self._skills.get(skill_id)
        if skill is None:
            return True
        return tool_name in set(skill.manifest.get("tools", {}).get("send_tools", []))

    def skill_keywords(self) -> set[str]:
        """Union of all intent keywords (lower-cased) for classifier routing."""
        kws: set[str] = set()
        for skill in self._skills.values():
            for intent in skill.manifest.get("intents", {}).values():
                for kw in intent.get("keywords", []):
                    kws.add(kw.lower())
        return kws

    def list_actions(self) -> list[dict]:
        """All routable (skill, intent → tool) mappings across enabled skills."""
        out: list[dict] = []
        for sid, skill in self._skills.items():
            for iname, intent in skill.manifest.get("intents", {}).items():
                out.append({
                    "skill_id": sid, "intent": iname, "tool": intent.get("tool"),
                    "send": bool(intent.get("send", False)),
                    "keywords": intent.get("keywords", []),
                })
        return out

    def describe_for_prompt(self) -> str:
        """A data-driven 'Available skills' block for the planner prompt. Empty
        string when no skills are loaded (so the prompt is unchanged)."""
        if not self._skills:
            return ""
        lines = [
            "Available skills — call a read tool with SKILL_QUERY and a send/mutation",
            "tool with SKILL_CALL. Step args format: `<skill_id> <tool> {<json args>}`.",
        ]
        for sid, skill in self._skills.items():
            send_tools = set(skill.manifest.get("tools", {}).get("send_tools", []))
            for tname, tool in skill.tools.items():
                desc = (getattr(tool, "description", "") or "").strip().splitlines()
                desc = desc[0] if desc else ""
                tag = " [SEND — requires voice approval]" if tname in send_tools else ""
                lines.append(f"  - {sid} {tname}{tag}: {desc}")
        return "\n".join(lines)

    @property
    def started(self) -> bool:
        return self._started

    def has_skills(self) -> bool:
        return bool(self._skills)

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _load_manifests(self) -> list[dict]:
        """Load all manifests, applying the user's enabled-state overrides.

        ~/.claude/skills/enabled.json holds {skill_id: bool} — USER state, so
        enabling a skill (e.g. google_pim after OAuth) never edits the
        checked-in manifest. Overrides win over the manifest's enabled flag.
        """
        manifests: list[dict] = []
        if not self._manifest_dir.is_dir():
            return manifests
        try:
            from skills.google_setup import load_enabled_overrides
            overrides = load_enabled_overrides()
        except Exception:
            overrides = {}
        for p in sorted(self._manifest_dir.glob("*.json")):
            try:
                m = json.loads(p.read_text(encoding="utf-8"))
                sid = m.get("skill_id")
                if sid in overrides:
                    m["enabled"] = overrides[sid]
                manifests.append(m)
            except (OSError, ValueError) as exc:
                log.warning("SkillRegistry: bad manifest %s: %s", p, exc)
        return manifests


def _extract_text(result) -> str:
    """Pull text out of an MCP CallToolResult's content blocks."""
    parts: list[str] = []
    for block in getattr(result, "content", []) or []:
        t = getattr(block, "text", None)
        if t:
            parts.append(t)
    return "\n".join(parts)
