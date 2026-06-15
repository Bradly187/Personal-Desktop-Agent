"""Conversational continuity for the voice command path.

The accessibility pipeline historically treated every utterance in isolation.
``cmd.session_context`` already carries the last few raw command *texts* into the
LLM prompt (see ``inference/local_inference._build_prompt``), but raw text is a
weak referent: it records that the user *said* "click save", not that a CLICK on
"Save button" at (812, 440) actually *succeeded*. That gap is why phrasings like
"do that again" or "click it" could not be resolved precisely — especially by the
local 8B model, which is poor at anaphora.

``ConversationState`` records the *resolved outcome* of each turn and offers two
deterministic, no-model-round-trip services consumed by ``HybridCoordinator``:

1. :meth:`resolve_anaphora` — rewrite a small, tightly-anchored set of anaphoric
   utterances ("again", "do that again", "click it") against the previous turn
   *before* inference. Matching is deliberately conservative so it never hijacks
   dictation ("type that's great") or ordinary commands.
2. :meth:`prompt_hint` — a single compact line summarising the last action, appended
   to ``session_context`` so both the local and cloud prompts see structured state.

The object holds no external dependencies and is pure/​synchronous, so it is cheap
to call on every command and trivial to unit-test.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

__all__ = ["Turn", "ConversationState"]


# Verbs that are not meaningful to re-issue or to treat as the antecedent of a
# pronoun. CLARIFY is a question, not an action; SCREENSHOT has no target.
_NON_REISSUABLE: frozenset[str] = frozenset({"CLARIFY"})

# Whole-utterance "repeat the last action" triggers. The utterance must be
# essentially ONLY one of these phrases — anchored with ^...$ on the normalised
# text — so a longer sentence that merely contains "again" is left untouched.
_REPEAT_RE = re.compile(
    r"^(?:do (?:that|it) again|again(?: please)?|repeat(?: that)?|"
    r"same again|do the same(?: thing)?|once more)$"
)

# "<verb> it/that/this [one]" — the verb's object is the previous turn's target.
# Restricted to a known verb set so free-form dictation can't match.
_PRONOUN_RE = re.compile(
    r"^(click|close|open|select|press|scroll)\s+(?:it|that|this)(?:\s+one)?$"
)


@dataclass
class Turn:
    """One resolved command turn."""

    command_text: str            # the utterance that produced this turn (post-rewrite)
    verb: str                    # parsed accessibility verb, upper-cased
    target: str = ""             # named target ("Save button"), or "" if none
    coords: Optional[tuple[int, int]] = None  # resolved click pixel, if any
    app: str = ""                # active window/app at execution (best-effort)
    success: bool = False


@dataclass
class ConversationState:
    """Rolling record of recent resolved turns + deterministic anaphora helpers."""

    max_turns: int = 10
    _turns: list[Turn] = field(default_factory=list)

    # -- recording -------------------------------------------------------- #
    def record(
        self,
        *,
        command_text: str,
        verb: str,
        target: str = "",
        coords: Optional[tuple[int, int]] = None,
        app: str = "",
        success: bool = False,
    ) -> None:
        """Append a resolved turn, trimming to ``max_turns``."""
        self._turns.append(
            Turn(
                command_text=command_text or "",
                verb=(verb or "").upper(),
                target=target or "",
                coords=coords,
                app=app or "",
                success=bool(success),
            )
        )
        if len(self._turns) > self.max_turns:
            del self._turns[: len(self._turns) - self.max_turns]

    def clear(self) -> None:
        self._turns.clear()

    @property
    def last(self) -> Optional[Turn]:
        return self._turns[-1] if self._turns else None

    def last_actionable(self) -> Optional[Turn]:
        """Most recent successful turn whose verb is worth re-issuing."""
        for turn in reversed(self._turns):
            if turn.success and turn.verb and turn.verb not in _NON_REISSUABLE:
                return turn
        return None

    # -- services consumed by the coordinator ----------------------------- #
    def resolve_anaphora(self, text: str) -> tuple[str, bool]:
        """Rewrite an anaphoric utterance against the previous turn.

        Returns ``(text, changed)``. ``changed`` is False (and ``text`` is
        returned verbatim) whenever there is no antecedent or the utterance does
        not match one of the narrow anaphora patterns — the safe default, so a
        non-match never alters a legitimate command or dictation.
        """
        if not text or not text.strip():
            return text, False
        norm = re.sub(r"[^\w\s]", " ", text.lower())
        norm = re.sub(r"\s+", " ", norm).strip()

        prev = self.last_actionable()
        if prev is None:
            return text, False

        # "again" family → re-issue the previous concrete command verbatim.
        if _REPEAT_RE.match(norm):
            return prev.command_text, True

        # "<verb> it/that" → substitute the previous target as the object.
        m = _PRONOUN_RE.match(norm)
        if m and prev.target:
            verb_word = text.strip().split()[0]  # preserve original casing
            return f"{verb_word} {prev.target}", True

        return text, False

    def prompt_hint(self) -> str:
        """One compact line describing the last action, for prompt context.

        Empty string when there is no history (first turn of a session), so the
        caller can append unconditionally without polluting an empty prompt.
        """
        turn = self.last
        if turn is None or not turn.verb:
            return ""
        outcome = "done" if turn.success else "failed"
        body = f"{turn.verb} {turn.target}".strip()
        return f"Last action: {body} ({outcome})"
