"""WorkflowHandler — voice workflow trigger, conversation mode, and macro
save/replay extracted from HybridCoordinator.

Owns:
  - maybe_handle_workflow: the voice "think hard about …" / "research …"
    trigger — decompose → fan_out sub-agents → synthesize → speak.
  - maybe_handle_conversation: wake/sleep-gated conversation-mode dialogue.
  - maybe_handle_macro / handle_macro_save / handle_macro_replay /
    note_pending_macro: self-skilling rung 2 (macro detection, "save that as
    …" promotion, and replay through CommandExecutor).

All cross-object coordinator state is injected as zero-arg accessor callables
resolved at call time, matching GateEvaluator/InferenceRunner/ActionExecutor —
several of these (workflow_runner, dev_agent, macro_store) are wired onto
HybridCoordinator after construction via a set_* method, and tests routinely
set attributes like ``c._wf_cfg`` / ``c._twin`` directly on a bare
``HybridCoordinator.__new__`` instance. ``pending_macro`` is owned entirely by
this module (never read outside these methods on the original class).
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable, Optional

from core.workflow_voice import (
    parse_workflow_request,
    parse_decomposition,
    build_decompose_prompt,
    build_synthesis_prompt,
    fanout_n_from_config,
    verify_enabled,
)

log = logging.getLogger(__name__)


from typing import Any, Callable, Coroutine, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from core.event_dispatcher import Command

class WorkflowHandler:
    def __init__(
        self,
        *,
        workflow_runner: Callable[[], Any],
        dev_agent: Callable[[], Any],
        wf_cfg: Callable[[], dict],
        twin: Callable[[], Any],
        conv_mode: Callable[[], Any],
        macro_store: Callable[[], Any],
        agent_db: Callable[[], Any],
        executor: Callable[[], Any],
        tts_speak: Callable[[str], Coroutine[Any, Any, None]],
        speak_and_suppress: Callable[[str], Coroutine[Any, Any, None]],
    ) -> None:
        self._workflow_runner = workflow_runner
        self._dev_agent = dev_agent
        self._wf_cfg = wf_cfg
        self._twin = twin
        self._conv_mode = conv_mode
        self._macro_store = macro_store
        self._agent_db = agent_db
        self._executor = executor
        self._tts_speak = tts_speak
        self._speak_and_suppress = speak_and_suppress
        self._pending_macro: Optional[dict] = None  # {id, name} awaiting "save that as ..."

    # ---------------------------------------------------------------------- #
    # Voice workflow trigger ("think hard about …" / "research …")
    # ---------------------------------------------------------------------- #

    async def maybe_handle_workflow(self, cmd) -> Optional[dict]:
        """Handle a voice 'think hard about …' / 'research …' request.

        Decomposes the goal into N sub-angles, fans them out as fresh-context
        sub-agents via ``WorkflowRunner.fan_out``, then synthesizes + speaks one
        answer. **Pure talk — no desktop actions, no command-pipeline bypass.**
        Returns a handled result dict, or ``None`` to fall through to ordinary
        routing (not a trigger, a flare, or any error — fail-safe, AGENTS.md #4).
        """
        runner = self._workflow_runner()
        if runner is None or not runner.enabled:
            return None
        try:
            req = parse_workflow_request(cmd.text or "")
            if req is None:
                return None  # not a workflow request — ordinary routing
            # Skip-on-flare (AGENTS.md #5): a multi-call fan-out is heavy. During a
            # flare let the request fall through to the ordinary single-model path
            # so the user still gets an answer with minimal friction.
            if await self._workflow_flare_active():
                log.info("WorkflowHandler: workflow trigger skipped (flare) — "
                         "falling through to ordinary routing")
                return None
            dev_agent = self._dev_agent()
            router = getattr(dev_agent, "_router", None) if dev_agent else None
            if router is None:
                return None
            log.info("WorkflowHandler: workflow trigger — goal=%r", req.goal)
            n = fanout_n_from_config(self._wf_cfg())
            subtasks = await self._decompose_goal(router, req.goal, n)
            if not subtasks:
                return None
            verify_criterion = (
                f"The answer is a correct, relevant, substantive response to: {req.goal}"
                if verify_enabled(self._wf_cfg()) else None
            )
            result = await runner.fan_out(
                subtasks, name="voice_workflow", goal=req.goal,
                verify_criterion=verify_criterion,
            )
            # Keep successful (and, if verification ran, verified) sub-answers.
            texts = [
                r.text for r in result.results
                if r.ok and r.text.strip() and (r.verified is None or r.verified)
            ]
            if not texts:
                msg = "Sorry, I couldn't work that one out just now."
                await self._speak_and_suppress(msg)
                return {"status": "ok", "action": "WORKFLOW", "spoken": msg,
                        "source": cmd.source, "trace_id": cmd.trace_id}
            answer = await self._synthesize_workflow(router, req.goal, texts)
            await self._speak_and_suppress(answer)
            return {"status": "ok", "action": "WORKFLOW", "spoken": answer,
                    "subtasks": len(subtasks), "verified": result.verified_count,
                    "source": cmd.source, "trace_id": cmd.trace_id}
        except Exception as exc:  # never strand the user — fall through
            log.warning("WorkflowHandler: workflow handling failed (%s)", exc)
            return None

    async def _decompose_goal(self, router, goal: str, n: int) -> list:
        """Split ``goal`` into ``n`` sub-angles via the resident general model.

        Returns a list of ``SubTask``. Degrades to a single sub-agent on the raw
        goal if decomposition fails or yields nothing, so the feature always
        produces an answer rather than going silent."""
        from inference.workflow import SubTask
        try:
            r = await router.infer(
                domain="general", user_text=build_decompose_prompt(goal, n),
                context="",
            )
            if getattr(r, "ok", True):
                lines = parse_decomposition(getattr(r, "text", "") or "", n)
                if lines:
                    return [SubTask(name=f"angle{i + 1}", prompt=line)
                            for i, line in enumerate(lines)]
        except Exception as exc:
            log.debug("workflow: decomposition failed (%s) — single-angle fallback", exc)
        return [SubTask(name="angle1", prompt=goal)]

    async def _synthesize_workflow(self, router, goal: str, texts: list[str]) -> str:
        """Synthesize sub-answers into one concise spoken reply. Degrades to the
        first sub-answer (truncated) if the synthesis call fails."""
        try:
            r = await router.infer(
                domain="general", user_text=build_synthesis_prompt(goal, texts),
                context="",
            )
            ans = (getattr(r, "text", "") or "").strip()
            if getattr(r, "ok", True) and ans:
                return ans
        except Exception as exc:
            log.debug("workflow: synthesis failed (%s) — first-angle fallback", exc)
        return texts[0].strip()[:600]

    async def _workflow_flare_active(self) -> bool:
        """True if a pain-day flare is active (AGENTS.md #5). A twin-snapshot
        error fails safe to 'no flare' so a telemetry hiccup never silently
        disables the feature."""
        twin = self._twin()
        if twin is None:
            return False
        try:
            snap = await twin.get_snapshot()
            return bool(getattr(snap, "pain_day_active", False))
        except Exception as exc:
            log.debug("workflow: twin snapshot failed (%s) — assuming no flare", exc)
            return False

    # ---------------------------------------------------------------------- #
    # Conversation mode
    # ---------------------------------------------------------------------- #

    async def maybe_handle_conversation(self, cmd) -> Optional[dict]:
        """Handle a voice utterance under conversation mode.

        Returns a result dict when the utterance was consumed by conversation
        mode (entering, replying, or leaving), or ``None`` to let the normal
        command/dev pipeline handle it (mode inactive and not a wake phrase).
        Fail-safe: any error degrades to ``None`` so a fault never strands the
        user — the utterance just routes as an ordinary command.
        """
        try:
            text = (cmd.text or "").strip()
            if not text:
                return None
            conv = self._conv_mode()

            if not conv.active:
                matched, remainder = conv.match_wake(text)
                if not matched:
                    return None  # ordinary command — fall through to the pipeline
                conv.enter()
                log.info("WorkflowHandler: entered conversation mode")
                if remainder:
                    # "conversation mode, <question>" — wake word and the first
                    # turn arrived together; answer it straight away instead of
                    # speaking a greeting that would leave the user waiting.
                    reply = await self._converse(remainder)
                    conv.record("user", remainder)
                    conv.record("assistant", reply)
                    await self._speak_and_suppress(reply)
                    return {"status": "ok", "action": "CONVERSATION_START",
                            "spoken": reply, "source": cmd.source,
                            "trace_id": cmd.trace_id}
                await self._speak_and_suppress(
                    "Okay, I'm listening. Say 'that's all' when you're done."
                )
                return {"status": "ok", "action": "CONVERSATION_START",
                        "source": cmd.source, "trace_id": cmd.trace_id}

            # --- active conversation ---
            if conv.detect_sleep(text):
                conv.exit()
                log.info("WorkflowHandler: left conversation mode")
                await self._speak_and_suppress("Okay, talk to you later.")
                return {"status": "ok", "action": "CONVERSATION_END",
                        "source": cmd.source, "trace_id": cmd.trace_id}

            reply = await self._converse(text)
            conv.record("user", text)
            conv.record("assistant", reply)
            await self._speak_and_suppress(reply)
            return {"status": "ok", "action": "CONVERSATION_REPLY",
                    "spoken": reply, "source": cmd.source, "trace_id": cmd.trace_id}
        except Exception as exc:  # never strand the user mid-conversation
            log.warning("WorkflowHandler: conversation handling failed (%s)", exc)
            return None

    async def _converse(self, text: str) -> str:
        """Answer one conversational turn with the resident general model.

        Reuses ``ModelRouter.infer(domain="general", ...)`` (gemma4:12b — already
        resident, so no extra VRAM and no eviction churn, AGENTS.md #6), threading
        the running dialogue in as prompt context. Degrades to a spoken apology if
        no router is wired or inference fails — never raises.
        """
        dev_agent = self._dev_agent()
        router = getattr(dev_agent, "_router", None) if dev_agent else None
        if router is None:
            return "Sorry, the conversation model isn't available right now."
        try:
            context = self._conv_mode().build_context()
            result = await router.infer(
                domain="general", user_text=text, context=context,
            )
            reply = (getattr(result, "text", "") or "").strip()
            if getattr(result, "ok", True) and reply:
                return reply
            log.debug("conversation: empty/failed general reply (%r)",
                      getattr(result, "error", None))
        except Exception as exc:
            log.warning("conversation: general inference failed (%s)", exc)
        return "Sorry, I didn't catch that. Could you say it another way?"

    # ---------------------------------------------------------------------- #
    # Macro save/replay (self-skilling rung 2)
    # ---------------------------------------------------------------------- #

    async def note_pending_macro(self, summaries: list) -> None:
        """Detector callback: a macro was detected — announce it and arm the
        "save that as ..." approval. Announcement only; nothing is enabled here
        (spec R4 fail-safe-DENY — only an explicit save promotes)."""
        if not summaries:
            return
        latest = summaries[-1]
        self._pending_macro = {"id": latest["id"], "name": latest.get("name", "")}
        await self._tts_speak(
            "I noticed you often repeat a series of steps. To save it as a "
            "command, say: save that as a command called, then a name."
        )

    async def maybe_handle_macro(self, cmd) -> Optional[dict]:
        """Route a voice utterance to the macro subsystem, else None.

        A "save that as ..." approval always wins (it's the human-approval edge,
        spec R4). A macro replay never shadows a built-in system-control phrase.
        """
        from core.macro_store import parse_macro_save
        # Deferred import: avoids a circular import with core.hybrid_coordinator,
        # which defines this predicate and imports WorkflowHandler at module level.
        from core.voice_system_control import _is_system_control_voice

        name = parse_macro_save(cmd.text)
        if name is not None:
            return await self.handle_macro_save(name)
        if _is_system_control_voice(cmd):
            return None
        macro = self._macro_store().match(cmd.text)
        if macro is not None:
            return await self.handle_macro_replay(macro, cmd)
        return None

    async def handle_macro_save(self, name: str) -> dict:
        """Promote the pending detected macro under the user-chosen name.

        Fail-safe: with nothing pending (no detection, or already saved/ignored),
        this promotes nothing (spec R4.1/R4.2)."""
        pending = self._pending_macro
        if not pending:
            await self._tts_speak("I don't have a suggested command to save right now.")
            return {"status": "ok", "action": "MACRO_SAVE_NONE"}
        row = None
        agent_db = self._agent_db()
        if agent_db and getattr(agent_db, "available", False):
            row = await agent_db.skills.get_evolution_candidate(pending["id"])
        if not row:
            self._pending_macro = None
            await self._tts_speak("Sorry, I couldn't find that suggestion anymore.")
            return {"status": "ok", "action": "MACRO_SAVE_MISSING"}
        macro = self._macro_store().register(row, name=name, keywords=[name.lower()])
        if macro is None:
            await self._tts_speak("Sorry, I couldn't save that one.")
            return {"status": "ok", "action": "MACRO_SAVE_FAILED"}
        import json as _json
        try:
            refs = _json.loads(row.get("source_refs") or "{}")
        except (ValueError, TypeError):
            refs = {}
        refs["name"] = name
        refs["keywords"] = [name.lower()]
        await agent_db.skills.promote_macro_candidate(row["id"], name, _json.dumps(refs))
        self._pending_macro = None
        await self._tts_speak(f"Saved. Say {name} to run it.")
        return {"status": "ok", "action": "MACRO_SAVE", "name": name}

    async def handle_macro_replay(self, macro, cmd) -> dict:
        """Replay a matched macro through the executor (every gate still fires)."""
        result = await self._macro_store().replay(macro, self._executor())
        status = (result or {}).get("status")
        if status == "clarify":
            await self._tts_speak(
                "I couldn't run that command. " + result.get("reason", ""))
        elif status == "error":
            await self._tts_speak(
                f"The command stopped at step {result.get('step', 0) + 1}.")
        return {"status": status or "error", "action": "MACRO_REPLAY",
                "macro": macro.name, "result": result}
