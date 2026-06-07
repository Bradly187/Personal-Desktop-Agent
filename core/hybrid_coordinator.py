"""HybridCoordinator — 4-gate routing between local LLM and Anthropic cloud.

Receives a Command from FusionEngine, decides whether to run local inference
or fall back to the cloud, executes the resulting action, and logs the outcome.

Gate logic (source-dependent):
  touch / multimodal                → bypass all 4 gates → local
  voice_local                       → skip Gate 1 → gates 2-4
  gesture / voice                   → full 4-gate evaluation

Gate 0 — Privacy:     command text contains no sensitive-data patterns
  fail → force local (never send to cloud)
  NOTE: bypass sources (touch / multimodal) are already local-only and never
  reach the cloud, so Gate 0 does not apply to them — a resolved touch action
  executes directly without LLM interference even when its text matches a
  sensitive pattern.

Gate 1 — Confidence:  whisper_logprob ≥ min AND gesture_conf ≥ min
  fail-voice, KNOWN misrecognition (vocab pass already fixed it) → local
  fail-voice, UNKNOWN low-confidence transcript                  → cloud
    (the cloud system prompt is tuned to repair voice misrecognitions the
     local dictionary cannot)
  fail-gesture  → discard silently

Gate 2 — Complexity:  token_count ≤ max AND no complexity keywords
  fail → Anthropic API

Gate 3 — VRAM:  vram_free_gb ≥ vram_free_min_gb  (via pynvml)
  fail → Anthropic API

Gate 4 — Latency EMA:  latency_ema_ms ≤ latency_budget_ms
  fail → Anthropic API

After inference: log outcome to agent.db (AgentDB), call CommandExecutor.execute().
Each log entry includes `gate_that_decided`: the decisive routing factor, one of
_GATE_DECISION_LABELS ("bypass", "gate0_privacy", "gate1_voice_conf",
"gate2_complexity", "gate3_vram", "gate4_latency", "all_pass"). Silently
discarded gestures (Gate 1, low confidence) return early WITHOUT a DB row, so
"discard" is intentionally not a logged label.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from core.command_executor import Command, CommandExecutor
from dataclasses import replace as _dc_replace
from inference.local_inference import LocalInference, OllamaInference, _build_prompt, _SYSTEM_PROMPT
from desktop.vision_grounder import VisionGrounder
from monitoring.trace import get_tracer

if TYPE_CHECKING:
    from storage.audit_log import AuditLog
    from adaptive.behavioral_twin_state import BehavioralTwinState
    from adaptive.content_filter import ContentFilter
    from adaptive.continuous_trainer import ContinuousTrainer
    from storage.db import AgentDB
    from inference.dev_agent import DevAgent

log = logging.getLogger(__name__)

# Cloud system prompt — mirrors local _SYSTEM_PROMPT but adds misrecognition
# guidance specific to voice+accessibility input.  Kept separate so we can
# tune cloud behaviour independently of the local LLM prompt.
_CLOUD_SYSTEM_PROMPT = (
    _SYSTEM_PROMPT
    + "\n\nAdditional guidance for cloud disambiguation:\n"
    "- Common voice misrecognitions to correct automatically:\n"
    '  "clothes"→CLOSE  "scroll done"→SCROLL down  "hot key"→HOTKEY\n'
    '  "oh pen"→OPEN  "clique"→CLICK  "tight"→TYPE\n'
    "- For multi-step commands (\"close then open\"): execute the FIRST action "
    "and emit CLARIFY for the remaining steps.\n"
    "- For gesture-source commands: POINT→CLICK FIST→CLOSE PALM→SCROLL up."
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_COMPLEXITY_KEYWORDS = (
    "and then", "after that", "for each", "followed by",
    "then open", "then click", "then type", "while", "repeat",
)


@dataclass
class CoordinatorConfig:
    # Gate 0 — Privacy (force local when sensitive patterns detected)
    gate0_enabled: bool = True
    gate0_sensitive_patterns: tuple[str, ...] = field(default_factory=lambda: (
        "password", "passwd", "secret", "api key", "api_key", "token",
        "credit card", "card number", "cvv", "ssn", "social security",
        "routing number", "account number", "private key", "ssh key",
    ))

    # Gate 1 thresholds
    whisper_logprob_min: float = -1.0       # log-prob (0 = perfect, -∞ = impossible)
    gesture_confidence_min: float = 0.6

    # Gate 2 thresholds
    max_local_tokens: int = 40              # rough word-count proxy

    # Gate 3 thresholds — 8.0 GB floor suits RTX 5090 (32 GB VRAM); lower for smaller GPUs
    vram_free_min_gb: float = 8.0

    # Gate 4 thresholds
    latency_budget_ms: float = 600.0
    latency_ema_alpha: float = 0.1          # smoothing factor for EMA

    # Local-inference circuit-breaker — a hung local call (Ollama wedged, GPU
    # stuck mid-flare, model reload stall) must not stall the accessibility
    # pipeline indefinitely. Warm p50 is ~373ms and an 8B cold load ~2.5s, so
    # 15s is ~6x the worst legitimate case while still catching a true hang.
    local_timeout_s: float = 15.0

    # (routing_log_path removed — outcomes written to agent.db commands table)

    # Anthropic API (cloud fallback). Haiku 4.5 — fast/cheap, 8/8 on voice
    # misrecognitions; the alias floats to the latest 4.5 snapshot. The command
    # path needs only a one-line verb, so Haiku is the right tier here (the dev
    # path uses Opus 4.8 via CloudDevAgent).
    anthropic_model: str = "claude-haiku-4-5"


# ---------------------------------------------------------------------------
# Cloud inference (Anthropic API)
# ---------------------------------------------------------------------------

class _CloudInference:
    """Anthropic SDK Claude backend. Lazy import."""

    def __init__(self, model: str) -> None:
        self._model = model
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                import anthropic
                self._client = anthropic.Anthropic()
            except ImportError:
                raise RuntimeError("anthropic not installed — run: pip install anthropic")
        return self._client

    async def infer(self, cmd: Command) -> str:
        try:
            client = self._get_client()
        except RuntimeError as exc:
            log.error("CloudInference unavailable: %s", exc)
            return f"CLARIFY cloud unavailable: {exc}"

        context_lines = ""
        if cmd.session_context:
            joined = "\n".join(f"- {c}" for c in cmd.session_context[-5:])
            context_lines = f"\n\nRecent commands:\n{joined}"

        user_content = f"Command: {cmd.text}{context_lines}"

        t0 = time.monotonic()
        try:
            response = await asyncio.to_thread(
                client.messages.create,
                model=self._model,
                max_tokens=64,
                temperature=0.0,
                system=_CLOUD_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_content}],
            )
            action = response.content[0].text.strip().splitlines()[0].strip()
            latency_ms = (time.monotonic() - t0) * 1000
            log.info("CloudInference: %r → %r (%.0f ms)", cmd.text, action, latency_ms)
            return action
        except Exception as exc:
            log.error("CloudInference failed: %s", exc)
            return f"CLARIFY cloud error: {exc}"


# ---------------------------------------------------------------------------
# Gate 1 — voice confidence fallback
# ---------------------------------------------------------------------------

# Phonetic vocabulary corrections for the most common voice misrecognitions.
# Keyed on lowercased word/phrase, mapped to the correct desktop-control word.
_VOICE_CORRECTIONS: dict[str, str] = {
    "clothes":     "close",
    "clothe":      "close",
    "scroll done": "scroll down",
    "clique":      "click",
    "tight":       "type",
    "oh pen":      "open",
    "oh pen up":   "open up",
    "hot key":     "hotkey",
    "screen shot": "screenshot",
    # App name phonetic corrections
    "key row":     "kiro",
    "key-row":     "kiro",
    "keyrow":      "kiro",
    "cairo":       "kiro",
    "slap":        "slack",
    "diskord":     "discord",
    "dis cord":    "discord",
}


def _apply_vocabulary_corrections(text: str) -> tuple[str, bool]:
    """Replace known voice misrecognitions with the intended desktop-control word.

    Returns (corrected_text, changed).  Checks multi-word phrases first so
    'scroll done' beats 'done' matching nothing.
    """
    lower = text.lower()
    # Longest-phrase-first so "scroll done" beats a single-word match
    for wrong, right in sorted(_VOICE_CORRECTIONS.items(), key=lambda kv: -len(kv[0])):
        if wrong in lower:
            corrected = lower.replace(wrong, right)
            # Restore original capitalisation pattern (title-case first word)
            parts = corrected.split()
            if parts:
                parts[0] = parts[0].capitalize()
            return " ".join(parts), True
    return text, False


async def _retranscribe(cmd: Command) -> Command:
    """Apply vocabulary correction to a low-confidence voice transcript before Gate 2."""
    corrected_text, changed = _apply_vocabulary_corrections(cmd.text)
    if changed:
        log.info("Gate 1 vocab correction: %r → %r", cmd.text, corrected_text)
        cmd = _dc_replace(cmd, text=corrected_text, whisper_logprob=0.0)
    return cmd




# ---------------------------------------------------------------------------
# HybridCoordinator
# ---------------------------------------------------------------------------

_BYPASS_SOURCES = {"touch", "multimodal"}
_SKIP_GATE1_SOURCES = {"voice_local"}

# Output schema for the command-path LLM. The local/cloud command models are
# prompted to answer verb-first with exactly one of these 11 accessibility
# verbs (dev verbs are handled by DevAgent before the gate path; SNAP_WINDOW is
# gesture-sourced and never an LLM output). Any response whose first token is
# not one of these is a malformed LLM output and is degraded to CLARIFY in
# _execute_action() rather than dispatched as a bad/unknown verb.
_VALID_COMMAND_VERBS: frozenset[str] = frozenset({
    "CLICK", "MOUSEDOWN", "MOUSEUP", "SCROLL", "TYPE", "OPEN",
    "CLOSE", "HOTKEY", "DICTATE", "CLARIFY", "SCREENSHOT",
})

# Exhaustive set of `gate_that_decided` values written to agent.db by route().
# Each names the decisive routing factor for one logged command. Kept as a
# single source of truth so analytics/tests can assert against it. NOTE:
# silently discarded gestures (Gate 1, low confidence) return early WITHOUT a DB
# row, so "discard" is deliberately absent.
_GATE_DECISION_LABELS: frozenset[str] = frozenset({
    "bypass",            # touch / multimodal — local, gates skipped
    "gate0_privacy",     # sensitive text — forced local
    "gate1_voice_conf",  # unknown low-confidence voice — escalated to cloud
    "gate2_complexity",  # complex command — cloud
    "gate3_vram",        # insufficient free VRAM — cloud
    "gate4_latency",     # local latency over budget — cloud
    "all_pass",          # all gates passed — local
})


def _apply_pain_day_adjustments(cfg, snapshot) -> "CoordinatorConfig":
    """Return a modified config copy with pain-day threshold relaxations.
    Does not mutate the original config.
    """
    from dataclasses import replace
    return replace(
        cfg,
        whisper_logprob_min=cfg.whisper_logprob_min - 0.15,
        gesture_confidence_min=cfg.gesture_confidence_min - 0.10,
    )


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
    "having an svt", "svt attack", "svt mode", "this is an svt day",
    "run voice calibration", "calibrate my voice", "quick calibration",
    "calibrate flare day", "calibrate allergy day",
    "calibrate svt", "calibrate svt attack",
    # Goal-level agent control
    "hey agent status", "what are you doing", "agent status",
    "status", "what's happening",
    "hey agent stop", "cancel task", "cancel agent", "stop agent",
    "stop the agent", "cancel the task",
    "hey agent history", "what did you do", "agent history",
    "show history", "recent actions",
    # Mic mute — voice can only mute; unmute requires the iPad button
    # (mic is deaf once muted, so a voice unmute command can never arrive)
    "mute mic", "mute microphone", "mic off", "silence mic",
})


def _is_system_control_voice(cmd) -> bool:
    """True if `cmd` is a voice system-control keyword that route()'s keyword
    block handles — so the dev-agent pre-gate leaves it alone."""
    if cmd.source not in ("voice", "voice_local"):
        return False
    norm = cmd.text.lower().strip(" \t\n.,!?;:\"'")
    if norm in _SYSTEM_CONTROL_PHRASES:
        return True
    # lecture-notes search is a startswith/contains pattern, not exact-match
    if "lecture notes" in norm and "search" in norm:
        return True
    # "hey agent authorize <goal>" — prefix match
    if norm.startswith("hey agent authorize ") or norm.startswith("authorize "):
        return True
    return False


class HybridCoordinator:
    # Class-level DomainClassifier — stateless keyword scorer, no need to
    # re-instantiate per route() call.
    _domain_classifier = None

    @classmethod
    def _get_domain_classifier(cls):
        if cls._domain_classifier is None:
            from core.domain_classifier import DomainClassifier
            cls._domain_classifier = DomainClassifier()
        return cls._domain_classifier

    def __init__(
        self,
        local: LocalInference | None = None,
        config: CoordinatorConfig | None = None,
        trainer: Optional["ContinuousTrainer"] = None,
        dev_agent: Optional["DevAgent"] = None,
        agent_db: Optional["AgentDB"] = None,
        session_id: int = -1,
        content_filter: Optional["ContentFilter"] = None,
        audit_log: Optional["AuditLog"] = None,
        twin_state: Optional["BehavioralTwinState"] = None,
        whisper_stream=None,
    ) -> None:
        self._local = local or OllamaInference()
        self._cfg = config or CoordinatorConfig()
        self._cloud = _CloudInference(self._cfg.anthropic_model)
        self._executor = CommandExecutor()
        self._trainer = trainer
        self._dev_agent = dev_agent
        self._agent_db = agent_db
        self._session_id = session_id
        self._content_filter = content_filter
        self._audit = audit_log
        self._twin = twin_state
        self._latency_ema: Optional[float] = None
        self._grounder = VisionGrounder()
        self._whisper = whisper_stream
        self._fusion = None   # set via set_fusion_engine() after FusionEngine is created
        self._pending_clarification: Optional[str] = None
        self._lecture_mode: bool = False
        self._profiler = None    # set via set_profiler()
        self._calibrator = None  # set via set_calibrator()
        # D8: correction tracking
        self._last_executed_action: str = ""
        self._last_command_id: int = -1

        # Gate 3 VRAM cache — avoid calling pynvml on every command
        self._vram_cache: tuple[bool, float] | None = None  # (result, monotonic_time)
        self._vram_cache_ttl: float = 2.0  # seconds
        self._metrics = None   # set via set_metrics()

        self._memory = None   # MemoryManager — wired via set_memory()

        # Cloud DevAgent (--cloud-dev-agent) — optional Anthropic-API fallback for
        # dev-domain queries so a 30B specialist (and a GPU wake) is not needed.
        self._cloud_dev_agent = None          # CloudDevAgent or None
        self._cloud_always = False            # True with --no-local-specialists
        self._local_specialist_available = None  # () -> bool (specialist awake?)
        # Small rolling buffer of recent dev queries — passed to the cloud agent
        # as context without a DB round-trip in the hot path.
        self._recent_dev_commands: list[str] = []

    # ---------------------------------------------------------------------- #
    # Internal helpers
    # ---------------------------------------------------------------------- #

    @staticmethod
    def _on_task_done(task: "asyncio.Task[None]", label: str) -> None:
        """Log any exception from a fire-and-forget task so failures are visible."""
        if not task.cancelled() and task.exception():
            log.error("%s raised: %s", label, task.exception())

    # ---------------------------------------------------------------------- #
    # Public entry point
    # ---------------------------------------------------------------------- #

    def set_dev_agent(self, dev_agent: "DevAgent") -> None:
        self._dev_agent = dev_agent

    def set_cloud_dev_agent(
        self,
        cloud_agent,
        *,
        always_cloud: bool = False,
        local_available_fn=None,
    ) -> None:
        """Wire a CloudDevAgent for dev-domain routing.

        always_cloud=True  → every dev-domain query goes to the cloud (used with
                             --no-local-specialists; no GPU specialist is woken).
        always_cloud=False → cloud is a fallback: a dev query goes local when a
                             local specialist is already awake, else to the cloud
                             (avoids a ~50 s GPU wake when the pool is idle/torn
                             down, freeing the GPU for the command path).
        local_available_fn → callable () -> bool, True when a local specialist is
                             currently GPU-resident.
        """
        self._cloud_dev_agent = cloud_agent
        self._cloud_always = always_cloud
        self._local_specialist_available = local_available_fn
        log.info(
            "HybridCoordinator: CloudDevAgent wired (always_cloud=%s, model=%s)",
            always_cloud, getattr(cloud_agent, "model", "?"),
        )

    def _should_route_cloud_dev(self) -> bool:
        """Decide whether a dev-domain query should go to the cloud agent."""
        if self._cloud_dev_agent is None:
            return False
        if self._cloud_always:
            return True
        # Fallback mode: cloud only when no local specialist is currently awake.
        if self._local_specialist_available is None:
            return False
        try:
            return not self._local_specialist_available()
        except Exception as exc:
            log.debug("local-specialist availability check failed: %s", exc)
            return False

    def _record_dev_command(self, text: str) -> None:
        self._recent_dev_commands.append(text)
        if len(self._recent_dev_commands) > 10:
            self._recent_dev_commands = self._recent_dev_commands[-10:]

    def set_whisper_stream(self, whisper_stream) -> None:
        self._whisper = whisper_stream

    def set_profiler(self, profiler) -> None:
        self._profiler = profiler

    def set_calibrator(self, calibrator) -> None:
        self._calibrator = calibrator

    def set_metrics(self, metrics) -> None:
        """Wire the global Metrics singleton for real-time observability."""
        self._metrics = metrics

    def add_personal_corrections(self, corrections: dict) -> None:
        """Merge condition-specific corrections into the live _VOICE_CORRECTIONS map."""
        global _VOICE_CORRECTIONS
        for heard, expected in corrections.items():
            if heard not in _VOICE_CORRECTIONS:
                _VOICE_CORRECTIONS[heard] = expected
                log.info("Personal correction loaded: %r → %r", heard, expected)

    async def _switch_condition(self, condition: str) -> None:
        """Load and apply a voice profile for the given condition."""
        if self._profiler and hasattr(self._profiler, 'load'):
            await self._profiler.load(condition)
            if self._whisper:
                await self._profiler.apply_to(self._whisper, coordinator=self)
            log.info("Condition switched to: %s", condition)

    async def _run_calibration(self, condition: str, quick: bool) -> None:
        """Start a guided voice calibration session."""
        if self._calibrator:
            report = await self._calibrator.run(condition=condition, quick=quick)
            # Apply the newly built profile immediately
            await self._switch_condition(condition)
            log.info(
                "Calibration complete: condition=%s accuracy=%.0f%% corrections=%d",
                report.condition, report.accuracy * 100, report.corrections_added,
            )

    def set_fusion_engine(self, fusion_engine) -> None:
        """Wire FusionEngine so pain-day thresholds propagate on each route()."""
        self._fusion = fusion_engine

    def set_memory(self, memory) -> None:
        """Wire MemoryManager for standardised storage access."""
        self._memory = memory

    def _approval_config(self) -> dict:
        """Load approval_config.json; returns {} on failure (safe defaults used by callers)."""
        try:
            import json
            from pathlib import Path
            p = Path(__file__).parent.parent / "approval_config.json"
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}

    async def _tts_speak(self, text: str) -> None:
        """Speak text via the configured TTS backend (fire-and-forget safe)."""
        try:
            from tts.polly_stream import get_client as _get_tts
            await asyncio.to_thread(_get_tts().speak_sync, text)
        except Exception as exc:
            log.debug("HybridCoordinator._tts_speak failed: %s", exc)

    async def _audit_history_summary(self, n: int = 5) -> str:
        """Query audit.db for the last `n` mcp_call events and return a spoken summary."""
        if self._audit is None:
            return "Audit log not available."
        try:
            rows = await self._audit.get_recent_mcp_calls(n=n)
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
            log.debug("HybridCoordinator._audit_history_summary failed: %s", exc)
            return "Could not retrieve history."

    async def route(self, cmd: Command) -> dict:
        """Route a Command through the gate decision tree and execute it.

        Dev-domain queries (code, math, vision, plan, general) are intercepted
        here and forwarded to DevAgent before the accessibility pipeline runs.
        """
        # Cross-layer trace: set the current-trace ContextVar so every awaited
        # descendant (router, executor) attaches spans without new params. Runs
        # as its own scheduler task, so no reset is needed. No-op unless DA_TRACE.
        _tracer = get_tracer()
        if _tracer.enabled:
            _tid = cmd.trace_id or _tracer.new_trace(source=cmd.source)
            if not cmd.trace_id:
                cmd.trace_id = _tid
            _tracer.set_current(_tid)

        # --- Dev-agent pre-gate: intercept non-command domains ---
        # Skip for voice system-control keywords so they reach the keyword block
        # below instead of being misrouted to an LLM (e.g. "pain day on").
        # Skip for bypass sources (touch / multimodal): these arrive
        # with a concrete accessibility action already resolved (e.g. a tilt-tap is
        # source="touch" action="CLICK" text="tilt_tap"). Classifying their text
        # would send "tilt_tap" to the DevAgent as a general-domain query and the
        # click would never fire — they must fall through to the gate-bypass path.
        if (
            self._dev_agent
            and not _is_system_control_voice(cmd)
            and cmd.source not in _BYPASS_SOURCES
        ):
            domain = self._get_domain_classifier().classify(cmd.text)
            if domain != "command":
                # Cloud DevAgent branch — route to Claude when configured to,
                # avoiding a 30B specialist wake (and the GPU teardown it forces).
                if self._should_route_cloud_dev():
                    log.info("HybridCoordinator: dev-domain=%s → CloudDevAgent (%s)",
                             domain, getattr(self._cloud_dev_agent, "model", "?"))
                    ctx = {
                        "session_id": self._session_id,
                        "recent_commands": list(self._recent_dev_commands),
                        "source": cmd.source,
                    }
                    response_text = await self._cloud_dev_agent.run(cmd.text, domain, ctx)
                    self._record_dev_command(cmd.text)
                    if self._twin:
                        self._twin.clear_dev_namespace()
                    return {
                        "status": "ok",
                        "action": "dev_agent",
                        "domain": domain,
                        "model": self._cloud_dev_agent.model,
                        "response": response_text,
                        "steps": 0,
                        "backend": "anthropic_cloud",
                    }

                log.info("HybridCoordinator: dev-domain=%s → DevAgent", domain)
                agent_result = await self._dev_agent.handle(cmd.text)
                self._record_dev_command(cmd.text)
                if self._twin:
                    self._twin.clear_dev_namespace()
                return {
                    "status": "ok",
                    "action": "dev_agent",
                    "domain": agent_result.domain,
                    "model": agent_result.model_used,
                    "response": agent_result.response_text,
                    "steps": len(agent_result.steps),
                    "backend": "local",
                }

        # System control commands — intercept before gate evaluation
        if cmd.source in ("voice", "voice_local"):
            # Strip surrounding whitespace AND punctuation: Whisper routinely
            # appends a period ("pain day on." / "lecture mode on?") which would
            # otherwise miss every exact-match keyword below.
            _lower = cmd.text.lower().strip(" \t\n.,!?;:\"'")

            # Lecture mode
            if _lower in ("start lecture mode", "lecture mode on", "begin lecture mode"):
                self._lecture_mode = True
                if self._whisper:
                    self._whisper.set_lecture_mode(True)
                log.info("Lecture mode ON")
                return {"status": "ok", "action": "LECTURE_MODE", "enabled": True}
            elif _lower in ("stop lecture mode", "lecture mode off", "end lecture mode"):
                self._lecture_mode = False
                if self._whisper:
                    self._whisper.set_lecture_mode(False)
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
                if self._agent_db and self._agent_db.available and search_q:
                    rows = await self._agent_db.search_lecture_notes(search_q, limit=10)
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
                if self._twin:
                    self._twin.set_manual_pain_day(True)
                # Immediately relax the recognizer (VAD + logprob floor) for the
                # next utterance, before route() reconciles on the next command.
                if self._whisper is not None:
                    self._whisper.apply_pain_day(True)
                return {"status": "ok", "action": "PAIN_DAY", "enabled": True}
            elif _lower in ("pain day off", "flare day off", "feeling better"):
                if self._twin:
                    self._twin.set_manual_pain_day(False)
                if self._whisper is not None:
                    self._whisper.apply_pain_day(False)
                return {"status": "ok", "action": "PAIN_DAY", "enabled": False}

            # Condition switching — loads calibrated voice profile for condition
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
                "having an svt":           "svt_attack",
                "svt attack":              "svt_attack",
                "svt mode":                "svt_attack",
                "this is an svt day":      "svt_attack",
            }
            if _lower in _CONDITION_TRIGGERS and self._profiler:
                condition = _CONDITION_TRIGGERS[_lower]
                t = asyncio.create_task(self._switch_condition(condition))
                t.add_done_callback(lambda t: self._on_task_done(t, "_switch_condition"))
                return {"status": "ok", "action": "CONDITION_SWITCH",
                        "condition": condition}

            # Calibration triggers
            _CALIBRATION_TRIGGERS: dict[str, tuple[str, bool]] = {
                "run voice calibration":   ("good_day",    False),
                "calibrate my voice":      ("good_day",    False),
                "quick calibration":       ("good_day",    True),
                "calibrate flare day":     ("flare_day",   False),
                "calibrate allergy day":   ("allergy_day", False),
                "calibrate svt":           ("svt_attack",  False),
                "calibrate svt attack":    ("svt_attack",  False),
            }
            if _lower in _CALIBRATION_TRIGGERS and self._calibrator:
                condition, quick = _CALIBRATION_TRIGGERS[_lower]
                t = asyncio.create_task(self._run_calibration(condition, quick))
                t.add_done_callback(lambda t: self._on_task_done(t, "_run_calibration"))
                return {"status": "ok", "action": "CALIBRATION_START",
                        "condition": condition, "quick": quick}

            # ── Goal-level agent control ──────────────────────────────────────

            # "hey agent status" / "what are you doing"
            if _lower in ("hey agent status", "what are you doing", "agent status",
                          "status", "what's happening"):
                if self._dev_agent is not None:
                    ps = self._dev_agent.get_plan_status()
                    if ps.get("active"):
                        msg = (f"Running step {ps['step']} of {ps['total_steps']}: "
                               f"{ps.get('goal', '')[:50]}")
                    else:
                        msg = "No active task."
                    asyncio.create_task(self._tts_speak(msg))
                    return {"status": "ok", "action": "AGENT_STATUS", "plan": ps}

            # "hey agent stop" / "cancel task" / "cancel agent"
            elif _lower in ("hey agent stop", "cancel task", "cancel agent", "stop agent",
                            "stop the agent", "cancel the task"):
                if self._dev_agent is not None:
                    self._dev_agent.request_cancel()
                    asyncio.create_task(self._tts_speak("Cancelling after current step."))
                    return {"status": "ok", "action": "AGENT_CANCEL"}
                return {"status": "ok", "action": "AGENT_CANCEL", "note": "no active agent"}

            # "hey agent authorize <goal>" — create a standalone goal session
            elif _lower.startswith("hey agent authorize ") or _lower.startswith("authorize "):
                goal_text = (cmd.text.split("authorize ", 1)[-1]).strip(" .,!?\"'")
                if goal_text:
                    from core.goal_session import GoalSessionStore
                    duration = self._approval_config().get("goal_session_duration_s", 900)
                    max_act = self._approval_config().get("goal_session_max_actions", 50)
                    GoalSessionStore.create(goal=goal_text, domain="plan",
                                            duration_s=duration, max_actions=max_act)
                    mins = int(duration / 60)
                    asyncio.create_task(
                        self._tts_speak(f"Goal authorized for {mins} minutes: {goal_text[:40]}")
                    )
                    return {"status": "ok", "action": "GOAL_AUTHORIZE", "goal": goal_text}

            # "hey agent history" / "what did you do"
            elif _lower in ("hey agent history", "what did you do", "agent history",
                            "show history", "recent actions"):
                summary = await self._audit_history_summary(n=5)
                asyncio.create_task(self._tts_speak(summary))
                return {"status": "ok", "action": "AGENT_HISTORY", "summary": summary}

            # Mic mute — voice one-way; unmute via iPad mic_mute message
            elif _lower in ("mute mic", "mute microphone", "mic off", "silence mic"):
                if self._whisper is not None:
                    self._whisper.set_muted(True)
                asyncio.create_task(self._tts_speak("Microphone muted. Tap the iPad to unmute."))
                return {"status": "ok", "action": "MIC_MUTE", "muted": True}

        t0 = time.monotonic()
        route_label = "local"
        gate_that_decided = "all_pass"
        action_str: Optional[str] = None
        success: Optional[bool] = None
        error_msg: Optional[str] = None
        command_id: int = -1

        try:
            source = cmd.source

            # --- Twin state snapshot and adjustments -----------------------
            from adaptive.behavioral_twin_state import _DEFAULT_SNAPSHOT
            snapshot = _DEFAULT_SNAPSHOT
            if self._twin:
                try:
                    snapshot = await self._twin.get_snapshot()
                except Exception as exc:
                    log.warning("BehavioralTwinState.get_snapshot failed: %s", exc)

            # Apply pain-day threshold adjustments
            if snapshot.pain_day_active:
                effective_cfg = _apply_pain_day_adjustments(self._cfg, snapshot)
            else:
                effective_cfg = self._cfg

            # Propagate pain-day state to sensor + voice thresholds. Each
            # consumer relaxes only if its flare_profile degrade flag is set,
            # and apply_pain_day is idempotent so calling every command is cheap.
            if self._fusion is not None:
                self._fusion.apply_pain_day(
                    tilt=snapshot.pain_day_active and snapshot.flare_tilt_degrades,
                )
            if self._whisper is not None:
                self._whisper.apply_pain_day(
                    snapshot.pain_day_active and snapshot.flare_voice_degrades
                )

            # Always apply vocabulary corrections before any gate evaluation
            # so app-name phonetics ("key-row" → "kiro") reach the LLM fixed
            # regardless of Whisper confidence level.
            vocab_corrected = False
            if cmd.source in ("voice", "voice_local"):
                corrected_text, changed = _apply_vocabulary_corrections(cmd.text)
                if changed:
                    log.debug(
                        "Pre-gate vocab correction: %r → %r", cmd.text, corrected_text
                    )
                    cmd = _dc_replace(cmd, text=corrected_text)
                    vocab_corrected = True

            # Populate session_context from twin state (always accessibility namespace)
            if self._twin and self._twin.is_ready:
                cmd = _dc_replace(cmd, session_context=self._twin.get_session_context("accessibility"))

            # Inject pending clarification so the LLM knows what "up" or "yes"
            # is answering.  Prepended so it appears closest to the user turn.
            if self._pending_clarification and cmd.source in ("voice", "voice_local"):
                clarify_ctx = f"[PENDING CLARIFICATION: {self._pending_clarification}]"
                ctx = [clarify_ctx] + list(cmd.session_context or [])
                cmd = _dc_replace(cmd, session_context=ctx)

            # Temporarily apply effective_cfg for gate evaluation
            _original_cfg = self._cfg
            self._cfg = effective_cfg
            try:
                # --- Bypass path (touch / multimodal) --------------------------
                # These sources always run local and never reach the cloud, so
                # Gate 0 — whose sole purpose is to keep sensitive text off an
                # external API — does not apply and is checked AFTER this branch.
                # Touch commands (iPad CommandPad taps, tilt-tap) arrive with a
                # concrete action already resolved — the text is just a label
                # ("tilt_tap"). Honor that action directly instead of asking the
                # LLM to infer it from the label, which yields CLARIFY ("What is
                # the target of the click?") because the model can't read
                # "tilt_tap" as a verb. Multimodal still infers via the LLM
                # (its action depends on which phrase fired).
                if source in _BYPASS_SOURCES:
                    if cmd.source == "touch" and cmd.action in _VALID_COMMAND_VERBS:
                        action_str = cmd.action
                    else:
                        action_str = await self._run_local(cmd)
                    route_label = "local"
                    gate_that_decided = "bypass"

                # --- Gate 0 — Privacy (force local for cloud-eligible sources) --
                elif not self._gate0(cmd):
                    log.debug("Gate 0 force-local (sensitive data): %r", cmd.text)
                    action_str = await self._run_local(cmd)
                    route_label = "local"
                    gate_that_decided = "gate0_privacy"

                # --- Skip Gate 1 path ------------------------------------------
                elif source in _SKIP_GATE1_SOURCES:
                    action_str, gate_that_decided, route_label = await self._gates_2_to_4(cmd)

                # --- Full 4-gate path -------------------------------------------
                else:
                    # Gate 1 — Confidence
                    passed, cmd = await self._gate1(cmd)
                    if passed is None:
                        # Gesture low confidence — discard silently
                        log.debug("Gate 1 discard (low gesture conf): %r", cmd.text)
                        return {"status": "discarded", "reason": "gate1_gesture_conf"}
                    if not passed:
                        # Voice low confidence. If the pre-gate vocabulary pass
                        # already fixed a KNOWN misrecognition the transcript is now
                        # high-confidence — continue local (fast, no round-trip).
                        # Otherwise it's an UNKNOWN low-confidence utterance:
                        # escalate to the cloud, whose system prompt is tuned to
                        # repair voice misrecognitions the local dictionary can't.
                        # Gate 0 has already passed here, so no sensitive data is
                        # transmitted.
                        if vocab_corrected:
                            cmd = await _retranscribe(cmd)
                            action_str, gate_that_decided, route_label = \
                                await self._gates_2_to_4(cmd)
                        else:
                            log.info(
                                "Gate 1 voice low-confidence (logprob=%.3f) — "
                                "escalating to cloud for misrecognition repair",
                                cmd.whisper_logprob,
                            )
                            action_str = await self._run_cloud(cmd)
                            gate_that_decided = "gate1_voice_conf"
                            route_label = "cloud"
                    else:
                        action_str, gate_that_decided, route_label = \
                            await self._gates_2_to_4(cmd)
            finally:
                self._cfg = _original_cfg

            # --- Execute the action ----------------------------------------
            result = await self._execute_action(action_str, cmd, route_label=route_label)
            success = result.get("status") == "ok"

            # Persist to DB now so command_id is valid before trainer uses it
            latency_ms = (time.monotonic() - t0) * 1000
            if self._agent_db and self._agent_db.available:
                try:
                    command_id = await self._agent_db.insert_command(
                        session_id=self._session_id,
                        cmd=cmd,
                        action=action_str,
                        route=route_label,
                        gate_that_decided=gate_that_decided,
                        latency_ms=latency_ms,
                        success=success,
                        error_msg=error_msg,
                        trace_id=cmd.trace_id or None,
                    )
                except Exception as db_exc:
                    log.warning("AgentDB.insert_command failed: %s", db_exc)

            # Record successful local executions for few-shot learning
            if (self._trainer and route_label == "local" and success):
                await self._trainer.record_success(
                    cmd, action_str, command_id=command_id
                )
            # Feed failed local executions into the twin's pain-day fail signal
            # ONLY (never the few-shot store). CLARIFY executes with status "ok"
            # so it is a success here, not a failure. Cloud outcomes stay out of
            # the twin, matching the local-only success gate above.
            elif (self._trainer and route_label == "local" and not success):
                await self._trainer.record_failure(
                    cmd, action_str, command_id=command_id
                )

            # D3: drain gesture velocity samples after every gesture command
            # that cleared Gate 1, regardless of execution outcome.
            if self._trainer and cmd.source == "gesture":
                await self._trainer.drain_and_persist_velocity(
                    pain_day=snapshot.pain_day_active
                )

            # D8: handle voice corrections — record the right action for
            # commands where the user said "no/wait/actually <new command>"
            if cmd.source == "voice_correction" and self._trainer:
                await self._on_correction(cmd, action_str)

            # D8: record action/status so WhisperStream can detect next correction
            self._last_executed_action = action_str or ""
            self._last_command_id = command_id
            if self._whisper:
                status_str = "ok" if success else ("CLARIFY" if action_str == "CLARIFY" else "failed")
                self._whisper.set_last_command_status(status_str, cmd.text)

            # Advance acoustic profiler command counter (seasonal drift check)
            if self._whisper and hasattr(self._whisper, "_profiler") \
                    and self._whisper._profiler:
                self._whisper._profiler.on_any_command()

        except Exception as exc:
            log.error("HybridCoordinator.route error: %s", exc)
            error_msg = str(exc)
            return {"status": "error", "error": str(exc)}

        finally:
            latency_ms = (time.monotonic() - t0) * 1000
            # Gate 4's EMA exists to detect when LOCAL inference is getting slow
            # (e.g. GPU contention during a flare) and shed load to the cloud.
            # Feeding cloud round-trip latency — inherently several times the
            # local budget — back into it would inflate the EMA, trip Gate 4, and
            # push even more commands to the cloud: a positive feedback loop that
            # never recovers. So only local routes update the Gate 4 EMA; when a
            # burst goes to cloud the EMA stays low and local is retried promptly.
            if route_label == "local":
                self._update_ema(latency_ms)
            # Record outcome in metrics singleton (non-fatal)
            if self._metrics is not None:
                try:
                    self._metrics.record_command_outcome(
                        success=success,
                        action=action_str or "",
                        latency_ms=latency_ms,
                        route=route_label,
                        domain=getattr(cmd, "domain", None),
                        gate=gate_that_decided,
                        whisper_logprob=getattr(cmd, "whisper_logprob", None),
                        gesture_conf=getattr(cmd, "gesture_confidence", None),
                    )
                except Exception:
                    pass
            # Cross-layer trace: the decisive routing span (no-op unless DA_TRACE)
            try:
                _tracer.record_span(
                    "route_decision", route=route_label, gate=gate_that_decided,
                    action=action_str or None, success=success,
                    dur_ms=round(latency_ms, 1),
                )
            except Exception:
                pass

        return result

    # ---------------------------------------------------------------------- #
    # Gate implementations
    # ---------------------------------------------------------------------- #

    async def _on_correction(self, cmd: Command, correct_action: str) -> None:
        """D8: Record a voice correction as a few-shot example.

        Called when the user says "no/wait/actually <new command>" after a
        failed or CLARIFY outcome.  The corrected text becomes the canonical
        example for future routing of similar commands.
        """
        if not self._trainer:
            return
        try:
            await self._trainer.record_correction(
                cmd=cmd,
                wrong_action=self._last_executed_action,
                correct_action=correct_action,
                command_id=self._last_command_id,
            )
            log.info(
                "HybridCoordinator: correction recorded %r → %r",
                self._last_executed_action, correct_action,
            )
        except Exception as exc:
            log.warning("HybridCoordinator._on_correction failed: %s", exc)

    def _gate0(self, cmd: Command) -> bool:
        """Gate 0 — Privacy. True = pass (safe to consider cloud).

        Checks command text against patterns for credentials, financial data,
        and PII. A match forces local routing regardless of subsequent gates.
        """
        if not self._cfg.gate0_enabled:
            return True
        text = cmd.text.lower()
        return not any(pat in text for pat in self._cfg.gate0_sensitive_patterns)

    async def _gate1(self, cmd: Command) -> tuple[bool | None, Command]:
        """Gate 1 — Confidence.

        Returns:
          (True,  cmd) — pass
          (False, cmd) — fail-voice (low whisper logprob)
          (None,  cmd) — fail-gesture (discard)
        """
        cfg = self._cfg
        logprob_ok = cmd.whisper_logprob >= cfg.whisper_logprob_min
        gesture_ok = cmd.gesture_confidence >= cfg.gesture_confidence_min

        if logprob_ok and gesture_ok:
            return True, cmd

        # Distinguish gesture failure (source=gesture) vs voice failure
        if cmd.source == "gesture" and not gesture_ok:
            return None, cmd  # discard

        # Voice low confidence — signal for re-transcription
        log.debug(
            "Gate 1 fail (voice): logprob=%.3f gesture=%.3f",
            cmd.whisper_logprob,
            cmd.gesture_confidence,
        )
        return False, cmd

    async def _gates_2_to_4(
        self, cmd: Command
    ) -> tuple[str, str, str]:
        """Run Gates 2-4. Returns (action_str, gate_that_decided, route_label)."""
        # Gate 2 — Complexity
        if not self._gate2(cmd):
            log.debug("Gate 2 fail (complexity): %r", cmd.text)
            return await self._run_cloud(cmd), "gate2_complexity", "cloud"

        # Gate 3 — VRAM
        if not self._gate3():
            log.debug("Gate 3 fail (VRAM)")
            return await self._run_cloud(cmd), "gate3_vram", "cloud"

        # Gate 4 — Latency EMA
        if not self._gate4():
            log.debug(
                "Gate 4 fail (latency EMA=%.0f ms)", self._latency_ema or 0
            )
            return await self._run_cloud(cmd), "gate4_latency", "cloud"

        return await self._run_local(cmd), "all_pass", "local"

    def _gate2(self, cmd: Command) -> bool:
        """Gate 2 — Complexity. True = pass (route local)."""
        text = cmd.text.lower()
        if any(kw in text for kw in _COMPLEXITY_KEYWORDS):
            return False
        token_count = len(cmd.text.split())
        return token_count <= self._cfg.max_local_tokens

    def _gate3(self) -> bool:
        """Gate 3 — VRAM. True = pass.

        Caches the result for 2 seconds to avoid calling pynvml (synchronous
        CUDA API) on every command. VRAM doesn't change fast enough to need
        per-command granularity.
        """
        now = time.monotonic()
        if self._vram_cache is not None:
            cached_result, cached_time = self._vram_cache
            if now - cached_time < self._vram_cache_ttl:
                return cached_result

        try:
            import pynvml as nvml
            nvml.nvmlInit()
            handle = nvml.nvmlDeviceGetHandleByIndex(0)
            info = nvml.nvmlDeviceGetMemoryInfo(handle)
            free_gb = info.free / (1024 ** 3)
            nvml.nvmlShutdown()
            result = free_gb >= self._cfg.vram_free_min_gb
        except Exception as exc:
            log.debug("Gate 3 NVML error (assuming pass): %s", exc)
            result = True  # if we can't check, don't penalise local

        self._vram_cache = (result, now)
        return result

    def _gate4(self) -> bool:
        """Gate 4 — Latency EMA. True = pass."""
        if self._latency_ema is None:
            return True  # no history yet — optimistically run local
        return self._latency_ema <= self._cfg.latency_budget_ms

    # ---------------------------------------------------------------------- #
    # Inference helpers
    # ---------------------------------------------------------------------- #

    async def _run_local(self, cmd: Command) -> str:
        examples = (
            await self._trainer.get_few_shot_examples(cmd)
            if self._trainer else None
        )
        t0 = time.monotonic()
        # Circuit-breaker: a wedged local backend (Ollama hung, GPU stuck during
        # a flare, stalled model reload) must not stall the pipeline forever.
        # On timeout, degrade to CLARIFY rather than blocking the accessibility
        # path indefinitely. Mirrors the cloud-path guard in _run_cloud().
        try:
            async with asyncio.timeout(self._cfg.local_timeout_s):
                action_str = await self._local.infer(cmd, few_shot_examples=examples)
        except TimeoutError:
            log.error(
                "HybridCoordinator: local inference timed out after %.0fs — CLARIFY fallback",
                self._cfg.local_timeout_s,
            )
            return "CLARIFY local inference timed out"
        latency_ms = (time.monotonic() - t0) * 1000
        # NOTE: Do NOT call _update_ema here — route()'s finally block already
        # updates EMA with the total route latency (which includes inference).
        # Calling it here too would double-count inference time in Gate 4's EMA.
        if self._agent_db and self._agent_db.available:
            status = self._local.get_status()
            error = action_str if action_str.startswith("CLARIFY inference") else None
            await self._agent_db.insert_inference(
                command_id=None,
                model=status.get("model", "unknown"),
                domain="command",
                prompt=None,
                response=action_str,
                tokens_in=None,
                tokens_out=None,
                latency_ms=latency_ms,
                backend=status.get("backend", "ollama"),
                error=error,
            )
        try:
            _st = self._local.get_status()
            get_tracer().record_span(
                "inference", route="local", backend=_st.get("backend"),
                model=_st.get("model"), dur_ms=round(latency_ms, 1),
            )
        except Exception:
            pass
        return action_str

    _CLOUD_TIMEOUT_S = 10.0  # circuit-breaker: cloud inference must complete within this window

    async def _run_cloud(self, cmd: Command) -> str:
        """Route to Anthropic API (Claude).

        Content filter scrubs secrets/PII from the command text before
        transmitting to external APIs. Findings are logged to the audit trail.

        A 10-second asyncio.timeout acts as a circuit-breaker: if the API hangs
        (network drop, service hiccup) the coroutine is cancelled and the
        pipeline degrades to a CLARIFY response rather than stalling indefinitely.
        """
        # Scrub secrets before sending to external API
        if self._content_filter:
            clean_text, findings = await self._content_filter.scrub(cmd.text)
            if findings:
                log.info("ContentFilter: redacted %d secret(s) before cloud call", len(findings))
                # replace() preserves every field (action, source, params, …) and
                # only overrides text — the manual rebuild dropped the required
                # `action` and used a nonexistent `_gaze_coords` kwarg (TypeError).
                cmd = _dc_replace(cmd, text=clean_text)

        try:
            async with asyncio.timeout(self._CLOUD_TIMEOUT_S):
                with get_tracer().timed("inference", route="cloud"):
                    return await self._cloud.infer(cmd)
        except TimeoutError:
            log.error(
                "HybridCoordinator: cloud inference timed out after %.0fs — CLARIFY fallback",
                self._CLOUD_TIMEOUT_S,
            )
            return "CLARIFY cloud inference timed out"

    # ---------------------------------------------------------------------- #
    # Action execution
    # ---------------------------------------------------------------------- #

    async def _execute_action(
        self, action_str: str, cmd: Command, route_label: str = "local"
    ) -> dict:
        """Parse the LLM's action string and execute it via CommandExecutor."""
        if not action_str:
            return {"status": "error", "error": "empty action string"}

        verb, target = self._parse_action(action_str)

        # Output-schema validation: the command-path LLM must answer with one of
        # the 11 accessibility verbs. A response whose first token is anything
        # else (prose, a hallucinated verb, a refusal) is malformed — degrade to
        # CLARIFY so the user is re-prompted instead of dispatching a bad verb
        # that would surface as a raw "Unknown action" error. The original
        # malformed action_str is still recorded to agent.db by route() for
        # later analysis.
        if verb not in _VALID_COMMAND_VERBS:
            log.warning(
                "Malformed LLM action %r — first token %r not in command "
                "vocabulary; degrading to CLARIFY",
                action_str, verb,
            )
            if self._metrics is not None:
                _rec = getattr(self._metrics, "record_malformed_action", None)
                if _rec is not None:
                    try:
                        _rec(action_str)
                    except Exception:
                        pass
            verb = "CLARIFY"
            target = "Sorry, I didn't catch that. Could you say it again?"

        params = self._parse_params(verb, target, cmd)
        # CLARIFY from a cloud route should speak via Polly TTS
        if route_label == "cloud":
            params["route"] = "cloud"

        # Vision grounding: resolve named CLICK targets to pixel coords.
        # Only runs when there's a named target and no coords already supplied
        # (explicit click coords or x/y from touch). Falls through silently on
        # any failure — CommandExecutor's Tesseract + cursor fallback takes over.
        grounded_coords = cmd.gaze_coords
        if verb == "CLICK" and target and "x" not in params:
            vision_coords = await self._ground_target(target)
            if vision_coords:
                params["x"], params["y"] = vision_coords
                grounded_coords = vision_coords

        exec_cmd = Command(
            text=target or cmd.text,
            action=verb,
            source=cmd.source,
            whisper_logprob=cmd.whisper_logprob,
            gesture_confidence=cmd.gesture_confidence,
            session_context=cmd.session_context,
            gaze_coords=grounded_coords,
            params=params,
        )

        # Pre-suppress: flush buffer and block mic BEFORE TTS starts playing.
        # This prevents Danielle's voice from accumulating in the WhisperStream
        # buffer and being transcribed as a new command mid-playback.
        if verb == "CLARIFY" and self._whisper is not None:
            word_count = len((target or "").split())
            # Generative TTS is ~0.45s/word; add 1s safety margin before + tail
            pre_suppress_s = max(3.0, word_count * 0.45 + 1.0)
            self._whisper.suppress(pre_suppress_s)
            log.debug("Pre-CLARIFY mic suppressed for %.1fs", pre_suppress_s)

        with get_tracer().timed("execute", verb=verb):
            result = await self._executor.execute(exec_cmd)

        # Post-action suppression:
        #   CLARIFY  — long suppress covers TTS echo (already pre-suppressed too)
        #   All else — short cooldown prevents app-startup sounds / utterance
        #              tail from being transcribed as a second command.
        if verb == "CLARIFY":
            self._pending_clarification = target or "unclear command"
            if self._whisper is not None:
                self._whisper.suppress(1.5)
                self._whisper.set_awaiting_clarification(True)
                log.debug("Post-CLARIFY mic suppressed 1.5s; wake-phrase bypassed")
        else:
            self._pending_clarification = None
            if self._whisper is not None:
                self._whisper.set_awaiting_clarification(False)
                if result.get("status") == "ok":
                    self._whisper.suppress(0.8)
                    log.debug("Post-action mic cooldown 0.8s (verb=%s)", verb)

        return result

    async def _ground_target(self, target: str) -> Optional[tuple[int, int]]:
        """Screenshot the desktop and ask VisionGrounder to resolve the target."""
        try:
            # mcp_server/ is already in sys.path via command_executor import
            from tools import screen as _screen
            screenshot_result = await asyncio.to_thread(_screen.screenshot)
            screenshot_b64: str = screenshot_result.get("image_base64", "")
            if not screenshot_b64:
                return None
            result = await asyncio.to_thread(
                self._grounder.ground, target, screenshot_b64
            )
            if result is None:
                return None
            return (result.x, result.y)
        except Exception as exc:
            log.warning("_ground_target failed for %r: %s", target, exc)
            return None

    @staticmethod
    def _parse_action(action_str: str) -> tuple[str, str]:
        """Split a raw LLM action string into (verb, target).

        verb is upper-cased; target is the remainder (may be empty). Callers
        validate verb against _VALID_COMMAND_VERBS before dispatch. Tolerates a
        single leading "Action:"/"ACTION:" label some models prepend, but does
        not otherwise hunt for a verb mid-string — an accessibility tool should
        re-prompt rather than guess at a destructive action.
        """
        text = action_str.strip()
        # Strip one optional leading label, e.g. "Action: CLICK button".
        low = text.lower()
        for label in ("action:", "command:"):
            if low.startswith(label):
                text = text[len(label):].strip()
                break
        parts = text.split(None, 1)
        if not parts:
            return "", ""
        verb = parts[0].upper().rstrip(":")
        target = parts[1].strip() if len(parts) > 1 else ""
        return verb, target

    @staticmethod
    def _parse_params(verb: str, target: str, original: Command) -> dict:
        """Extract verb-specific params from the LLM target string."""
        params: dict = {}

        if verb == "SCROLL":
            words = target.lower().split()
            direction = "down"
            for w in words:
                if w in ("up", "down", "left", "right"):
                    direction = w
                    break
            amount = 3
            for w in words:
                try:
                    amount = int(w)
                    break
                except ValueError:
                    pass
            params = {"direction": direction, "amount": amount}

        elif verb == "CLICK":
            # Carry through explicit touch coords (tilt-tap pins cursor x/y +
            # snap_nearest in FusionEngine.on_touch). Without this the coords are
            # dropped and the click falls back to re-searching UIA for the text.
            if "x" in original.params and "y" in original.params:
                params = {"x": original.params["x"], "y": original.params["y"]}
                if original.params.get("snap_nearest"):
                    params["snap_nearest"] = True
            elif original.gaze_coords:
                x, y = original.gaze_coords
                params = {"x": x, "y": y}

        elif verb == "TYPE":
            params = {"text": target}

        elif verb == "OPEN":
            params = {"target": target}

        elif verb == "HOTKEY":
            keys = [k.strip() for k in target.replace("+", " ").split() if k.strip()]
            params = {"keys": keys}

        elif verb == "CLARIFY":
            params = {"message": target}

        return params

    # ---------------------------------------------------------------------- #
    # Latency EMA
    # ---------------------------------------------------------------------- #

    def _update_ema(self, latency_ms: float) -> None:
        α = self._cfg.latency_ema_alpha
        if self._latency_ema is None:
            self._latency_ema = latency_ms
        else:
            self._latency_ema = α * latency_ms + (1 - α) * self._latency_ema

    # ---------------------------------------------------------------------- #
    # Correction API — user feedback loop
    # ---------------------------------------------------------------------- #

    async def correct(self, cmd: Command, wrong_action: str, correct_action: str) -> dict:
        """Record a user correction for a misresolved command.

        Called when the user indicates the last action was wrong and provides
        the correct one (e.g. via iPad "undo + correct" flow or voice "no, I
        meant close").

        Args:
            cmd: The original Command that was misresolved.
            wrong_action: The action that was incorrectly executed.
            correct_action: The action the user actually wanted.

        Returns:
            Status dict with confirmation.
        """
        log.info(
            "Correction received: %r → was %s, should be %s",
            cmd.text, wrong_action, correct_action,
        )

        if self._trainer:
            await self._trainer.record_correction(cmd, wrong_action, correct_action)

        return {
            "status": "ok",
            "correction": {
                "text": cmd.text,
                "wrong": wrong_action,
                "correct": correct_action,
            },
        }

    # ---------------------------------------------------------------------- #
    # Status
    # ---------------------------------------------------------------------- #

    def get_status(self) -> dict:
        return {
            "local_backend": self._local.get_status(),
            "latency_ema_ms": round(self._latency_ema, 1) if self._latency_ema else None,
            "config": {
                "whisper_logprob_min": self._cfg.whisper_logprob_min,
                "gesture_confidence_min": self._cfg.gesture_confidence_min,
                "max_local_tokens": self._cfg.max_local_tokens,
                "vram_free_min_gb": self._cfg.vram_free_min_gb,
                "latency_budget_ms": self._cfg.latency_budget_ms,
            },
        }
