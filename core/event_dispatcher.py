import logging
import time
from typing import Optional
from dataclasses import replace as _dc_replace
from monitoring.trace import get_tracer
from core.command_executor import Command
from core.domain_classifier import deglue_command_verb
from core.voice_system_control import _is_system_control_voice
from core.gate_evaluator import _EFFECTIVE_CFG
from core.inference_runner import _PENDING_INFERENCE_IDS
from inference.local_inference import get_inference_capture, set_inference_capture
from storage.personal_kb import is_personal_query as _is_personal_query


log = logging.getLogger(__name__)

from core.routing_constants import _BYPASS_SOURCES

class EventDispatcher:
    """Handles routing Commands through the gate decision tree and dev agents."""
    
    def __init__(self, coordinator):
        self._coordinator = coordinator

    async def route_impl(self, cmd: Command) -> dict:
        """Route a Command through the gate decision tree and execute it."""
        _tracer = get_tracer()
        if _tracer.enabled:
            _tid = cmd.trace_id or _tracer.new_trace(source=cmd.source)
            if not cmd.trace_id:
                cmd = _dc_replace(cmd, trace_id=_tid)
            _tracer.set_current(_tid)

        if cmd.source not in _BYPASS_SOURCES:
            deglued = deglue_command_verb(cmd.text)
            if deglued != cmd.text:
                log.info("HybridCoordinator: de-glued command verb %r -> %r",
                         cmd.text, deglued)
                cmd = _dc_replace(cmd, text=deglued)

        if self._coordinator._macro_store is not None and cmd.source in ("voice", "voice_local"):
            macro_result = await self._coordinator._workflow.maybe_handle_macro(cmd)
            if macro_result is not None:
                return macro_result

        if cmd.source in ("voice", "voice_local"):
            wf_result = await self._coordinator._workflow.maybe_handle_workflow(cmd)
            if wf_result is not None:
                return wf_result

        if self._coordinator._conv_mode.enabled and cmd.source in ("voice", "voice_local"):
            conv_result = await self._coordinator._workflow.maybe_handle_conversation(cmd)
            if conv_result is not None:
                return conv_result

        if (
            self._coordinator._dev_agent
            and not _is_system_control_voice(cmd)
            and cmd.source not in _BYPASS_SOURCES
        ):
            domain = self._coordinator._get_domain_classifier().classify(cmd.text)
            if domain != "command":
                # GAP-6: anchor/track session intent for dev-domain commands.
                self._coordinator._correction_handler.note_intent_drift(cmd)
                # Privacy: the dev pre-gate runs BEFORE the command-path Gate 0,
                # so enforce Gate 0 here too — sensitive text (credentials/PII)
                # must never reach the cloud. A Gate-0 failure forces the LOCAL
                # DevAgent regardless of the cloud-routing preference.
                route_cloud = self._coordinator._should_route_cloud_dev()
                if domain == "skill":
                    # Skills execute LOCALLY via the SkillRegistry (the cloud dev
                    # agent has no registry) — never route a skill to the cloud.
                    route_cloud = False
                if _is_personal_query(cmd.text):
                    # Questions about the user's OWN documents answer from the
                    # local PersonalKB — the query itself must never go to cloud.
                    route_cloud = False
                if route_cloud and not self._coordinator._gates.gate0(cmd):
                    log.info("HybridCoordinator: dev-domain=%s contains sensitive "
                             "data — forcing LOCAL DevAgent (Gate 0)", domain)
                    route_cloud = False

                # Cloud DevAgent branch — route to Claude when configured to,
                # avoiding a 30B specialist wake (and the GPU teardown it forces).
                if route_cloud:
                    # Scrub secrets/PII from the query AND the recent-command
                    # context before any cloud egress (the command-path cloud
                    # route scrubs in _run_cloud; this path bypasses it).
                    clean_text = cmd.text
                    recent = list(self._coordinator.state.recent_dev_commands)
                    if self._coordinator._content_filter:
                        clean_text, findings = await self._coordinator._content_filter.scrub(cmd.text)
                        if findings:
                            log.info("ContentFilter: redacted %d secret(s) before "
                                     "cloud dev call", len(findings))
                        recent = [
                            (await self._coordinator._content_filter.scrub(rc))[0] for rc in recent
                        ]
                    log.info("HybridCoordinator: dev-domain=%s → CloudDevAgent (%s)",
                             domain, getattr(self._coordinator._cloud_dev_agent, "model", "?"))
                    ctx = {
                        "session_id": self._coordinator._session_id,
                        "recent_commands": recent,
                        "source": cmd.source,
                        "trace_id": cmd.trace_id,
                    }
                    self._coordinator._gates.note_cloud_call()  # GAP-10: denial-of-wallet tripwire
                    # Clear the task-local capture so a stale prompt/token count
                    # from a prior inference in this task can't be attributed to
                    # this cloud-dev call (mirrors _run_cloud).
                    set_inference_capture(None)
                    response_text = await self._coordinator._cloud_dev_agent.run(clean_text, domain, ctx)
                    # Persist the Opus-tier token usage for the cost ledger. This
                    # is the most expensive cloud path; CloudDevAgent.run() set the
                    # capture from the Bedrock response usage.
                    db = self._coordinator._agent_db
                    if db and db.available:
                        _p, _ti, _to = get_inference_capture()
                        _cda_model = getattr(self._coordinator._cloud_dev_agent, "model", "unknown")
                        await db.inferences.insert_inference(
                            command_id=None,
                            model=_cda_model,
                            domain=domain,
                            prompt=None,
                            response=None,
                            tokens_in=_ti,
                            tokens_out=_to,
                            latency_ms=0.0,
                            backend="bedrock",
                            error=None,
                        )
                    # Record the ORIGINAL locally (kept on-device; re-scrubbed at
                    # the next cloud egress).
                    self._coordinator._record_dev_command(cmd.text)
                    if self._coordinator._twin:
                        self._coordinator._twin.clear_dev_namespace()
                    return {
                        "status": "ok",
                        "action": "dev_agent",
                        "domain": domain,
                        "model": self._coordinator._cloud_dev_agent.model,
                        "response": response_text,
                        "steps": 0,
                        "backend": "bedrock",
                    }

                log.info("HybridCoordinator: dev-domain=%s → DevAgent", domain)
                # Chat file attachments (specs/chat-context-attachments R2.4): the
                # chat server stuffs extracted context + an optional image into
                # cmd.params. Forward both; absent → byte-identical to today.
                _att_ctx = cmd.params.get("attachment_context", "") if cmd.params else ""
                _att_img = cmd.params.get("attachment_image_b64") if cmd.params else None
                agent_result = await self._coordinator._dev_agent.handle(
                    cmd.text, screenshot_b64=_att_img, trace_id=cmd.trace_id,
                    attachment_context=_att_ctx)
                # Personal-document queries are NOT recorded in the rolling dev
                # context: _recent_dev_commands is sent verbatim to the cloud
                # dev agent on later queries, which would leak the very text the
                # force-local guard kept on-device.
                if not _is_personal_query(cmd.text):
                    self._coordinator._record_dev_command(cmd.text)
                if self._coordinator._twin:
                    self._coordinator._twin.clear_dev_namespace()
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
        vsc_result = await self._coordinator._voice_control.maybe_handle(cmd)
        if vsc_result is not None:
            return vsc_result

        t0 = time.monotonic()
        route_label = "local"
        gate_that_decided = "all_pass"
        action_str: Optional[str] = None
        success: bool = False  # default False so a pre-action exception never sends None to metrics (#11)
        error_msg: Optional[str] = None
        command_id: int = -1
        result = {"status": "failed", "reason": "unknown"}
        # Per-command accumulator for inference row ids written by
        # _run_local/_run_cloud BEFORE the command row exists. Task-local
        # (ContextVar) so concurrent route() tasks never share a list. The ids
        # are backfilled onto inferences.command_id right after insert_command.
        _inf_ids_token = _PENDING_INFERENCE_IDS.set([])

        try:
            source = cmd.source
            db = self._coordinator._agent_db

            # --- Twin state snapshot and adjustments -----------------------
            from adaptive.behavioral_twin_state import _DEFAULT_SNAPSHOT
            snapshot = _DEFAULT_SNAPSHOT
            if self._coordinator._twin:
                try:
                    snapshot = await self._coordinator._twin.get_snapshot()
                except Exception as exc:
                    log.warning("BehavioralTwinState.get_snapshot failed: %s", exc)

            # Apply pain-day threshold adjustments
            from core.hybrid_coordinator import (
                _apply_pain_day_adjustments,
                _apply_vocabulary_corrections,
            )
            if snapshot.pain_day_active:
                effective_cfg = _apply_pain_day_adjustments(self._coordinator._cfg, snapshot)
            else:
                effective_cfg = self._coordinator._cfg

            # Propagate pain-day state to sensor + voice thresholds. Each
            # consumer relaxes only if its flare_profile degrade flag is set,
            # and apply_pain_day is idempotent so calling every command is cheap.
            if self._coordinator._fusion is not None:
                self._coordinator._fusion.apply_pain_day(
                    tilt=snapshot.pain_day_active and snapshot.flare_tilt_degrades,
                )
            if self._coordinator._whisper is not None:
                self._coordinator._whisper.apply_pain_day(
                    snapshot.pain_day_active and snapshot.flare_voice_degrades
                )

            # Always apply vocabulary corrections before any gate evaluation
            # so app-name phonetics ("key-row" → "vscode") reach the LLM fixed
            # regardless of Whisper confidence level.
            vocab_corrected = False
            if cmd.source in ("voice", "voice_local"):
                corrected_text, changed = _apply_vocabulary_corrections(cmd.text)
                if changed:
                    log.debug("Pre-gate vocab correction: %r → %r", cmd.text, corrected_text)
                    cmd = _dc_replace(cmd, text=corrected_text)
                    vocab_corrected = True

            # Populate session_context from twin state (always accessibility namespace)
            if self._coordinator._twin and self._coordinator._twin.is_ready:
                cmd = _dc_replace(cmd, session_context=self._coordinator._twin.get_session_context("accessibility"))

            # Conversational continuity (voice only): deterministically resolve
            # anaphora ("do that again", "click it") against the previous resolved
            # turn BEFORE inference — the local 8B model is poor at this — and
            # append one structured last-action line so both the local and cloud
            # prompts see what actually happened, not just what was said.
            if cmd.source in ("voice", "voice_local", "voice_correction"):
                resolved_text, changed = self._coordinator._conversation.resolve_anaphora(cmd.text)
                if changed:
                    log.debug("Anaphora resolved: %r → %r", cmd.text, resolved_text)
                    cmd = _dc_replace(cmd, text=resolved_text)
            hint = self._coordinator._conversation.prompt_hint()
            if hint:
                cmd = _dc_replace(cmd, session_context=list(cmd.session_context or []) + [hint])

            # Inject pending clarification so the LLM knows what "up" or "yes"
            # is answering.  Prepended so it appears closest to the user turn.
            if self._coordinator.state.pending_clarification and cmd.source in ("voice", "voice_local"):
                clarify_ctx = f"[PENDING CLARIFICATION: {self._coordinator.state.pending_clarification}]"
                clarify_session_ctx = [clarify_ctx] + list(cmd.session_context or [])
                cmd = _dc_replace(cmd, session_context=clarify_session_ctx)

            # Gate evaluation — set effective_cfg in a task-local ContextVar so
            # concurrent route() tasks each see their own pain-day-adjusted config
            # without mutating the shared self._cfg (#4). Gate/inference helpers
            # read it via effective_cfg(), which falls back to their own self._cfg
            # when unset.
            _cfg_token = _EFFECTIVE_CFG.set(effective_cfg)
            try:
                eval_res = await self._coordinator._gates.evaluate(cmd, vocab_corrected=vocab_corrected)
            finally:
                _EFFECTIVE_CFG.reset(_cfg_token)
            action_str = eval_res.action
            gate_that_decided = eval_res.gate
            route_label = eval_res.route
            cmd = eval_res.new_cmd or cmd
            if action_str == "DISCARDED":
                # E10: record the drop so it is visible to retraining and
                # analytics instead of vanishing with only a debug line.
                if db and db.available:
                    try:
                        await db.commands.insert_command(
                            session_id=self._coordinator._session_id,
                            cmd=cmd,
                            action="DISCARDED",
                            route=route_label,
                            gate_that_decided=gate_that_decided,
                            latency_ms=(time.monotonic() - t0) * 1000,
                            success=False,
                            error_msg=eval_res.discard_reason,
                            trace_id=cmd.trace_id or None,
                        )
                    except Exception as _disc_exc:
                        log.debug("discard log failed: %s", _disc_exc)
                return {"status": "discarded", "reason": gate_that_decided}

            # --- Execute the action ----------------------------------------
            result = await self._coordinator._action_executor.execute_action(action_str, cmd, route_label=route_label)
            success = result.get("status") == "ok"

            # E17: record a concrete failure reason so root-cause analysis does
            # not have to reverse-engineer it from the action column. CLARIFY
            # outcomes carry their reason text; verify_failed/resolve_miss/error
            # carry the status (or executor error).
            if not success and error_msg is None:
                if isinstance(action_str, str) and action_str.upper().startswith("CLARIFY"):
                    error_msg = action_str[len("CLARIFY"):].strip()[:200] or "clarify"
                else:
                    error_msg = result.get("error") or result.get("status")

            # Persist to DB now so command_id is valid before trainer uses it
            latency_ms = (time.monotonic() - t0) * 1000
            if db and db.available:
                try:
                    resolved_by_val = result.get("result", {}).get("resolved_by") if isinstance(result.get("result"), dict) else None
                    command_id = await db.commands.insert_command(
                        session_id=self._coordinator._session_id,
                        cmd=cmd,
                        action=action_str,
                        route=route_label,
                        gate_that_decided=gate_that_decided,
                        latency_ms=latency_ms,
                        success=success,
                        error_msg=error_msg,
                        trace_id=cmd.trace_id or None,
                        resolved_by=resolved_by_val,
                    )
                except Exception as db_exc:
                    log.warning("AgentDB.insert_command failed: %s", db_exc)
                else:
                    # Backfill inferences.command_id now that the command row
                    # exists — without this the fine-tuning extraction JOIN
                    # (inferences ⨝ commands) matches nothing.
                    _inf_ids = _PENDING_INFERENCE_IDS.get()
                    if _inf_ids and command_id and command_id > 0:
                        try:
                            await db.commands.link_inferences_to_command(
                                _inf_ids, command_id
                            )
                        except Exception as link_exc:
                            log.debug("link_inferences_to_command failed: %s", link_exc)

            # Publish gate decision and command outcome events (fail-safe).
            if self._coordinator._event_bus is not None:
                try:
                    from core.events import TOPIC_COMMAND_EXECUTED, TOPIC_GATE_DECIDED
                    _ev_payload_gate = {
                        "gate": gate_that_decided, "route": route_label,
                        "domain": getattr(cmd, "domain", None),
                        "latency_ms": round(latency_ms, 1),
                    }
                    _ev_payload_cmd = {
                        "action": action_str, "route": route_label,
                        "gate": gate_that_decided,
                        "latency_ms": round(latency_ms, 1),
                        "success": success,
                    }
                    from core.async_utils import fire_and_log
                    fire_and_log(self._coordinator._event_bus.publish(
                        TOPIC_GATE_DECIDED, _ev_payload_gate, source="coordinator",
                        session_id=self._coordinator._session_id, command_id=command_id,
                        trace_id=cmd.trace_id or None,
                    ))
                    fire_and_log(self._coordinator._event_bus.publish(
                        TOPIC_COMMAND_EXECUTED, _ev_payload_cmd, source="coordinator",
                        session_id=self._coordinator._session_id, command_id=command_id,
                        trace_id=cmd.trace_id or None,
                    ))
                    # Update bridge's active trace_id so ipad_log entries
                    # arriving within the 2 s window are correlated to this command.
                    if self._coordinator._bridge is not None and cmd.trace_id:
                        self._coordinator._bridge.set_active_trace_id(cmd.trace_id)
                except Exception as _ev_exc:
                    log.debug("HybridCoordinator: event publish failed: %s", _ev_exc)

            # Record successful local executions for few-shot learning
            if (self._coordinator._trainer and route_label == "local" and success):
                await self._coordinator._trainer.record_success(
                    cmd, action_str, command_id=command_id
                )
            # Feed failed local executions into the twin's pain-day fail signal
            # and the counterexample store (guarded — see trainer.record_failure;
            # never the positive few-shot store). CLARIFY executes with status
            # "ok" so it is a success here, not a failure. Cloud outcomes stay
            # out of the twin, matching the local-only success gate above.
            elif (self._coordinator._trainer and route_label == "local" and not success):
                await self._coordinator._trainer.record_failure(
                    cmd, action_str, command_id=command_id
                )

            # D3: drain gesture velocity samples after every gesture command
            # that cleared Gate 1, regardless of execution outcome.
            if self._coordinator._trainer and cmd.source == "gesture":
                await self._coordinator._trainer.drain_and_persist_velocity(
                    pain_day=snapshot.pain_day_active
                )

            # D8: handle voice corrections — record the right action for
            # commands where the user said "no/wait/actually <new command>"
            if cmd.source == "voice_correction":
                await self._coordinator._on_correction(cmd, action_str)

            # D8: record action/status so WhisperStream can detect next correction
            self._coordinator.state.last_executed_action = action_str or ""
            self._coordinator.state.last_command_id = command_id
            if self._coordinator._whisper:
                status_str = "ok" if success else ("CLARIFY" if action_str == "CLARIFY" else "failed")
                self._coordinator._whisper.set_last_command_status(status_str, cmd.text)

            # Advance acoustic profiler command counter (seasonal drift check)
            if self._coordinator._whisper and hasattr(self._coordinator._whisper, "_profiler") \
                    and self._coordinator._whisper._profiler:
                self._coordinator._whisper._profiler.on_any_command()

        except Exception as exc:
            log.error("HybridCoordinator.route error: %s", exc)
            error_msg = str(exc)
            return {"status": "error", "error": str(exc)}

        finally:
            _PENDING_INFERENCE_IDS.reset(_inf_ids_token)
            latency_ms = (time.monotonic() - t0) * 1000
            # Gate 4's EMA exists to detect when LOCAL inference is getting slow
            # (e.g. GPU contention during a flare) and shed load to the cloud.
            # Feeding cloud round-trip latency — inherently several times the
            # local budget — back into it would inflate the EMA, trip Gate 4, and
            # push even more commands to the cloud: a positive feedback loop that
            # never recovers. So only local routes update the Gate 4 EMA; when a
            # burst goes to cloud the EMA stays low and local is retried promptly.
            if route_label == "local":
                self._coordinator._gates.update_ema(latency_ms)
            # Record outcome in metrics singleton (non-fatal)
            if self._coordinator._metrics is not None:
                try:
                    self._coordinator._metrics.record_command_outcome(
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
