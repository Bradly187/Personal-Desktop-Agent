"""HybridCoordinator — 4-gate routing between local LLM and AWS Bedrock.

Receives a Command from FusionEngine, decides whether to run local inference
or fall back to the cloud, executes the resulting action, and logs the outcome.

Gate logic (source-dependent):
  touch / sound_action / gaze_dwell / multimodal → bypass all 4 gates → local
  voice_local                                     → skip Gate 1 → gates 2-4
  gesture / voice                                 → full 4-gate evaluation

Gate 0 — Privacy:     command text contains no sensitive-data patterns
  fail → force local (never send to cloud)

Gate 1 — Confidence:  whisper_logprob ≥ min AND gesture_conf ≥ min
  fail-voice    → Amazon Transcribe re-transcription, then retry Gate 2
  fail-gesture  → discard silently

Gate 2 — Complexity:  token_count ≤ max AND no complexity keywords
  fail → AWS Bedrock

Gate 3 — VRAM:  vram_free_gb ≥ vram_free_min_gb  (via pynvml)
  fail → AWS Bedrock

Gate 4 — Latency EMA:  latency_ema_ms ≤ latency_budget_ms
  fail → AWS Bedrock

After inference: log outcome to agent.db (AgentDB), call CommandExecutor.execute().
Each log entry includes `gate_that_decided`: which gate was the decisive routing
factor ("bypass", "gate0_privacy", "gate2_complexity", "gate3_vram",
"gate4_latency", "all_pass", "discard").
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from command_executor import Command, CommandExecutor
from dataclasses import replace as _dc_replace
from local_inference import LocalInference, OllamaInference, _build_prompt, _SYSTEM_PROMPT
from vision_grounder import VisionGrounder

if TYPE_CHECKING:
    from audit_log import AuditLog
    from behavioral_twin_state import BehavioralTwinState
    from content_filter import ContentFilter
    from continuous_trainer import ContinuousTrainer
    from db import AgentDB
    from dev_agent import DevAgent

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

    # (routing_log_path removed — outcomes written to agent.db commands table)

    # AWS Bedrock (cloud fallback — raw API, used when AgentCore unavailable)
    # Model: Claude Haiku 4.5 cross-region inference profile (verified 2026-05-15).
    # Claude 3.5 Haiku is now marked Legacy and requires explicit model access re-grant.
    bedrock_model_id: str = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    bedrock_region: str = "us-east-1"

    # Cloud path: raw Bedrock Claude Haiku (AgentCore deployment deferred)


# ---------------------------------------------------------------------------
# Cloud inference (AWS Bedrock)
# ---------------------------------------------------------------------------

class _CloudInference:
    """AWS Bedrock Claude backend. Lazy boto3 import."""

    def __init__(self, model_id: str, region: str) -> None:
        self._model_id = model_id
        self._region = region
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                import boto3
                self._client = boto3.client("bedrock-runtime", region_name=self._region)
            except ImportError:
                raise RuntimeError("boto3 not installed — run: pip install boto3")
        return self._client

    async def infer(self, cmd: Command) -> str:
        try:
            client = self._get_client()
        except RuntimeError as exc:
            log.error("CloudInference unavailable: %s", exc)
            return f"CLARIFY cloud unavailable: {exc}"

        # Build few-shot context string for the user message
        context_lines = ""
        if cmd.session_context:
            joined = "\n".join(f"- {c}" for c in cmd.session_context[-5:])
            context_lines = f"\n\nRecent commands:\n{joined}"

        user_content = f"Command: {cmd.text}{context_lines}"

        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 64,
            "temperature": 0.0,
            "system": _CLOUD_SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": user_content}],
        }

        t0 = time.monotonic()
        try:
            result = await asyncio.to_thread(
                client.invoke_model,
                modelId=self._model_id,
                body=json.dumps(body),
                contentType="application/json",
                accept="application/json",
            )
            data = json.loads(result["body"].read())
            action = data["content"][0]["text"].strip().splitlines()[0].strip()
            latency_ms = (time.monotonic() - t0) * 1000
            log.info("CloudInference: %r → %r (%.0f ms)", cmd.text, action, latency_ms)
            return action
        except Exception as exc:
            log.error("CloudInference failed: %s", exc)
            return f"CLARIFY cloud error: {exc}"


# ---------------------------------------------------------------------------
# Amazon Transcribe re-transcription (Gate 1 voice fallback)
# ---------------------------------------------------------------------------

# Phonetic vocabulary corrections for the most common voice misrecognitions.
# Applied as a fast first pass before (optionally) calling Amazon Transcribe.
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
    """Attempt to improve a low-confidence voice transcript before Gate 2.

    Two-stage approach:
      1. Vocabulary correction — instant, no network, fixes the 6 deterministic
         misrecognitions documented in the cloud system prompt.
      2. Amazon Transcribe streaming — tried when `amazon-transcribe` is installed
         AND audio bytes were preserved in cmd.params['audio_bytes'].  Falls back
         gracefully (returns corrected or original cmd) on any error or timeout.

    To enable Transcribe streaming:
        pip install amazon-transcribe
    Audio bytes are preserved automatically by WhisperStream when
    `preserve_audio=True` (default True from Phase 6 onwards).
    """
    # --- Stage 1: deterministic vocabulary correction (always runs) ----------
    corrected_text, changed = _apply_vocabulary_corrections(cmd.text)
    if changed:
        log.info(
            "Gate 1 vocab correction: %r → %r", cmd.text, corrected_text
        )
        cmd = _dc_replace(cmd, text=corrected_text, whisper_logprob=0.0)

    # --- Stage 2: Amazon Transcribe streaming (optional) --------------------
    audio_bytes: bytes | None = cmd.params.get("audio_bytes")
    sample_rate: int = cmd.params.get("sample_rate", 16_000)

    if not audio_bytes:
        log.debug(
            "Gate 1 Transcribe: no audio bytes in cmd.params — skipping "
            "(WhisperStream.preserve_audio must be True)"
        )
        return cmd

    try:
        from amazon_transcribe.client import TranscribeStreamingClient  # type: ignore
        from amazon_transcribe.handlers import TranscriptResultStreamHandler  # type: ignore
        from amazon_transcribe.model import TranscriptEvent  # type: ignore
    except ImportError:
        log.debug("Gate 1 Transcribe: amazon-transcribe not installed — using vocab correction only")
        return cmd

    class _ResultHandler(TranscriptResultStreamHandler):
        def __init__(self, stream):
            super().__init__(stream)
            self.transcript = ""
            self.confidence = 0.0

        async def handle_transcript_event(self, event: TranscriptEvent):
            for result in event.transcript.results:
                if not result.is_partial and result.alternatives:
                    alt = result.alternatives[0]
                    self.transcript = alt.transcript
                    items = getattr(alt, "items", []) or []
                    confs = [
                        getattr(i, "confidence", 1.0)
                        for i in items
                        if getattr(i, "confidence", None) is not None
                    ]
                    self.confidence = sum(confs) / len(confs) if confs else 0.8

    try:
        t_client = TranscribeStreamingClient(region="us-east-1")

        async def _audio_gen():
            chunk = 3200  # 100 ms of 16 kHz int16
            for i in range(0, len(audio_bytes), chunk):
                yield audio_bytes[i: i + chunk]

        stream = await t_client.start_stream_transcription(
            language_code="en-US",
            media_sample_rate_hz=sample_rate,
            media_encoding="pcm",
        )

        handler = _ResultHandler(stream.output_stream)
        await asyncio.wait_for(
            asyncio.gather(stream.input_stream.send_audio_event(audio_chunk=audio_bytes),
                           stream.input_stream.end_stream(),
                           handler.handle_events()),
            timeout=3.0,
        )

        if handler.transcript and handler.transcript.lower() != cmd.text.lower():
            log.info(
                "Gate 1 Transcribe: %r → %r (conf=%.2f)",
                cmd.text, handler.transcript, handler.confidence,
            )
            from dataclasses import replace as dc_replace
            logprob_equiv = -max(0.0, 1.0 - handler.confidence)
            cmd = _dc_replace(cmd, text=handler.transcript, whisper_logprob=logprob_equiv)

    except asyncio.TimeoutError:
        log.warning("Gate 1 Transcribe: timed out after 3s — using vocab-corrected text")
    except Exception as exc:
        log.warning("Gate 1 Transcribe: error — %s (using vocab-corrected text)", exc)

    return cmd




# ---------------------------------------------------------------------------
# HybridCoordinator
# ---------------------------------------------------------------------------

_BYPASS_SOURCES = {"touch", "sound_action", "gaze_dwell", "multimodal"}
_SKIP_GATE1_SOURCES = {"voice_local"}


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


class HybridCoordinator:
    # Class-level DomainClassifier — stateless keyword scorer, no need to
    # re-instantiate per route() call.
    _domain_classifier = None

    @classmethod
    def _get_domain_classifier(cls):
        if cls._domain_classifier is None:
            from domain_classifier import DomainClassifier
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
        self._cloud = _CloudInference(self._cfg.bedrock_model_id, self._cfg.bedrock_region)
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

    # ---------------------------------------------------------------------- #
    # Public entry point
    # ---------------------------------------------------------------------- #

    def set_dev_agent(self, dev_agent: "DevAgent") -> None:
        self._dev_agent = dev_agent

    def set_whisper_stream(self, whisper_stream) -> None:
        self._whisper = whisper_stream

    def set_profiler(self, profiler) -> None:
        self._profiler = profiler

    def set_calibrator(self, calibrator) -> None:
        self._calibrator = calibrator

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

    async def route(self, cmd: Command) -> dict:
        """Route a Command through the gate decision tree and execute it.

        Dev-domain queries (code, math, vision, plan, general) are intercepted
        here and forwarded to DevAgent before the accessibility pipeline runs.
        """
        # --- Dev-agent pre-gate: intercept non-command domains ---
        if self._dev_agent:
            domain = self._get_domain_classifier().classify(cmd.text)
            if domain != "command":
                log.info("HybridCoordinator: dev-domain=%s → DevAgent", domain)
                agent_result = await self._dev_agent.handle(cmd.text)
                return {
                    "status": "ok",
                    "action": "dev_agent",
                    "domain": agent_result.domain,
                    "model": agent_result.model_used,
                    "response": agent_result.response_text,
                    "steps": len(agent_result.steps),
                }

        # System control commands — intercept before gate evaluation
        if cmd.source in ("voice", "voice_local"):
            _lower = cmd.text.lower().strip()

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
                # Immediately loosen VAD for softer voice
                if self._whisper and hasattr(self._whisper, '_profiler') \
                        and self._whisper._profiler:
                    vad = self._whisper._profiler.get_vad_threshold(pain_day=True)
                    self._whisper._silence_thresh = vad
                    log.info("Pain day ON — VAD relaxed to %.3f", vad)
                return {"status": "ok", "action": "PAIN_DAY", "enabled": True}
            elif _lower in ("pain day off", "flare day off", "feeling better"):
                if self._twin:
                    self._twin.set_manual_pain_day(False)
                if self._whisper and hasattr(self._whisper, '_profiler') \
                        and self._whisper._profiler:
                    vad = self._whisper._profiler.get_vad_threshold(pain_day=False)
                    self._whisper._silence_thresh = vad
                    log.info("Pain day OFF — VAD restored to %.3f", vad)
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
                asyncio.create_task(self._switch_condition(condition))
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
                asyncio.create_task(self._run_calibration(condition, quick))
                return {"status": "ok", "action": "CALIBRATION_START",
                        "condition": condition, "quick": quick}

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
            from behavioral_twin_state import _DEFAULT_SNAPSHOT
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

            # Propagate pain-day state to FusionEngine sensor thresholds
            if self._fusion is not None:
                self._fusion.apply_pain_day(snapshot.pain_day_active)

            # Always apply vocabulary corrections before any gate evaluation
            # so app-name phonetics ("key-row" → "kiro") reach the LLM fixed
            # regardless of Whisper confidence level.
            if cmd.source in ("voice", "voice_local"):
                corrected_text, changed = _apply_vocabulary_corrections(cmd.text)
                if changed:
                    log.debug(
                        "Pre-gate vocab correction: %r → %r", cmd.text, corrected_text
                    )
                    cmd = _dc_replace(cmd, text=corrected_text)

            # Populate session_context from twin state
            if self._twin and self._twin.is_ready:
                cmd = _dc_replace(cmd, session_context=self._twin.get_session_context())

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
                # --- Gate 0 — Privacy (applies before bypass; forces local) ----
                if not self._gate0(cmd):
                    log.debug("Gate 0 force-local (sensitive data): %r", cmd.text)
                    action_str = await self._run_local(cmd)
                    route_label = "local"
                    gate_that_decided = "gate0_privacy"

                # --- Bypass path -----------------------------------------------
                elif source in _BYPASS_SOURCES:
                    action_str = await self._run_local(cmd)
                    route_label = "local"
                    gate_that_decided = "bypass"

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
                        # Voice low confidence — re-transcribe and continue to Gate 2
                        cmd = await _retranscribe(cmd)

                    action_str, gate_that_decided, route_label = await self._gates_2_to_4(cmd)
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
                    )
                except Exception as db_exc:
                    log.warning("AgentDB.insert_command failed: %s", db_exc)

            # Record successful local executions for few-shot learning
            if (self._trainer and route_label == "local" and success):
                await self._trainer.record_success(
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
            self._update_ema(latency_ms)

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
        action_str = await self._local.infer(cmd, few_shot_examples=examples)
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
        return action_str

    async def _run_cloud(self, cmd: Command) -> str:
        """Route to raw Bedrock Claude Haiku.

        Content filter scrubs secrets/PII from the command text before
        transmitting to external APIs. Findings are logged to the audit trail.
        """
        # Scrub secrets before sending to external API
        if self._content_filter:
            clean_text, findings = await self._content_filter.scrub(cmd.text)
            if findings:
                log.info("ContentFilter: redacted %d secret(s) before cloud call", len(findings))
                cmd = Command(
                    text=clean_text,
                    whisper_logprob=cmd.whisper_logprob,
                    gesture_confidence=cmd.gesture_confidence,
                    source=cmd.source,
                    session_context=cmd.session_context,
                    _gaze_coords=cmd.gaze_coords,
                )

        return await self._cloud.infer(cmd)

    # ---------------------------------------------------------------------- #
    # Action execution
    # ---------------------------------------------------------------------- #

    async def _execute_action(
        self, action_str: str, cmd: Command, route_label: str = "local"
    ) -> dict:
        """Parse the LLM's action string and execute it via CommandExecutor."""
        if not action_str:
            return {"status": "error", "error": "empty action string"}

        parts = action_str.strip().split(None, 1)
        verb = parts[0].upper()
        target = parts[1] if len(parts) > 1 else ""

        params = self._parse_params(verb, target, cmd)
        # CLARIFY from a cloud route should speak via Polly TTS
        if route_label == "cloud":
            params["route"] = "cloud"

        # Vision grounding: resolve named CLICK targets to pixel coords.
        # Only runs when there's a named target and no coords already supplied
        # (gaze_coords or explicit x/y from touch). Falls through silently on
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
            if original.gaze_coords:
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
