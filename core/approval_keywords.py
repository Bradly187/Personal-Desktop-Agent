"""Shared approval-gate confirmation vocabulary — single source of truth.

Both halves of the voice approval gate parse the same yes/no language and must
never drift out of sync:

  * ``sensors/whisper_stream.py`` decides whether a transcript is a *deliberate*
    answer worth writing to ``~/.claude/approval/response`` — ambient audio, the
    TTS echo of the question, or a stray word must NOT be treated as an answer.
  * ``approval_hook.py`` (the Claude Code ``PreToolUse`` hook) reads that
    response and turns it into allow (exit 0) / block (exit 2).

If the two used different keyword lists, one side could treat a word as a
confirmation that the other ignored — exactly the kind of mismatch that let
background speech silently approve a destructive tool call (the bug this module
fixes). Keeping the vocabulary here guarantees they agree.

No heavy imports: ``approval_hook.py`` is a latency-sensitive hook on the
critical path of every gated tool call.
"""

from __future__ import annotations

import re

# Single-word confirmations — compared against the set of punctuation-stripped
# words in the transcript.
APPROVE_WORDS: frozenset[str] = frozenset({
    "yes", "yeah", "yep", "yup", "ok", "okay", "sure", "approve", "approved",
    "go", "proceed", "continue", "affirmative", "correct", "absolutely",
    "definitely", "fine", "alright", "confirm", "confirmed",
})

REJECT_WORDS: frozenset[str] = frozenset({
    "no", "nope", "nah", "stop", "cancel", "cancelled", "reject", "rejected",
    "abort", "block", "deny", "denied", "negative", "skip", "disallow",
    "refuse", "dont",
})

# Multi-word confirmation phrases — matched as substrings of the normalised
# transcript so "go ahead and do it" still resolves even though no single word
# above appears.
APPROVE_PHRASES: tuple[str, ...] = (
    "go ahead", "do it", "go for it", "sounds good", "looks good",
    "yes please",
)

REJECT_PHRASES: tuple[str, ...] = (
    "do not", "no thanks", "no thank", "hold on", "hold off", "back off",
    "never mind", "nevermind", "cut it out",
)

# Approvals are short, deliberate utterances (1-3 words in practice). A long
# transcript that merely happens to contain a keyword is almost always ambient
# audio — a podcast, a phone call, a sentence about something unrelated — so we
# refuse to read it as an answer. (Real 2026-06-04 logs that wrongly resolved
# the gate: "share our oil", "Let's put C3 up on the screen", "broken record".)
MAX_ANSWER_WORDS: int = 6

_WORD_RE = re.compile(r"[a-z']+")


def _normalise(text: str) -> tuple[str, list[str]]:
    """Lower-case, strip punctuation, return (normalised string, word list).

    Apostrophes are dropped from the words so "don't" matches the "dont" entry
    in REJECT_WORDS regardless of how Whisper renders the contraction.
    """
    words = [w.replace("'", "") for w in _WORD_RE.findall((text or "").lower())]
    words = [w for w in words if w]
    return " ".join(words), words


def classify_confirmation(text: str) -> str | None:
    """Classify a transcript as a deliberate approval answer.

    Returns:
        ``"approve"`` — the transcript clearly grants permission.
        ``"deny"``    — the transcript clearly refuses.
        ``None``      — no confirmation keyword present, or the utterance is too
                        long to be a deliberate yes/no (ambient speech / garbage
                        / the TTS echo of the question). Callers must treat this
                        as "no answer yet" and keep waiting — never as consent.

    Deny always wins over approve when both appear, so an ambiguous answer fails
    safe toward blocking the action.
    """
    norm, words = _normalise(text)
    if not words:
        return None

    # Too long to be a deliberate yes/no — treat as ambient, not an answer.
    if len(words) > MAX_ANSWER_WORDS:
        return None

    word_set = set(words)
    deny = bool(word_set & REJECT_WORDS) or any(p in norm for p in REJECT_PHRASES)
    approve = bool(word_set & APPROVE_WORDS) or any(p in norm for p in APPROVE_PHRASES)

    if deny:
        return "deny"      # fail-safe: deny wins ties
    if approve:
        return "approve"
    return None
