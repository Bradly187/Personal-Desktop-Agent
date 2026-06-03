"""VoicePromptComposer — accumulates voice utterances into a Claude Code prompt.

State machine:
  IDLE      → trigger phrase  → COMPOSING  (TTS: "Composing.")
  COMPOSING → any utterance   → append     (TTS: "Added.")
  COMPOSING → "submit"/"send" → type + ⏎  → IDLE  (TTS: "Submitted.")
  COMPOSING → "cancel"        → discard    → IDLE  (TTS: "Cancelled.")
  COMPOSING → "read back"     → TTS buffer → COMPOSING

Trigger (after "hey agent" wake-phrase strip): "claude compose", "claude", "hey claude"

Usage:
    composer = VoicePromptComposer()
    composer.set_speak_fn(polly_client.speak_sync)
    whisper.set_composer(composer)

Then say: "hey agent claude compose" → "write a function that..." → "submit"
"""
from __future__ import annotations

import asyncio
import logging
import re
from enum import Enum, auto
from typing import Callable, Optional

log = logging.getLogger(__name__)

_SUBMIT_WORDS  = frozenset({"submit", "send", "done"})
_CANCEL_WORDS  = frozenset({"cancel", "discard", "abort", "never mind", "nevermind"})
_READBACK_WORDS = frozenset({"read back", "readback", "read it back"})

# Matched against text AFTER the "hey agent" wake phrase has been stripped.
_TRIGGER_PHRASES: tuple[str, ...] = ("claude compose", "hey claude", "claude")


class _State(Enum):
    IDLE = auto()
    COMPOSING = auto()


class VoicePromptComposer:
    """Accumulates spoken utterances and pastes them as a prompt into the focused window."""

    def __init__(self) -> None:
        self._state = _State.IDLE
        self._buffer: list[str] = []
        self._speak_fn: Optional[Callable[[str], None]] = None
        self._target_hwnd: int = 0  # foreground window captured at compose-start
        self._suppress_fn: Optional[Callable[[float], None]] = None

    # ── Wiring ────────────────────────────────────────────────────────────────

    def set_speak_fn(self, fn: Callable[[str], None]) -> None:
        self._speak_fn = fn

    def set_suppress_fn(self, fn: Callable[[float], None]) -> None:
        """Wire whisper.suppress so TTS echo doesn't re-enter the compose buffer."""
        self._suppress_fn = fn

    # ── Queries ───────────────────────────────────────────────────────────────

    def is_composing(self) -> bool:
        return self._state is _State.COMPOSING

    def is_trigger(self, text: str) -> bool:
        """True if `text` (post-wake-strip) should start compose mode."""
        norm = _normalise(text)
        return any(norm == p or norm.startswith(p + " ") for p in _TRIGGER_PHRASES)

    # ── Main entry point ──────────────────────────────────────────────────────

    async def handle_utterance(self, text: str) -> None:
        """Route `text` through the state machine.

        Called from the event loop via run_coroutine_threadsafe.
        """
        norm = _normalise(text)

        if self._state is _State.IDLE:
            if self.is_trigger(text):
                content = _tail_after_trigger(norm)
                self._target_hwnd = _get_foreground_hwnd()
                self._state = _State.COMPOSING
                self._buffer.clear()
                await self._speak("Composing.")
                if content:
                    self._buffer.append(_restore_case(text, content))
                    log.info("VoicePromptComposer: started + added %r", content)
                    await self._speak("Added.")
                else:
                    log.info("VoicePromptComposer: compose mode started")
            return

        # ── COMPOSING ─────────────────────────────────────────────────────────
        bare = norm.rstrip(".,!?")

        if bare in _SUBMIT_WORDS:
            await self._do_submit()
            return

        if bare in _CANCEL_WORDS or norm in _CANCEL_WORDS:
            await self._do_cancel()
            return

        if norm in _READBACK_WORDS or any(norm.startswith(r) for r in _READBACK_WORDS):
            await self._do_readback()
            return

        self._buffer.append(text.strip())
        log.info("VoicePromptComposer: segment %d added: %r", len(self._buffer), text.strip())
        await self._speak("Added.")

    # ── Actions ───────────────────────────────────────────────────────────────

    async def _do_submit(self) -> None:
        full_text = " ".join(self._buffer).strip()
        self._buffer.clear()
        self._state = _State.IDLE
        if not full_text:
            await self._speak("Buffer is empty.")
            return
        log.info("VoicePromptComposer: submitting %d chars to hwnd=%d", len(full_text), self._target_hwnd)
        await asyncio.to_thread(_type_and_submit, full_text, self._target_hwnd)
        await self._speak("Submitted.")

    async def _do_cancel(self) -> None:
        n = len(self._buffer)
        self._buffer.clear()
        self._state = _State.IDLE
        log.info("VoicePromptComposer: cancelled (%d segment(s) discarded)", n)
        await self._speak("Cancelled.")

    async def _do_readback(self) -> None:
        if not self._buffer:
            await self._speak("Buffer is empty.")
        else:
            joined = " ".join(self._buffer)
            log.info("VoicePromptComposer: reading back %d chars", len(joined))
            await self._speak(joined)

    async def _speak(self, text: str) -> None:
        if self._speak_fn is None:
            return
        try:
            await asyncio.to_thread(self._speak_fn, text)
        except Exception as exc:
            log.debug("VoicePromptComposer._speak failed: %s", exc)
        finally:
            if self._suppress_fn is not None:
                self._suppress_fn(1.5)

    # ── Status ────────────────────────────────────────────────────────────────

    def get_status(self) -> dict:
        return {
            "composing": self.is_composing(),
            "segments": len(self._buffer),
            "chars": sum(len(s) for s in self._buffer),
        }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _normalise(text: str) -> str:
    s = re.sub(r"[^\w\s]", " ", text.lower())
    return re.sub(r"\s+", " ", s).strip()


def _tail_after_trigger(normalised: str) -> str:
    for phrase in _TRIGGER_PHRASES:
        if normalised.startswith(phrase):
            return normalised[len(phrase):].strip()
    return ""


def _restore_case(original: str, normalised_tail: str) -> str:
    """Return the tail of `original` that corresponds to `normalised_tail`."""
    for phrase in _TRIGGER_PHRASES:
        if _normalise(original).startswith(phrase):
            phrase_word_count = len(phrase.split())
            orig_words = re.split(r"[\s,\.]+", original.strip())
            return " ".join(orig_words[phrase_word_count:]).strip()
    return normalised_tail


def _get_foreground_hwnd() -> int:
    try:
        import win32gui
        return win32gui.GetForegroundWindow()
    except Exception:
        return 0


def _type_and_submit(text: str, target_hwnd: int = 0) -> None:
    """Paste `text` into `target_hwnd` and press Enter. Blocking — run in thread."""
    import time
    import win32clipboard
    import win32con
    import pyautogui

    # Restore focus to the window that was active when compose mode started.
    if target_hwnd:
        try:
            import win32gui
            import win32com.client
            # Shell.Application trick bypasses the foreground-lock on Windows 10/11
            # that prevents SetForegroundWindow from working from background threads.
            shell = win32com.client.Dispatch("WScript.Shell")
            shell.SendKeys("")  # unlock foreground permission
            win32gui.SetForegroundWindow(target_hwnd)
            time.sleep(0.1)  # let window activate before paste
        except Exception as exc:
            log.debug("_type_and_submit: SetForegroundWindow failed (%s) — pasting anyway", exc)

    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
    finally:
        win32clipboard.CloseClipboard()

    time.sleep(0.05)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.05)
    pyautogui.press("enter")
