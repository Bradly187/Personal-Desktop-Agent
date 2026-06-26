"""Voice conversation mode — a wake/sleep-gated, talk-only dialogue state.

The accessibility pipeline treats every utterance as a *command*: a transcript
goes through the DomainClassifier and either fires an accessibility verb or a
single-shot dev query. ``core/conversation_state.ConversationState`` adds narrow
anaphora ("do that again") and a last-action hint, but there is no running
*dialogue* — each turn is stateless.

``ConversationMode`` is the missing piece: an explicit, voice-driven mode the
user enters with a **wake phrase** ("let's talk") and leaves with a **sleep
phrase** ("that's all"). While active, every utterance (other than the sleep
phrase) is a conversational turn answered by the resident *general* model with
the full running history threaded into the prompt — the command pipeline is
bypassed entirely. Replies are spoken back via the configured TTS backend
(Kokoro by default).

Design constraints honoured here:

* **Pure / synchronous / no I/O.** All model inference, TTS, and mic suppression
  live in ``HybridCoordinator`` (the orchestrator). This object only holds state
  and runs deterministic phrase detection, so it is trivial to unit-test and
  cheap to call on every command (AGENTS.md #2 — nothing heavy near the tick).
* **Fail-safe-DENY (AGENTS.md #4).** Detection is conservative anchored matching:
  a phrase must constitute essentially the *whole* utterance (modulo trivial
  politeness fillers), so ordinary speech like "how do you say goodbye in
  French" never toggles the mode. A non-match leaves the current state untouched.
* **Default OFF.** Experimental — ship behind ``conversation_mode.enabled`` in
  ``~/.claude/ipad_bridge/config.json`` until a baseline locks (AGENTS.md #9).

Spec: ``specs/conversation-mode/``.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

__all__ = ["ConversationTurn", "ConversationMode", "conversation_mode_config"]


# Leading filler/wake words stripped before matching, so "hey agent, let's talk"
# normalises to "let's talk". Trailing politeness ("please", "now") is stripped
# too. Kept deliberately small — these must never swallow a meaningful word.
_LEADING_FILLER: frozenset[str] = frozenset(
    {"hey", "ok", "okay", "agent", "computer", "please", "um", "uh", "so"}
)
_TRAILING_FILLER: frozenset[str] = frozenset({"please", "now", "then"})

_DEFAULT_WAKE: tuple[str, ...] = (
    "let's talk",
    "lets talk",
    "let's chat",
    "lets chat",
    "let's have a conversation",
    "let's have a chat",
    "start a conversation",
    "start conversation",
    "conversation mode",
    "enter conversation mode",
)

_DEFAULT_SLEEP: tuple[str, ...] = (
    "that's all",
    "thats all",
    "that's all for now",
    "that is all",
    "goodbye",
    "good bye",
    "bye for now",
    "we're done",
    "were done",
    "we are done",
    "stop talking",
    "end conversation",
    "end the conversation",
    "exit conversation",
    "stop conversation",
    "leave conversation mode",
)

# A conversational, TTS-friendly directive prepended to the model context so the
# resident general model answers in short spoken-style replies rather than the
# long research-synthesis prose its profile system prompt asks for.
_DEFAULT_DIRECTIVE = (
    "You are having a spoken, back-and-forth conversation with the user. Reply "
    "naturally and concisely — a few sentences at most, suitable for being read "
    "aloud by text-to-speech. Do not use markdown, headings, bullet lists, code "
    "blocks, or emoji. Ask a brief follow-up question when it keeps the "
    "conversation going."
)


def _normalize(text: str) -> str:
    """Lower-case, strip punctuation to spaces, collapse whitespace, and drop a
    small set of leading/trailing filler words. Empty string for empty input."""
    if not text:
        return ""
    norm = re.sub(r"[^\w\s]", " ", text.lower())
    norm = re.sub(r"\s+", " ", norm).strip()
    if not norm:
        return ""
    words = norm.split()
    while words and words[0] in _LEADING_FILLER:
        words.pop(0)
    while words and words[-1] in _TRAILING_FILLER:
        words.pop()
    return " ".join(words)


@dataclass
class ConversationTurn:
    """One turn of the running dialogue."""

    role: str       # "user" | "assistant"
    content: str


def conversation_mode_config() -> dict:
    """The ``conversation_mode`` config block. Default: disabled (experimental,
    ship behind a flag until a baseline locks — spec §4)."""
    default = {"enabled": False}
    try:
        cfg_path = Path(os.path.expanduser("~/.claude/ipad_bridge/config.json"))
        if not cfg_path.exists():
            return default
        block = json.loads(cfg_path.read_text(encoding="utf-8")).get("conversation_mode")
        return block if isinstance(block, dict) else default
    except Exception as exc:  # malformed config must never crash the pipeline
        log.debug("conversation_mode config unreadable (%s)", exc)
        return default


@dataclass
class ConversationMode:
    """Wake/sleep-gated talk-only dialogue state + deterministic phrase detection."""

    enabled: bool = False
    wake_phrases: frozenset[str] = field(
        default_factory=lambda: frozenset(_normalize(p) for p in _DEFAULT_WAKE)
    )
    sleep_phrases: frozenset[str] = field(
        default_factory=lambda: frozenset(_normalize(p) for p in _DEFAULT_SLEEP)
    )
    directive: str = _DEFAULT_DIRECTIVE
    max_history_turns: int = 12   # user+assistant entries retained (≈6 exchanges)

    active: bool = False
    _history: list[ConversationTurn] = field(default_factory=list)

    # -- construction ----------------------------------------------------- #
    @classmethod
    def from_config(cls, cfg: dict | None = None) -> "ConversationMode":
        """Build from a ``conversation_mode`` config block (see
        :func:`conversation_mode_config`). Unknown / malformed values fall back
        to the defaults so a bad config never disables detection silently in a
        confusing way — it simply uses the built-in phrase sets."""
        cfg = cfg or {}
        wake = cfg.get("wake_phrases")
        sleep = cfg.get("sleep_phrases")
        kwargs: dict = {"enabled": bool(cfg.get("enabled", False))}
        if isinstance(wake, list) and wake:
            kwargs["wake_phrases"] = frozenset(_normalize(str(p)) for p in wake) - {""}
        if isinstance(sleep, list) and sleep:
            kwargs["sleep_phrases"] = frozenset(_normalize(str(p)) for p in sleep) - {""}
        if isinstance(cfg.get("directive"), str) and cfg["directive"].strip():
            kwargs["directive"] = cfg["directive"].strip()
        try:
            mh = int(cfg.get("max_history_turns", 12))
            if mh > 0:
                kwargs["max_history_turns"] = mh
        except (TypeError, ValueError):
            pass
        return cls(**kwargs)

    # -- detection (deterministic, no model) ------------------------------ #
    def match_wake(self, text: str) -> tuple[bool, str]:
        """Detect a wake phrase at the START of the utterance.

        Returns ``(matched, remainder)``. ``remainder`` is the rest of the
        utterance after the wake phrase — so "conversation mode, what's the
        weather like" matches and yields the first conversational turn
        ("what s the weather like") in one breath; a bare "let's talk" matches
        with an empty remainder. Prefix (not whole-utterance) matching is what
        lets the wake word and the first question arrive together, as they do in
        natural speech. Wake phrases are distinctive enough that a prefix match
        carries little false-positive risk, and a wrong entry is harmless — the
        user simply says "that's all". Sleep detection stays strict (anchored).
        """
        norm = _normalize(text)
        if not norm:
            return (False, "")
        tokens = norm.split()
        # Longest phrase first, so "let's have a conversation" wins over a
        # hypothetical shorter prefix.
        for phrase in sorted(self.wake_phrases, key=lambda p: -len(p.split())):
            ptokens = phrase.split()
            if ptokens and tokens[: len(ptokens)] == ptokens:
                return (True, " ".join(tokens[len(ptokens):]).strip())
        return (False, "")

    def detect_wake(self, text: str) -> bool:
        """True iff ``text`` BEGINS with a wake phrase (see :meth:`match_wake`)."""
        return self.match_wake(text)[0]

    def detect_sleep(self, text: str) -> bool:
        """True iff ``text`` is (essentially) one of the sleep phrases.

        Strict whole-utterance match (unlike wake) so a sleep word embedded in a
        sentence ("how do you say goodbye in French") never ends the dialogue."""
        return _normalize(text) in self.sleep_phrases

    # -- lifecycle -------------------------------------------------------- #
    def enter(self) -> None:
        """Enter conversation mode with a fresh history."""
        self.active = True
        self._history.clear()

    def exit(self) -> None:
        """Leave conversation mode and discard history."""
        self.active = False
        self._history.clear()

    # -- history ---------------------------------------------------------- #
    def record(self, role: str, content: str) -> None:
        self._history.append(ConversationTurn(role=role, content=content or ""))
        if len(self._history) > self.max_history_turns:
            del self._history[: len(self._history) - self.max_history_turns]

    @property
    def history(self) -> list[ConversationTurn]:
        return list(self._history)

    def build_context(self) -> str:
        """The directive plus a plain-text transcript of prior turns, to be
        passed as ``ModelRouter.infer(context=...)``. The *current* user turn is
        NOT included here — it is supplied separately as ``user_text``. Returns
        just the directive when there is no prior history (first reply)."""
        lines = [self.directive]
        if self._history:
            lines.append("")
            lines.append("Conversation so far:")
            for turn in self._history:
                speaker = "User" if turn.role == "user" else "Assistant"
                lines.append(f"{speaker}: {turn.content}")
        return "\n".join(lines)
