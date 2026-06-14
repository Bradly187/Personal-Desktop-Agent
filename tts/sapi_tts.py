"""Windows SAPI TTS — fully local, zero-dependency speech.

Uses the built-in Windows Speech API (SAPI 5) via win32com (pywin32, already a
project dependency). No network, no GPU, no model download — it speaks through
the OS voices (Settings → Time & Language → Speech). The voice is more robotic
than Polly/Chatterbox, but it needs no install and works on any Windows box.

Mirrors the PollyStreamClient / ChatterboxClient interface so
polly_stream.get_client() can dispatch here when tts_backend == "sapi".

Thread-safety: speak_sync() creates the SpVoice inside the calling thread with
COM initialised, so it is safe to invoke from asyncio.to_thread (each worker
thread gets its own COM apartment). The client object itself holds only config.
"""

from __future__ import annotations

import logging
from typing import AsyncIterator, Optional

import asyncio

log = logging.getLogger(__name__)

# SAPI ISpeechVoice.Speak flags
_SVSF_ASYNC = 1
_SVSF_PURGE_BEFORE_SPEAK = 2


def _select_voice(voice, hint: str) -> None:
    """Best-effort: select the first installed voice whose name contains `hint`."""
    try:
        tokens = voice.GetVoices()
        for i in range(tokens.Count):
            tok = tokens.Item(i)
            if hint.lower() in tok.GetDescription().lower():
                voice.Voice = tok
                return
    except Exception as exc:
        log.debug("SAPI TTS: voice select failed for %r — %s", hint, exc)


def speak_sync(text: str, rate: int = 0, voice_hint: Optional[str] = None) -> bool:
    """Synthesise and play `text` synchronously (blocks until playback ends).

    Returns True on success, False on any error. Safe from a worker thread.
    """
    if not text or not text.strip():
        return False
    try:
        import pythoncom
        import win32com.client
    except ImportError:
        log.warning("SAPI TTS: pywin32 not available — cannot speak")
        return False

    pythoncom.CoInitialize()
    try:
        voice = win32com.client.Dispatch("SAPI.SpVoice")
        if rate:
            try:
                voice.Rate = int(rate)   # -10 (slow) .. 10 (fast)
            except Exception:
                pass
        if voice_hint:
            _select_voice(voice, voice_hint)
        voice.Speak(text)   # synchronous — blocks until spoken
        return True
    except Exception as exc:
        log.error("SAPI TTS: speak error — %s", exc)
        return False
    finally:
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass


class SapiClient:
    """Windows SAPI drop-in for PollyStreamClient (used by get_client dispatch)."""

    def __init__(self, rate: int = 0, voice_hint: Optional[str] = None) -> None:
        self.rate = rate
        self.voice_hint = voice_hint

    # --- Synchronous -------------------------------------------------------

    def speak_sync(self, text: str) -> bool:
        return speak_sync(text, rate=self.rate, voice_hint=self.voice_hint)

    # --- Async wrappers ----------------------------------------------------

    async def speak(self, text: str) -> bool:
        return await asyncio.to_thread(self.speak_sync, text)

    async def speak_stream(self, tokens: AsyncIterator[str]) -> str:
        """No true token streaming — buffer the full response, then speak once."""
        parts: list[str] = []
        async for tok in tokens:
            parts.append(tok)
        text = "".join(parts)
        if text.strip():
            await asyncio.to_thread(self.speak_sync, text)
        return text

    def cancel(self) -> None:
        """Best-effort: purge any queued/active speech."""
        try:
            import pythoncom
            import win32com.client
            pythoncom.CoInitialize()
            try:
                v = win32com.client.Dispatch("SAPI.SpVoice")
                v.Speak("", _SVSF_ASYNC | _SVSF_PURGE_BEFORE_SPEAK)
            finally:
                pythoncom.CoUninitialize()
        except Exception:
            pass

    def shutdown(self) -> None:
        pass

    def get_status(self) -> dict:
        return {"backend": "sapi", "rate": self.rate, "voice_hint": self.voice_hint}
