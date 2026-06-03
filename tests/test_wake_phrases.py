"""Wake-phrase robustness: tolerate Whisper mishearings of "agent".

Live device run: "hey agent" was transcribed as "Hey agents" / "Hey, Aiden" —
Whisper routinely mishears "agent". The wake gate requires the transcript to
start with a wake phrase, so without these variants the command was silently
dropped. This guards the variant list and replicates the gate's match logic.
"""

import re

from sensors.whisper_stream import WhisperStream


def test_wake_phrases_include_common_mishearings():
    wp = WhisperStream.WAKE_PHRASES
    for variant in ("hey agent", "agent", "hey aiden", "aiden",
                    "hey agents", "agents"):
        assert variant in wp, f"missing wake variant: {variant!r}"


def _match_and_strip(text: str) -> str | None:
    """Replicates the wake gate (whisper_stream._transcribe): normalize, match a
    wake phrase at the start, strip it, return the remaining command (or None)."""
    normalised = re.sub(r'[^\w\s]', ' ', text.lower())
    normalised = re.sub(r'\s+', ' ', normalised).strip()
    matched = next(
        (p for p in sorted(WhisperStream.WAKE_PHRASES, key=len, reverse=True)
         if normalised.startswith(p)),
        None,
    )
    if matched is None:
        return None
    words = re.split(r'[\s,\.]+', text.strip())
    command = ' '.join(w for w in words[len(matched.split()):] if w)
    return command if command and re.search(r'[a-zA-Z]', command) else None


def test_mishearing_agents_routes_command():
    assert _match_and_strip("Hey agents, pain day on") == "pain day on"


def test_mishearing_aiden_routes_command():
    assert _match_and_strip("Hey, Aiden, scroll up") == "scroll up"


def test_clean_wake_still_works():
    assert _match_and_strip("Hey agent, open kiro") == "open kiro"


def test_no_wake_phrase_discarded():
    assert _match_and_strip("Pain day on.") is None


def test_wake_with_no_command_discarded():
    assert _match_and_strip("hey agent") is None
