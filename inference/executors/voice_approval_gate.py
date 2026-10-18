import asyncio
import logging
import time
from typing import Optional

from core.approval_authority import ApprovalRequest, ApprovalResponse
from core.approval_keywords import classify_confirmation
from core.events import TOPIC_DAG_APPROVAL

log = logging.getLogger(__name__)


async def _publish_response(agent, request: ApprovalRequest, decision: str) -> None:
    """Publish one exact responder verdict; the authority remains the validator."""
    response = ApprovalResponse(
        request_id=request.request_id,
        action_digest=request.action_digest,
        decision="approve" if decision == "approve" else "deny",
        responded_at=time.time(),
    )
    try:
        await asyncio.to_thread(agent._approval_authority.write_response, response)
    except Exception as exc:
        # A second responder makes the request ambiguous inside ApprovalAuthority.
        log.info("DevAgent._confirm: response publication rejected (%s)", exc)


async def confirm_destructive_op_locked(
    agent,
    request: ApprovalRequest,
    description: str,
    card: Optional[dict] = None,
) -> None:
    """Collect a response for one exact operation without granting execution."""
    import numpy as np

    log.info("DevAgent: confirmation required — %s", description)

    chat_live = agent._event_bus is not None and bool(agent._active_trace_id)
    response_window_open = False
    if chat_live:
        try:
            await asyncio.to_thread(
                agent._approval_authority.open_response_window, request
            )
            response_window_open = True
        except Exception as exc:
            log.info("DevAgent._confirm: responder window unavailable (%s) — DENY", exc)
            return
        payload = {
            "message": description,
            "destructive": True,
            "request_id": request.request_id,
            "action_digest": request.action_digest,
        }
        if card:
            payload.update({k: v for k, v in card.items() if v})
        await agent._publish_live(TOPIC_DAG_APPROVAL, payload)

    chat_window_s = getattr(agent, "_chat_confirm_window_s", 7.0)

    # Speak before opening the streaming-mic responder window so the question's
    # own audio cannot become a response. Whisper adds a second echo guard.
    tts_ok = True
    try:
        from tts.polly_stream import get_client as _get_tts
        await asyncio.to_thread(_get_tts().speak_sync, description)
    except Exception as exc:
        if not chat_live:
            log.info("DevAgent._confirm: TTS unavailable (%s) — DENY", exc)
            await _publish_response(agent, request, "deny")
            return
        log.info("DevAgent._confirm: TTS unavailable (%s) — chat card only", exc)
        tts_ok = False

    # Chat, streamed voice, and iPad A2UI all publish the same exact response.
    if chat_live:
        opened = False
        response_window_sealed = False
        try:
            await asyncio.to_thread(
                agent._approval_authority.publish_pending_request, request
            )
            opened = True
            deadline = time.monotonic() + chat_window_s
            while time.monotonic() < deadline:
                if agent._approval_authority.response_path(request.request_id).exists():
                    return
                await asyncio.sleep(0.1)
        except OSError as exc:
            log.debug("DevAgent._confirm: structured responder window failed: %s", exc)
        finally:
            if response_window_open:
                try:
                    await asyncio.to_thread(
                        agent._approval_authority.close_response_window, request
                    )
                    response_window_sealed = True
                except Exception as exc:
                    log.info("DevAgent._confirm: could not seal responders (%s)", exc)
            if opened:
                await asyncio.to_thread(
                    agent._approval_authority.close_pending_request, request
                )
            await agent._publish_live("dag.approval_closed", {
                "request_id": request.request_id,
            })
        if agent._approval_authority.response_path(request.request_id).exists():
            return
        if not response_window_sealed:
            log.info("DevAgent._confirm: responder closure unverified — DENY")
            return
        if not tts_ok:
            log.info("DevAgent._confirm: no TTS and no exact answer → DENY")
            await _publish_response(agent, request, "deny")
            return

    # Direct PC microphone fallback is also born as a structured response.
    try:
        import sounddevice as sd
        audio = await asyncio.to_thread(
            lambda: sd.rec(
                int(4.0 * 16_000), samplerate=16_000,
                channels=1, dtype="float32",
            ).flatten()
        )
        await asyncio.to_thread(sd.wait)
    except Exception as exc:
        log.info("DevAgent._confirm: mic unavailable (%s) — DENY", exc)
        await _publish_response(agent, request, "deny")
        return

    rms = float(np.sqrt(np.mean(audio ** 2)))
    if rms < 0.005:
        log.info("DevAgent._confirm: silence → DENY")
        await _publish_response(agent, request, "deny")
        return

    try:
        if getattr(agent, "_confirm_whisper", None) is None:
            from faster_whisper import WhisperModel
            import typing
            agent._confirm_whisper = typing.cast(
                typing.Any,
                await asyncio.to_thread(
                    WhisperModel, "tiny", device="cpu", compute_type="int8"
                ),
            )
        import typing
        model: typing.Any = agent._confirm_whisper

        def _transcribe() -> str:
            segs, _ = model.transcribe(
                audio, language="en", beam_size=1, vad_filter=False
            )
            return " ".join(s.text for s in segs).lower().strip()

        text = await asyncio.to_thread(_transcribe)
        log.info("DevAgent._confirm: heard %r", text)
    except Exception as exc:
        log.info("DevAgent._confirm: transcription failed (%s) — DENY", exc)
        await _publish_response(agent, request, "deny")
        return

    verdict = classify_confirmation(text)
    if verdict is None:
        log.info("DevAgent._confirm: ambiguous response %r → DENY", text)
    await _publish_response(agent, request, verdict or "deny")
