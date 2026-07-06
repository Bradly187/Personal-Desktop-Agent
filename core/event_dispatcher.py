import logging
import time
from typing import Optional
from dataclasses import replace as _dc_replace
from monitoring.trace import get_tracer
from core.command_executor import Command
from core.domain_classifier import deglue_command_verb
from core.voice_system_control import _is_system_control_voice
from storage.personal_kb import is_personal_query as _is_personal_query


log = logging.getLogger(__name__)

# Bypasses: UI gestures/clicks that already carry a concrete target action.
# 'multi' is a multimodal prompt where the user clicks an element and types text;
# the click resolves to a target_uid on the client before reaching here.
_BYPASS_SOURCES = ("touch", "multi")

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

        if (self._coordinator._workflow_runner is not None and self._coordinator._workflow_runner.enabled
                and cmd.source in ("voice", "voice_local")):
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
                self._coordinator._correction_handler.note_intent_drift(cmd)
                
                clean_text = cmd.text
                recent = list(self._coordinator.state.recent_dev_commands)
                if self._coordinator._content_filter:
                    clean_text, findings = await self._coordinator._content_filter.scrub(cmd.text)
                    if findings:
                        db = self._coordinator._agent_db
                        if db and getattr(db, "available", False):
                            from core.async_utils import fire_and_log
                            fire_and_log(
                                db.logs.insert_scrub_findings(
                                    self._coordinator._session_id, cmd.trace_id, findings, cmd.text),
                                log, label="insert scrub")
                        if self._coordinator._local.gate_check_failed(findings, domain):
                            return {"status": "failed", "reason": "content_filter_local_reject"}
                        clean_text = None

                if clean_text:
                    if self._coordinator._local_specialist_available and self._coordinator._local_specialist_available():
                        ans = await self._coordinator._local.route_to_specialist(clean_text, domain)
                        if ans:
                            if self._coordinator._tts_speak:
                                from core.async_utils import fire_and_log
                                fire_and_log(self._coordinator._tts_speak(ans), log, label="specialist TTS")
                            return {"status": "ok", "action": "SPEAK", "result": ans}

                    _att_img, _att_ctx = None, None
                    if hasattr(cmd, "attachment"):
                        _att_img = getattr(cmd.attachment, "image_b64", None)
                        _att_ctx = getattr(cmd.attachment, "ui_context", None)

                    if self._coordinator._cloud_always or _att_img or _att_ctx:
                        if self._coordinator._cloud_dev_agent:
                            try:
                                ans = await self._coordinator._cloud_dev_agent.query(
                                    cmd.text, screenshot_b64=_att_img, trace_id=cmd.trace_id,
                                    attachment_context=_att_ctx)
                                if not _is_personal_query(cmd.text):
                                    self._coordinator._record_dev_command(cmd.text)
                                if self._coordinator._tts_speak:
                                    from core.async_utils import fire_and_log
                                    fire_and_log(self._coordinator._tts_speak(ans), log, label="cloud TTS")
                                return {"status": "ok", "action": "SPEAK", "result": ans}
                            except DevAgentException as e:
                                if _att_img and "payload too large" in str(e).lower():
                                    log.warning("DevAgent: image payload too large, falling back to local text.")
                                    if self._coordinator._tts_speak:
                                        from core.async_utils import fire_and_log
                                        fire_and_log(self._coordinator._tts_speak(_ATT_ERROR_MSG), log, label="size err TTS")
                                    return {"status": "failed", "reason": "payload_too_large"}
                                else:
                                    log.error("CloudDevAgent failed: %s", e)
                            except Exception as e:
                                log.error("CloudDevAgent unhandled err: %s", e)

                    ans = await self._coordinator._dev_agent.query(clean_text, recent)
                    self._coordinator._record_dev_command(cmd.text)
                    if self._coordinator._tts_speak:
                        from core.async_utils import fire_and_log
                        fire_and_log(self._coordinator._tts_speak(ans), log, label="dev TTS")
                    return {"status": "ok", "action": "SPEAK", "result": ans}
                else:
                    log.warning("HybridCoordinator: Dev query blocked by content filter.")
                    return {"status": "failed", "reason": "content_filter_reject"}

        t0 = time.monotonic()
        action_str = None
        success = False
        command_id = -1
        route_label = "unknown"
        gate_that_decided = "unknown"
        result = {"status": "failed", "reason": "unknown"}

        try:
            db = self._coordinator._agent_db
            if db and db.available:
                try:
                    command_id = await db.commands.insert_command(
                        self._coordinator._session_id, cmd.trace_id, cmd.text, cmd.source)
                except Exception as exc:
                    log.error("Failed to insert command: %s", exc)

            # --- Twin state snapshot and adjustments -----------------------
            from adaptive.behavioral_twin_state import _DEFAULT_SNAPSHOT
            snapshot = _DEFAULT_SNAPSHOT
            if hasattr(self._coordinator, "_twin") and self._coordinator._twin:
                try:
                    snapshot = await self._coordinator._twin.get_snapshot()
                except Exception as exc:
                    log.warning("BehavioralTwinState.get_snapshot failed: %s", exc)

            context = list(cmd.session_context or [])
            if snapshot.summary_text:
                context.insert(0, snapshot.summary_text)
            
            # (We skip the config EMA adjustments here since they are handled in GateEvaluator or elsewhere now, 
            # but we preserve the context injection for the intent model)
            if context:
                cmd = _dc_replace(cmd, session_context=context)

                    
            vocab_corrected = False
            if cmd.source in ("voice", "voice_local"):
                from core.hybrid_coordinator import _apply_vocabulary_corrections
                corrected_text, changed = _apply_vocabulary_corrections(cmd.text)
                if changed:
                    log.debug("Pre-gate vocab correction: %r → %r", cmd.text, corrected_text)
                    cmd = _dc_replace(cmd, text=corrected_text)
                    vocab_corrected = True
                    
            if cmd.source in ("voice", "voice_local", "voice_correction"):
                resolved_text, changed = self._coordinator._conversation.resolve_anaphora(cmd.text)
                if changed:
                    log.debug("Anaphora resolved: %r → %r", cmd.text, resolved_text)
                    cmd = _dc_replace(cmd, text=resolved_text)
            hint = self._coordinator._conversation.prompt_hint()
            if hint:
                cmd = _dc_replace(cmd, session_context=list(cmd.session_context or []) + [hint])
                
            if self._coordinator.state.pending_clarification and cmd.source in ("voice", "voice_local"):
                clarify_ctx = f"[PENDING CLARIFICATION: {self._coordinator.state.pending_clarification}]"
                ctx = [clarify_ctx] + list(cmd.session_context or [])
                cmd = _dc_replace(cmd, session_context=ctx)
                
            eval_res = await self._coordinator._gates.evaluate(cmd, vocab_corrected=vocab_corrected)
            action_str = eval_res.action
            gate_that_decided = eval_res.gate
            route_label = eval_res.route
            cmd = eval_res.new_cmd or cmd
            if action_str == "DISCARDED":
                if self._coordinator._agent_db and self._coordinator._agent_db.available:
                    try:
                        await self._coordinator._agent_db.insert_command(
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
                    except Exception as exc:
                        log.debug("discard log failed: %s", exc)
                return {"status": "discarded", "reason": gate_that_decided}
                
            if self._coordinator._metrics:
                self._coordinator._metrics.record_gate_decision(gate_that_decided)

            if action_str == "CLARIFY":
                prompt_text = "What would you like to open?"
                self._coordinator.state.pending_clarification = "open_target"
                result = {"status": "ok", "action": "CLARIFY", "prompt": prompt_text}
                success = True
                
                if self._coordinator._bridge and self._coordinator._bridge.available:
                    from core.async_utils import fire_and_log
                    from api.messages import ClarifyRequestMessage
                    fire_and_log(
                        self._coordinator._bridge.broadcast(ClarifyRequestMessage(
                            prompt=prompt_text,
                            options=self._coordinator.state.recent_open_targets[:5],
                            command_id=command_id,
                        )),
                        log, label="clarify broadcast"
                    )

            elif action_str == "LOCAL_LLM":
                result = await self._coordinator._run_local(cmd)
                success = result.get("status") == "ok"
            elif action_str and hasattr(self._coordinator._action_executor, f"do_{action_str.lower()}"):
                method = getattr(self._coordinator._action_executor, f"do_{action_str.lower()}")
                result = await method(cmd)
                success = result.get("status") == "ok"
            else:
                result = await self._coordinator._action_executor.execute_action(action_str, cmd, route_label=route_label)
                success = result.get("status") == "ok"
        except Exception as exc:
            log.exception("HybridCoordinator: _route_impl crash: %s", exc)
            result = {"status": "error", "error": str(exc)}
            success = False
            route_label = "crash"
            gate_that_decided = "exception"
        finally:
            latency_ms = (time.monotonic() - t0) * 1000
            self._coordinator._gates.update_ema(latency_ms)

            if self._coordinator._metrics:
                self._coordinator._metrics.record_latency(latency_ms)
                if not success:
                    self._coordinator._metrics.record_error("route_failed")

            if command_id != -1 and db and db.available:
                from core.async_utils import fire_and_log
                fire_and_log(
                    db.commands.mark_command_executed(
                        command_id, action_str, success, latency_ms, route_label),
                    log, label="mark command executed"
                )

                if action_str == "CLARIFY":
                    await self._coordinator._on_correction(cmd, action_str)

            self._coordinator.state.last_executed_action = action_str or ""
            self._coordinator.state.last_command_id = command_id
            if self._coordinator._whisper:
                status_str = "ok" if success else ("CLARIFY" if action_str == "CLARIFY" else "failed")
                self._coordinator._whisper.set_last_command_status(status_str, cmd.text)

            if _tracer.enabled:
                try:
                    _tracer.record_span(
                        "route_decision", route=route_label, gate=gate_that_decided,
                        action=action_str or None, success=success,
                        dur_ms=round(latency_ms, 1),
                    )
                except Exception:
                    pass

        return result
