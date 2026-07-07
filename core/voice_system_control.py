"""VoiceSystemControl — keyword-matched voice system-control commands
extracted from HybridCoordinator._route_impl.

Owns the "System control commands — intercept before gate evaluation" block:
lecture mode, lecture-notes search, manual pain-day toggle, condition
switching, calibration triggers, goal-level agent control (status/cancel/
resume/authorize), proactive scheduling, agent history, the escalation
review queue, mic mute, capability discovery ("help"), personal-KB indexing,
and the Google PIM connect flow.

Per the spec's explicit-DI requirement (specs/hybrid-coordinator-decomposition
R3), this module holds NO reference to the full HybridCoordinator instance —
every dependency it needs is an individually named, zero-arg accessor
callable (or a narrow async delegate for the two behaviors — condition
switching and calibration — that must run on the real coordinator because an
external profiler API takes `coordinator=` as a kwarg).
"""
from __future__ import annotations

import asyncio
import logging
import time as _time
from typing import Awaitable, Callable, Coroutine, Optional, TYPE_CHECKING
from typing import Any

if TYPE_CHECKING:
    from sensors.whisper_stream import WhisperStream
    from storage.db import AgentDB
    from adaptive.behavioral_twin_state import BehavioralTwinState
    from adaptive.condition_profiler import ConditionProfiler
    from adaptive.condition_calibration import ConditionCalibration
    from inference.dev_agent import DevAgent
    from storage.personal_kb import PersonalKB
    from skills.registry import SkillRegistry
    from storage.audit_log import AuditLog
    from core.event_dispatcher import Command

from core.schedule_parser import is_schedule_phrase, parse as parse_schedule

log = logging.getLogger(__name__)

# Voice phrases handled by the system-control block in route() (pain day,
# lecture mode, condition switching, calibration). The dev-agent pre-gate must
# NOT intercept these: the DomainClassifier maps some (e.g. "pain day on" →
# general) to dev domains, which would shadow the keyword handler and send the
# phrase to an LLM instead. KEEP IN SYNC with the keyword block in route().
_SYSTEM_CONTROL_PHRASES: frozenset[str] = frozenset({
    "start lecture mode", "lecture mode on", "begin lecture mode",
    "stop lecture mode", "lecture mode off", "end lecture mode",
    "pain day on", "flare day on", "bad day",
    "pain day off", "flare day off", "feeling better",
    "this is a good day", "good day mode", "feeling well",
    "this is a flare day", "flare day", "flare mode",
    "this is an allergy day", "allergy day", "allergy mode",
    "run voice calibration", "calibrate my voice", "quick calibration",
    "calibrate flare day", "calibrate allergy day",
    # Goal-level agent control
    "hey agent status", "what are you doing", "agent status",
    "status", "what's happening",
    "hey agent stop", "cancel task", "cancel agent", "stop agent",
    "stop the agent", "cancel the task",
    # Crash recovery — advertised by the post-crash TTS notice in main.py;
    # the resume itself is voice-confirm-gated inside resume_pending_plan()
    "resume task", "resume the task", "hey agent resume",
    "resume work", "resume interrupted task",
    "hey agent history", "what did you do", "agent history",
    "show history", "recent actions",
    "review queue", "show review queue", "what needs review",
    "hey agent review queue", "show escalations", "pending reviews",
    "clear review queue", "dismiss reviews", "clear escalations",
    # Mic mute — voice can only mute; unmute requires the iPad button
    # (mic is deaf once muted, so a voice unmute command can never arrive)
    "mute mic", "mute microphone", "mic off", "silence mic",
    # Capability discovery + personal KB maintenance
    "help", "what can you do", "what can i say", "list your skills",
    "index my notes", "reindex my notes", "index my documents",
    # Google PIM auth lifecycle (one-time setup + expired-token recovery)
    "connect google", "reconnect google", "connect gmail", "set up gmail",
    "set up google",
})

def _is_system_control_voice(cmd: 'Command') -> bool:
    """True if `cmd` is a voice system-control keyword that route()'s keyword
    block handles — so the dev-agent pre-gate leaves it alone."""
    if cmd.source not in ("voice", "voice_local"):
        return False
    norm = cmd.text.lower().strip(" \t\n.,!?;:\"'")
    if norm in _SYSTEM_CONTROL_PHRASES:
        return True
    if "lecture notes" in norm and "search" in norm:
        return True
    if norm.startswith("hey agent authorize ") or norm.startswith("authorize "):
        return True
    if is_schedule_phrase(norm):
        return True
    return False


# Hoisted to module level to avoid rebuilding on every voice command (#5).
_CONDITION_TRIGGERS: dict[str, str] = {
    "this is a good day":      "good_day",
    "good day mode":           "good_day",
    "feeling well":            "good_day",
    "this is a flare day":     "flare_day",
    "flare day":               "flare_day",
    "flare mode":              "flare_day",
    "this is an allergy day":  "allergy_day",
    "allergy day":             "allergy_day",
    "allergy mode":            "allergy_day",
}

_CALIBRATION_TRIGGERS: dict[str, tuple[str, bool]] = {
    "run voice calibration":   ("good_day",    False),
    "calibrate my voice":      ("good_day",    False),
    "quick calibration":       ("good_day",    True),
    "calibrate flare day":     ("flare_day",   False),
    "calibrate allergy day":   ("allergy_day", False),
}


class VoiceSystemControl:
    def __init__(
        self,
        *,
        whisper: Callable[[], Optional['WhisperStream']],
        agent_db: Callable[[], Optional['AgentDB']],
        twin: Callable[[], Optional['BehavioralTwinState']],
        profiler: Callable[[], Optional['ConditionProfiler']],
        calibrator: Callable[[], Optional['ConditionCalibration']],
        dev_agent: Callable[[], Optional['DevAgent']],
        personal_kb: Callable[[], Optional['PersonalKB']],
        skill_registry: Callable[[], Optional['SkillRegistry']],
        audit: Callable[[], Optional['AuditLog']],
        tts_speak: Callable[[str], Coroutine[Any, Any, None]],
        approval_config: Callable[[], dict],
        switch_condition: Callable[[str], Coroutine[Any, Any, None]],
        run_calibration: Callable[[str, bool], Coroutine[Any, Any, None]],
    ) -> None:
        self._whisper = whisper
        self._agent_db = agent_db
        self._twin = twin
        self._profiler = profiler
        self._calibrator = calibrator
        self._dev_agent = dev_agent
        self._personal_kb = personal_kb
        self._skill_registry = skill_registry
        self._audit = audit
        self._tts_speak = tts_speak
        self._approval_config = approval_config
        self._switch_condition = switch_condition
        self._run_calibration = run_calibration
        self._lecture_mode: bool = False

    @staticmethod
    def _on_task_done(task: "asyncio.Task[Any]", label: str) -> None:
        """Log any exception from a fire-and-forget task so failures are visible."""
        if not task.cancelled() and task.exception():
            log.error("%s raised: %s", label, task.exception())

    # ---------------------------------------------------------------------- #
    # Entry point
    # ---------------------------------------------------------------------- #

    async def maybe_handle(self, cmd) -> Optional[dict]:
        """Match a voice utterance against system-control keywords.

        Returns a handled result dict, or ``None`` to fall through to
        ordinary gate evaluation (not a voice source, or no keyword matched).
        """
        if cmd.source not in ("voice", "voice_local"):
            return None

        # Strip surrounding whitespace AND punctuation: Whisper routinely
        # appends a period ("pain day on." / "lecture mode on?") which would
        # otherwise miss every exact-match keyword below.
        _lower = cmd.text.lower().strip(" \t\n.,!?;:\"'")

        # Lecture mode
        if _lower in ("start lecture mode", "lecture mode on", "begin lecture mode"):
            self._lecture_mode = True
            whisper = self._whisper()
            if whisper:
                whisper.set_lecture_mode(True)
            log.info("Lecture mode ON")
            return {"status": "ok", "action": "LECTURE_MODE", "enabled": True}
        elif _lower in ("stop lecture mode", "lecture mode off", "end lecture mode"):
            self._lecture_mode = False
            whisper = self._whisper()
            if whisper:
                whisper.set_lecture_mode(False)
            log.info("Lecture mode OFF")
            return {"status": "ok", "action": "LECTURE_MODE", "enabled": False}

        # Lecture notes search — "search my lecture notes for X"
        elif "lecture notes" in _lower and (
            _lower.startswith("search") or "search" in _lower
        ):
            # Extract query after "for" or "about"
            for sep in ("for ", "about ", "on "):
                if sep in _lower:
                    search_q = cmd.text[_lower.index(sep) + len(sep):].strip()
                    break
            else:
                search_q = cmd.text  # fallback: search whole phrase
            agent_db = self._agent_db()
            if agent_db and agent_db.available and search_q:
                rows = await agent_db.memory.search_lecture_notes(search_q, limit=10)
                if rows:
                    summary = "\n".join(f"- {r['text']}" for r in rows[:5])
                    log.info("Lecture notes search %r: %d results", search_q, len(rows))
                    return {"status": "ok", "action": "LECTURE_SEARCH",
                            "query": search_q, "results": len(rows),
                            "preview": summary}
                else:
                    return {"status": "ok", "action": "LECTURE_SEARCH",
                            "query": search_q, "results": 0,
                            "preview": "No lecture notes found for that query."}

        # Manual pain day toggle
        elif _lower in ("pain day on", "flare day on", "bad day"):
            twin = self._twin()
            if twin:
                twin.set_manual_pain_day(True)
            # Immediately relax the recognizer (VAD + logprob floor) for the
            # next utterance, before route() reconciles on the next command.
            whisper = self._whisper()
            if whisper is not None:
                whisper.apply_pain_day(True)
            return {"status": "ok", "action": "PAIN_DAY", "enabled": True}
        elif _lower in ("pain day off", "flare day off", "feeling better"):
            twin = self._twin()
            if twin:
                twin.set_manual_pain_day(False)
            whisper = self._whisper()
            if whisper is not None:
                whisper.apply_pain_day(False)
            return {"status": "ok", "action": "PAIN_DAY", "enabled": False}

        # Condition switching — loads calibrated voice profile for condition
        # (dict hoisted to module level, #5)
        if _lower in _CONDITION_TRIGGERS and self._profiler():
            condition = _CONDITION_TRIGGERS[_lower]
            t = asyncio.create_task(self._switch_condition(condition))
            t.add_done_callback(lambda t: self._on_task_done(t, "_switch_condition"))
            return {"status": "ok", "action": "CONDITION_SWITCH",
                    "condition": condition}

        # Calibration triggers (dict hoisted to module level, #5)
        if _lower in _CALIBRATION_TRIGGERS and self._calibrator():
            condition, quick = _CALIBRATION_TRIGGERS[_lower]
            t = asyncio.create_task(self._run_calibration(condition, quick))
            t.add_done_callback(lambda t: self._on_task_done(t, "_run_calibration"))
            return {"status": "ok", "action": "CALIBRATION_START",
                    "condition": condition, "quick": quick}

        # ── Goal-level agent control ──────────────────────────────────────

        # "hey agent status" / "what are you doing"
        if _lower in ("hey agent status", "what are you doing", "agent status",
                      "status", "what's happening"):
            dev_agent = self._dev_agent()
            if dev_agent is not None:
                ps = dev_agent.get_plan_status()
                if ps.get("active"):
                    msg = (f"Running step {ps['step']} of {ps['total_steps']}: "
                           f"{ps.get('goal', '')[:50]}")
                else:
                    msg = "No active task."
                from core.async_utils import fire_and_log
                fire_and_log(self._tts_speak(msg), log, label="tts agent status")  # fix #15
                return {"status": "ok", "action": "AGENT_STATUS", "plan": ps}

        # "hey agent stop" / "cancel task" / "cancel agent"
        elif _lower in ("hey agent stop", "cancel task", "cancel agent", "stop agent",
                        "stop the agent", "cancel the task"):
            dev_agent = self._dev_agent()
            if dev_agent is not None:
                dev_agent.request_cancel()
                from core.async_utils import fire_and_log
                fire_and_log(self._tts_speak("Cancelling after current step."), log, label="tts agent cancel")  # fix #15
                return {"status": "ok", "action": "AGENT_CANCEL"}
            return {"status": "ok", "action": "AGENT_CANCEL", "note": "no active agent"}

        # "resume task" — offer to resume the most recent interrupted plan
        # (post-crash recovery; advertised by the crash-notice TTS in
        # main.py). Safe to fire-and-forget: resume_pending_plan() is
        # itself gated on an explicit spoken confirmation, so this phrase
        # alone can never re-run a plan with destructive steps.
        elif _lower in ("resume task", "resume the task", "hey agent resume",
                        "resume work", "resume interrupted task"):
            pending: list = []
            agent_db = self._agent_db()
            if agent_db and agent_db.available:
                pending = await agent_db.runs.get_interrupted_runs(limit=1)
            dev_agent = self._dev_agent()
            if dev_agent is None or not pending:
                from core.async_utils import fire_and_log
                fire_and_log(self._tts_speak("No interrupted task to resume."), log, label="tts resume none")  # fix #15
                return {"status": "ok", "action": "AGENT_RESUME", "offered": False}
            t = asyncio.create_task(dev_agent.resume_pending_plan())  # type: ignore[arg-type]
            t.add_done_callback(lambda t: self._on_task_done(t, "resume_pending_plan"))
            return {"status": "ok", "action": "AGENT_RESUME", "offered": True,
                    "goal": pending[0].get("goal", "")[:80]}

        # "undo that run" — Revert the most recent dev agent run (VoiceRewindHandler)
        elif _lower in ("undo that run", "undo run", "revert run", "undo last task", "revert last task", 
                        "undo the run", "undo the last run", "undo task"):
            dev_agent = self._dev_agent()
            if dev_agent is None:
                from core.async_utils import fire_and_log
                fire_and_log(self._tts_speak("No agent available to undo."), log, label="tts undo none")
                return {"status": "ok", "action": "REVERT_RUN", "offered": False}
                
            t = asyncio.create_task(dev_agent.revert_last_run())
            t.add_done_callback(lambda t: self._on_task_done(t, "revert_last_run"))
            return {"status": "ok", "action": "REVERT_RUN", "offered": True}

        # "hey agent authorize <goal>" — create a standalone goal session
        elif _lower.startswith("hey agent authorize ") or _lower.startswith("authorize "):
            goal_text = (cmd.text.split("authorize ", 1)[-1]).strip(" .,!?\"'")
            if goal_text:
                from core.goal_session import GoalSessionStore
                duration = self._approval_config().get("goal_session_duration_s", 900)
                max_act = self._approval_config().get("goal_session_max_actions", 50)
                GoalSessionStore.create(goal=goal_text, domain="plan",
                                        duration_s=duration, max_actions=max_act)
                # Durable goal backlog (gap D): persist the goal so it survives a
                # crash/restart, then kick the drainer to run it. idempotency_key
                # uses millisecond precision so two quick re-issues within the
                # same second get distinct keys (#7).
                agent_db = self._agent_db()
                if agent_db and agent_db.available:
                    key = f"authorize:{goal_text[:80]}:{_time.time():.3f}"  # fix #7
                    await agent_db.goals.enqueue_goal(
                        goal_text, domain="plan", idempotency_key=key,
                    )
                    dev_agent = self._dev_agent()
                    if dev_agent is not None:
                        asyncio.create_task(dev_agent.drain_goal_queue())
                mins = int(duration / 60)
                from core.async_utils import fire_and_log
                fire_and_log(  # fix #15
                    self._tts_speak(f"Goal authorized for {mins} minutes: {goal_text[:40]}"),
                    log, label="tts goal authorize",
                )
                return {"status": "ok", "action": "GOAL_AUTHORIZE", "goal": goal_text}

        # ── Proactive scheduling / reminders / event rules (N+2) ──────────
        elif is_schedule_phrase(_lower):
            return await self._handle_schedule_command(
                parse_schedule(cmd.text, _time.time()))

        # "hey agent history" / "what did you do"
        elif _lower in ("hey agent history", "what did you do", "agent history",
                        "show history", "recent actions"):
            summary = await self._audit_history_summary(n=5)
            from core.async_utils import fire_and_log
            fire_and_log(self._tts_speak(summary), log, label="tts agent history")  # fix #15
            return {"status": "ok", "action": "AGENT_HISTORY", "summary": summary}

        # "review queue" / "what needs review" — dev plans that exhausted
        # their replan/step budget, were rolled back, and now need a human
        # decision (R-10 escalation queue)
        elif _lower in ("review queue", "show review queue", "what needs review",
                        "hey agent review queue", "show escalations",
                        "pending reviews"):
            items: list = []
            total = 0
            agent_db = self._agent_db()
            if agent_db and agent_db.available:
                total = await agent_db.sagas.count_pending_escalations()
                items = await agent_db.sagas.get_pending_escalations(limit=5)
            if total:
                newest = items[0]
                msg = (f"{total} plan{'s' if total != 1 else ''} need review. "
                       f"Most recent: {newest['goal'][:50]}, "
                       f"{newest['reason'].replace('_', ' ')}.")
            else:
                msg = "Review queue is empty."
            from core.async_utils import fire_and_log
            fire_and_log(self._tts_speak(msg), log, label="tts review queue")  # fix #15
            return {"status": "ok", "action": "AGENT_ESCALATIONS",
                    "count": total, "items": items}

        # "clear review queue" — acknowledge every pending escalation
        elif _lower in ("clear review queue", "dismiss reviews",
                        "clear escalations"):
            cleared = 0
            agent_db = self._agent_db()
            if agent_db and agent_db.available:
                cleared = await agent_db.sagas.resolve_escalations(
                    status="acknowledged")
            from core.async_utils import fire_and_log
            fire_and_log(  # fix #15
                self._tts_speak(f"Cleared {cleared} review item{'s' if cleared != 1 else ''}."),
                log, label="tts clear queue",
            )
            return {"status": "ok", "action": "AGENT_ESCALATIONS_CLEAR",
                    "count": cleared}

        # Mic mute — voice one-way; unmute via iPad mic_mute message
        elif _lower in ("mute mic", "mute microphone", "mic off", "silence mic"):
            whisper = self._whisper()
            if whisper is not None:
                whisper.set_muted(True)
            from core.async_utils import fire_and_log
            fire_and_log(self._tts_speak("Microphone muted. Tap the iPad to unmute."), log, label="tts mic mute")  # fix #15
            return {"status": "ok", "action": "MIC_MUTE", "muted": True}

        # Capability discovery — "help" / "what can you do" (GAP-4)
        elif _lower in ("help", "what can you do", "what can i say",
                        "list your skills"):
            summary = self._capability_summary()
            from core.async_utils import fire_and_log
            fire_and_log(self._tts_speak(summary), log, label="tts help")  # fix #15
            return {"status": "ok", "action": "HELP", "summary": summary}

        # Personal KB maintenance — "index my notes"
        elif _lower in ("index my notes", "reindex my notes",
                        "index my documents"):
            personal_kb = self._personal_kb()
            from core.async_utils import fire_and_log
            if personal_kb is not None and getattr(personal_kb, "available", False):
                if personal_kb.get_status().get("paused"):
                    fire_and_log(  # fix #15
                        self._tts_speak(
                            "Indexing is paused during the flare — I'll be able "
                            "to index after it passes."),
                        log, label="tts kb paused",
                    )
                    return {"status": "ok", "action": "PERSONAL_KB_PAUSED"}
                fire_and_log(personal_kb.index(), log,
                             label="voice personal_kb index")
                fire_and_log(  # fix #15
                    self._tts_speak("Okay, indexing your documents in the background."),
                    log, label="tts kb index",
                )
                return {"status": "ok", "action": "PERSONAL_KB_INDEX"}
            fire_and_log(self._tts_speak("The personal knowledge base isn't available."), log, label="tts kb unavail")  # fix #15
            return {"status": "ok", "action": "PERSONAL_KB_UNAVAILABLE"}

        # Google PIM auth — "connect google" / "reconnect google".
        # One spoken phrase + one browser consent click replaces the old
        # env-var + script + manifest-edit setup; also the recovery path
        # the expired-token messages name.
        elif _lower in ("connect google", "reconnect google",
                        "connect gmail", "set up gmail", "set up google"):
            return await self._handle_google_connect()

        return None

    # ---------------------------------------------------------------------- #
    # Helpers
    # ---------------------------------------------------------------------- #

    def _capability_summary(self) -> str:
        """A concise spoken summary of what the user can ask for (GAP-4).

        Static core abilities plus the DYNAMIC skill intents from the registry
        and proactivity/personal-KB availability — so the summary never goes
        stale as skills are added by manifest.
        """
        parts = [
            "I can control the desktop: click, scroll, type, open and close apps, "
            "take screenshots, and dictate.",
            "I can write code, run plans, and answer technical questions.",
            "You can set reminders, like: every morning at 8, brief me. "
            "Say 'what are my reminders' to review them.",
        ]
        personal_kb = self._personal_kb()
        if personal_kb is not None and getattr(personal_kb, "available", False):
            parts.append("I can search your own documents — ask things like: "
                         "what did I write in my notes about a topic.")
        skill_registry = self._skill_registry()
        if skill_registry is not None and skill_registry.has_skills():
            kws: list[str] = []
            for action in skill_registry.list_actions()[:6]:
                if action.get("keywords"):
                    kws.append(action["keywords"][0])
            if kws:
                parts.append("Connected skills also let you say: " + "; ".join(kws) + ".")
        parts.append("Say 'pain day on' when you're flaring and I'll adapt.")
        return " ".join(parts)

    async def _handle_google_connect(self) -> dict:
        """Start the Google OAuth consent flow by voice (setup or recovery).

        Speaks any setup blocker (libraries / client secret) with the exact fix;
        otherwise launches the browser consent flow in the background and, on
        success, hot-starts the google_pim skill — no agent restart.
        """
        from skills import google_setup
        blocker = google_setup.setup_blocker()
        if blocker:
            asyncio.create_task(self._tts_speak(blocker))
            return {"status": "ok", "action": "GOOGLE_CONNECT_BLOCKED",
                    "reason": blocker}
        from core.async_utils import fire_and_log
        asyncio.create_task(self._tts_speak(
            "Opening Google sign-in in your browser — approve access there, "
            "and I'll tell you when it's done."))
        fire_and_log(self._google_connect_flow(), log, label="google connect flow")
        return {"status": "ok", "action": "GOOGLE_CONNECT_STARTED"}

    async def _google_connect_flow(self) -> None:
        """Background half of the connect flow: await consent, hot-start, report."""
        from skills.google_setup import run_auth_flow
        ok, message = await run_auth_flow()
        skill_registry = self._skill_registry()
        if ok and skill_registry is not None:
            try:
                started = await skill_registry.start_skill("google_pim")
                if not started:
                    message += " The skill will start on the next agent launch."
            except Exception as exc:
                log.warning("google_pim hot-start failed: %s", exc)
                message += " The skill will start on the next agent launch."
        await self._tts_speak(message)

    async def _handle_schedule_command(self, spec: Optional[dict]) -> dict:
        """Act on a parsed voice schedule / reminder / event-rule / management
        command (N+2). Writes to AgentDB; the ProactiveScheduler and
        EventRuleEngine pick the new rows up on their next tick/event."""
        if spec is None:
            asyncio.create_task(self._tts_speak(
                "Sorry, I didn't catch that. Try 'every morning at 8 brief me'."))
            return {"status": "ok", "action": "SCHEDULE_UNPARSED"}
        db = self._agent_db()
        if not (db and db.available):
            return {"status": "ok", "action": "SCHEDULE_NOOP"}
        kind = spec.get("kind")

        if kind == "schedule":
            key = f"sched:{spec['goal'][:60]}:{spec['execute_at']:.0f}"
            await db.goals.enqueue_scheduled_goal(
                spec["goal"], execute_at=spec["execute_at"],
                recurrence=spec.get("recurrence"), source_trigger="schedule",
                idempotency_key=key)
            asyncio.create_task(self._tts_speak(spec.get("spoken", "Reminder set.")))
            return {"status": "ok", "action": "SCHEDULE_SET", "goal": spec["goal"]}

        if kind == "event_rule":
            await db.events.insert_event_rule(
                topic_pattern=spec["topic_pattern"], goal_template=spec["goal_template"],
                name=spec.get("name"), predicate=spec.get("predicate"),
                action_kind=spec.get("action_kind", "notify"))
            asyncio.create_task(self._tts_speak(spec.get("spoken", "Rule set.")))
            return {"status": "ok", "action": "EVENT_RULE_SET"}

        if kind == "list":
            scheds = await db.goals.list_schedules()
            rules = await db.events.list_event_rules()
            total = len(scheds) + len(rules)
            if not total:
                msg = "You have no reminders set."
            else:
                parts = [s["goal"][:40] for s in scheds[:5]]
                parts += [(r.get("name") or r["topic_pattern"]) for r in rules[:5]]
                msg = f"You have {total} reminder{'s' if total != 1 else ''}: " + "; ".join(parts) + "."
            asyncio.create_task(self._tts_speak(msg))
            return {"status": "ok", "action": "SCHEDULE_LIST", "count": total}

        if kind == "cancel":
            q = (spec.get("query") or "").strip().lower()
            cancelled = 0
            for s in await db.goals.list_schedules():
                if not q or q in s["goal"].lower():
                    if await db.goals.cancel_schedule(s["id"]):
                        cancelled += 1
            asyncio.create_task(self._tts_speak(
                f"Cancelled {cancelled} reminder{'s' if cancelled != 1 else ''}."))
            return {"status": "ok", "action": "SCHEDULE_CANCEL", "count": cancelled}

        return {"status": "ok", "action": "SCHEDULE_NOOP"}

    async def _audit_history_summary(self, n: int = 5) -> str:
        """Query audit.db for the last `n` mcp_call events and return a spoken summary."""
        audit = self._audit()
        if audit is None:
            return "Audit log not available."
        try:
            rows = await audit.get_recent_mcp_calls(n=n)
            if not rows:
                return "No recent actions recorded."
            parts = []
            for r in rows:
                tool = r.get("tool", "unknown")
                params = r.get("params") or ""
                # Extract the most readable param for speech
                try:
                    import json as _json
                    p = _json.loads(params) if isinstance(params, str) else params
                    detail = (
                        p.get("command", "")[:30] or
                        p.get("text", "")[:30] or
                        p.get("title_substring", "")[:30] or
                        p.get("key", "") or ""
                    )
                except Exception:
                    detail = ""
                parts.append(f"{tool}{' ' + detail if detail else ''}")
            return "Last " + str(len(parts)) + " actions: " + ", ".join(parts) + "."
        except Exception as exc:
            log.debug("VoiceSystemControl._audit_history_summary failed: %s", exc)
            return "Could not retrieve history."
