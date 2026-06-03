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
    # Single-segment helper still models the no-arming case (legacy behaviour).
    assert _match_and_strip("hey agent") is None


# --- wake-arming window: tolerate VAD splitting "hey agent" from the command --

def _gate(text, armed_until, now, window=6.0):
    """Replicates the wake gate's arm/accept/discard decision (whisper_stream).
    Returns (command_or_None, new_armed_until)."""
    normalised = re.sub(r'[^\w\s]', ' ', text.lower())
    normalised = re.sub(r'\s+', ' ', normalised).strip()
    matched = next(
        (p for p in sorted(WhisperStream.WAKE_PHRASES, key=len, reverse=True)
         if normalised.startswith(p)),
        None,
    )
    armed = now < armed_until
    if matched is None:
        if armed:
            if len(text.split()) > 8:
                return None, 0.0           # ambient leaked into the open window
            return text, 0.0               # armed command accepted
        return None, armed_until           # no wake, not armed → discard
    words = re.split(r'[\s,\.]+', text.strip())
    stripped = ' '.join(w for w in words[len(matched.split()):] if w)
    if not stripped or not re.search(r'[a-zA-Z]', stripped):
        return None, now + window          # bare wake → arm
    return stripped, 0.0                    # wake + command in one segment


def test_bare_wake_arms_then_next_segment_is_command():
    # Segment 1: just "hey agent" (VAD split) → arms the window
    cmd, armed = _gate("Hey, agent.", armed_until=0.0, now=100.0)
    assert cmd is None and armed == 106.0
    # Segment 2: the command, within the window → accepted without a wake
    cmd2, armed2 = _gate("pain day on.", armed_until=armed, now=101.0)
    assert cmd2 == "pain day on." and armed2 == 0.0


def test_command_without_prior_wake_still_discarded():
    cmd, armed = _gate("pain day on.", armed_until=0.0, now=100.0)
    assert cmd is None


def test_armed_window_rejects_long_ambient():
    cmd, armed = _gate("so anyway the whole thing about the movie was really quite something",
                       armed_until=200.0, now=101.0)
    assert cmd is None  # >8 words → treated as ambient, not a command


def test_single_segment_wake_plus_command_unchanged():
    cmd, armed = _gate("hey agent pain day on", armed_until=0.0, now=100.0)
    assert cmd == "pain day on" and armed == 0.0
