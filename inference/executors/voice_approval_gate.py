import asyncio
import logging
import time
from pathlib import Path
from typing import Optional
from core.approval_keywords import classify_confirmation
from core.events import TOPIC_DAG_APPROVAL

log = logging.getLogger(__name__)

async def confirm_destructive_op_locked(agent, description: str, card: Optional[dict] = None) -> bool:
    import numpy as np

    log.info("DevAgent: confirmation required — %s", description)

    chat_live = agent._event_bus is not None and bool(agent._active_trace_id)
    if chat_live:
        payload = {"message": description, "destructive": True}
        if card:
            payload.update({k: v for k, v in card.items() if v})
        await agent._publish_live(TOPIC_DAG_APPROVAL, payload)
    
    # StepExecutor proxy works for `_chat_confirm_window_s`
    chat_window_s = getattr(agent, "_chat_confirm_window_s", 7.0)

    # --- 1. Speak via TTS ------------------------------------------------
    tts_ok = True
    try:
        from tts.polly_stream import get_client as _get_tts
        _tts = _get_tts()
        await asyncio.to_thread(_tts.speak_sync, description)
    except Exception as exc:
        if not chat_live:
            log.info("DevAgent._confirm: TTS unavailable (%s) — DENY (fail-safe)", exc)
            return False
        log.info("DevAgent._confirm: TTS unavailable (%s) — chat card only", exc)
        tts_ok = False

    # --- 1b. Chat/signal-file window --------------------------------------
    if chat_live:
        _appr_dir = Path.home() / ".claude" / "approval"
        _pending = _appr_dir / "pending"
        _response = _appr_dir / "response"
        try:
            _appr_dir.mkdir(parents=True, exist_ok=True)
            _response.unlink(missing_ok=True)
            _pending.write_text(str(time.monotonic()), encoding="utf-8")
            deadline = time.monotonic() + chat_window_s
            transcript: Optional[str] = None
            while time.monotonic() < deadline:
                if _response.exists():
                    transcript = _response.read_text(encoding="utf-8-sig").strip()
                    break
                await asyncio.sleep(0.1)
        except OSError as exc:
            log.debug("DevAgent._confirm: signal-file window failed: %s", exc)
            transcript = None
        finally:
            _pending.unlink(missing_ok=True)
            _response.unlink(missing_ok=True)
            
        if transcript is not None:
            verdict = classify_confirmation(transcript)
            if verdict == "deny":
                log.info("DevAgent._confirm: REJECTED via signal file — %r", transcript)
                return False
            if verdict == "approve":
                log.info("DevAgent._confirm: approved via signal file — %r", transcript)
                return True
        if not tts_ok:
            log.info("DevAgent._confirm: no TTS and no chat answer → DENY (fail-safe)")
            return False

    # --- 2. Record 4 s of mic audio --------------------------------------
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
        log.info("DevAgent._confirm: mic unavailable (%s) — DENY (fail-safe)", exc)
        return False

    # --- 3. Check for voice activity; silence → DENY (fail-safe) ---------
    rms = float(np.sqrt(np.mean(audio ** 2)))
    if rms < 0.005:
        log.info("DevAgent._confirm: silence → DENY (fail-safe)")
        return False

    # --- 4. Transcribe with tiny Whisper on CPU --------------------------
    try:
        if getattr(agent, "_confirm_whisper", None) is None:
            from faster_whisper import WhisperModel
            import typing
            agent._confirm_whisper = typing.cast(typing.Any, await asyncio.to_thread(
                WhisperModel, "tiny", device="cpu", compute_type="int8"
            ))
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
        log.info("DevAgent._confirm: transcription failed (%s) — DENY (fail-safe)", exc)
        return False

    # --- 5. Keyword detection --------------------------------------------
    verdict = classify_confirmation(text)
    if verdict == "deny":
        log.info("DevAgent._confirm: REJECTED — %r", text)
        return False
    if verdict == "approve":
        log.info("DevAgent._confirm: approved — %r", text)
        return True

    log.info("DevAgent._confirm: ambiguous response %r → DENY (fail-safe)", text)
    return False
