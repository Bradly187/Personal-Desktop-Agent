"""VoiceCalibrator — guided voice calibration for condition-aware speech recognition.

Addresses the critical accessibility gap: disabled users (RA, allergies)
have voices that vary significantly across conditions.  This calibrator:

  1. Announces each phrase via Danielle TTS: "Please say: scroll down"
  2. Captures the next WhisperStream transcript as the user's response
  3. Stores the (expected → heard) mapping in AgentDB
  4. Builds a personal voice_profile with per-user VAD threshold and
     Gate 1 logprob floor derived from measured acoustics

Conditions:
    good_day     — baseline; calibrated when feeling well
    flare_day    — RA flare; fatigue, reduced breath support, slower speech
    allergy_day  — nasal congestion changes vowels significantly

Trigger via voice:
    "hey agent run voice calibration"         → good_day session
    "hey agent calibrate flare day"           → flare_day session
    "hey agent calibrate allergy day"         → allergy_day session
    "hey agent quick calibration"             → 5-phrase quick session

Trigger via iPad:
    Settings → Voice Calibration tab → select condition → Start
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:
    from calibration.acoustic_profiler import AcousticProfiler
    from storage.db import AgentDB

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Calibration phrase bank
# ---------------------------------------------------------------------------

# Full session: ~20 phrases, ~3 min
FULL_PHRASES: list[dict] = [
    # Wake phrase
    {"expected": "hey agent",             "category": "wake",       "instruction": "hey agent"},
    # Core commands
    {"expected": "click",                  "category": "verb",       "instruction": "click"},
    {"expected": "scroll down",            "category": "verb",       "instruction": "scroll down"},
    {"expected": "scroll up",              "category": "verb",       "instruction": "scroll up"},
    {"expected": "close",                  "category": "verb",       "instruction": "close"},
    {"expected": "screenshot",             "category": "verb",       "instruction": "screenshot"},
    {"expected": "open notepad",           "category": "app",        "instruction": "open Notepad"},
    {"expected": "open chrome",            "category": "app",        "instruction": "open Chrome"},
    {"expected": "open terminal",          "category": "app",        "instruction": "open terminal"},
    {"expected": "open vs code",           "category": "app",        "instruction": "open VS Code"},
    {"expected": "open slack",             "category": "app",        "instruction": "open Slack"},
    # Multi-word commands
    {"expected": "scroll down five",       "category": "navigation", "instruction": "scroll down five"},
    {"expected": "type hello",             "category": "navigation", "instruction": "type hello"},
    {"expected": "hotkey control c",       "category": "navigation", "instruction": "hotkey control C"},
    # Lecture mode
    {"expected": "start lecture mode",     "category": "mode",       "instruction": "start lecture mode"},
    {"expected": "stop lecture mode",      "category": "mode",       "instruction": "stop lecture mode"},
    # Condition declarations (test these so corrections fire reliably)
    {"expected": "this is a flare day",    "category": "condition",  "instruction": "this is a flare day"},
    {"expected": "this is a good day",     "category": "condition",  "instruction": "this is a good day"},
    # Numbers
    {"expected": "scroll up three",        "category": "navigation", "instruction": "scroll up three"},
    {"expected": "click file menu",        "category": "verb",       "instruction": "click file menu"},
]

# Quick session: wake phrase + 4 most-used commands, ~45 seconds
QUICK_PHRASES: list[dict] = [p for p in FULL_PHRASES
                              if p["expected"] in (
                                  "hey agent", "click", "scroll down",
                                  "open vs code", "close",
                              )]

CONDITION_DISPLAY: dict[str, str] = {
    "good_day":    "Good day (baseline)",
    "flare_day":   "Flare day",
    "allergy_day": "Allergy / congestion day",
}


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class PhraseResult:
    expected: str
    heard: str
    logprob: float
    duration_s: float
    match: bool = field(init=False)

    def __post_init__(self):
        self.match = self.heard.lower().strip() == self.expected.lower().strip()


@dataclass
class CalibrationReport:
    condition: str
    session_id: int
    total: int
    matched: int
    corrections_added: int
    duration_s: float
    results: list[PhraseResult]

    @property
    def accuracy(self) -> float:
        return self.matched / max(1, self.total)


# ---------------------------------------------------------------------------
# VoiceCalibrator
# ---------------------------------------------------------------------------

class VoiceCalibrator:
    """Runs guided voice calibration sessions driven by Danielle TTS prompts."""

    # How long to wait for user to speak after the TTS prompt (seconds)
    RESPONSE_TIMEOUT_S: float = 8.0
    # Pause between phrases (seconds) — gives user time to prepare
    INTER_PHRASE_PAUSE_S: float = 1.5

    def __init__(
        self,
        agent_db: "AgentDB",
        whisper_stream,
        profiler: "AcousticProfiler",
    ) -> None:
        self._db = agent_db
        self._whisper = whisper_stream
        self._profiler = profiler
        self._running = False
        # Injected by main.py — used to speak TTS prompts
        self._speak: Optional[Callable[[str], None]] = None

    def set_tts(self, speak_fn: Callable[[str], None]) -> None:
        """Wire the TTS speak function (polly_stream.get_client().speak_sync)."""
        self._speak = speak_fn

    # ---------------------------------------------------------------------- #
    # Public entry points
    # ---------------------------------------------------------------------- #

    async def run(
        self,
        condition: str = "good_day",
        quick: bool = False,
        on_progress: Optional[Callable[[int, int, PhraseResult], None]] = None,
    ) -> CalibrationReport:
        """Run a full (or quick) calibration session for the given condition.

        Args:
            condition:    "good_day" | "flare_day" | "allergy_day"
            quick:        True → use 5-phrase QUICK_PHRASES set
            on_progress:  callback(phrase_idx, total, result) for iPad UI updates

        Returns:
            CalibrationReport with accuracy stats and corrections built.
        """
        if self._running:
            log.warning("VoiceCalibrator: session already in progress")
            return CalibrationReport(condition, -1, 0, 0, 0, 0.0, [])

        if condition not in CONDITION_DISPLAY:
            # e.g. an old iPad build sending the removed svt_attack condition
            log.warning(
                "VoiceCalibrator: unknown condition %r — falling back to good_day",
                condition,
            )
            condition = "good_day"

        self._running = True
        t_start = time.monotonic()
        phrases = QUICK_PHRASES if quick else FULL_PHRASES
        results: list[PhraseResult] = []

        # Announce session start
        mode_label = "quick " if quick else ""
        condition_label = CONDITION_DISPLAY.get(condition, condition)
        self._speak_safe(
            f"Starting {mode_label}voice calibration for {condition_label}. "
            f"I will ask you to say {len(phrases)} phrases. "
            f"Repeat each phrase clearly after the beep."
        )
        await asyncio.sleep(2.0)

        # Create DB session
        session_id = await self._db.start_calibration_session(condition)

        try:
            for i, phrase_def in enumerate(phrases):
                if not self._running:
                    break

                expected = phrase_def["expected"]
                instruction = phrase_def["instruction"]

                # Prompt the user
                self._speak_safe(f"Say: {instruction}")

                # Suppress mic during prompt + 0.5s echo tail, then open for response
                word_count = len(instruction.split())
                tts_duration_estimate = max(1.5, word_count * 0.35)
                self._whisper.suppress(tts_duration_estimate + 0.5)
                await asyncio.sleep(tts_duration_estimate + 0.5)

                # Capture the user's response via WhisperStream
                heard, logprob, duration_s = await self._capture_response()

                result = PhraseResult(
                    expected=expected,
                    heard=heard,
                    logprob=logprob,
                    duration_s=duration_s,
                )
                results.append(result)

                # Store in DB
                await self._db.insert_pronunciation(
                    session_id=session_id,
                    expected=expected,
                    heard=heard,
                    logprob=logprob,
                    duration_s=duration_s,
                )

                # Brief spoken feedback
                if result.match:
                    log.info("Calibration [%d/%d] MATCH: %r", i + 1, len(phrases), expected)
                else:
                    log.info(
                        "Calibration [%d/%d] MISMATCH: expected=%r heard=%r",
                        i + 1, len(phrases), expected, heard,
                    )

                if on_progress:
                    on_progress(i + 1, len(phrases), result)

                await asyncio.sleep(self.INTER_PHRASE_PAUSE_S)

        finally:
            self._running = False

        # Build personal corrections from mismatches
        corrections = self._build_corrections(results)
        corrections_added = len(corrections)

        # Save voice profile for this condition
        await self._db.save_voice_profile(
            condition=condition,
            corrections=corrections,
            vad_threshold=self._profiler.get_vad_threshold(),
            logprob_floor=self._profiler.get_logprob_floor(),
        )

        # Announce completion
        accuracy_pct = int(round(sum(1 for r in results if r.match) / max(1, len(results)) * 100))
        self._speak_safe(
            f"Calibration complete. {accuracy_pct} percent accuracy. "
            f"{corrections_added} personal corrections saved."
        )

        elapsed = time.monotonic() - t_start
        matched = sum(1 for r in results if r.match)

        log.info(
            "VoiceCalibrator: session complete — condition=%s accuracy=%d%% "
            "corrections=%d duration=%.0fs",
            condition, accuracy_pct, corrections_added, elapsed,
        )

        return CalibrationReport(
            condition=condition,
            session_id=session_id,
            total=len(results),
            matched=matched,
            corrections_added=corrections_added,
            duration_s=elapsed,
            results=results,
        )

    def stop(self) -> None:
        """Cancel an in-progress session."""
        self._running = False

    # ---------------------------------------------------------------------- #
    # Internal helpers
    # ---------------------------------------------------------------------- #

    async def _capture_response(self) -> tuple[str, float, float]:
        """Wait for the next WhisperStream transcription after the mic opens.

        Returns (heard_text, avg_logprob, duration_s).
        Times out after RESPONSE_TIMEOUT_S, returning ("", -1.0, 0.0).
        """
        # Register a one-shot capture callback on WhisperStream
        future: asyncio.Future[tuple[str, float, float]] = asyncio.get_event_loop().create_future()

        def _on_transcript(text: str, logprob: float, duration_s: float) -> None:
            if not future.done():
                future.set_result((text, logprob, duration_s))

        self._whisper.set_calibration_capture(_on_transcript)

        try:
            return await asyncio.wait_for(future, timeout=self.RESPONSE_TIMEOUT_S)
        except asyncio.TimeoutError:
            log.warning("VoiceCalibrator: response timeout after %.1fs", self.RESPONSE_TIMEOUT_S)
            return ("", -1.0, 0.0)
        finally:
            self._whisper.set_calibration_capture(None)

    def _build_corrections(self, results: list[PhraseResult]) -> dict[str, str]:
        """Build {heard: expected} correction map from mismatched results."""
        corrections: dict[str, str] = {}
        for r in results:
            if not r.match and r.heard:
                heard_key = r.heard.lower().strip()
                if heard_key and heard_key != r.expected.lower():
                    corrections[heard_key] = r.expected.lower()
                    log.info(
                        "VoiceCalibrator: correction learned: %r → %r",
                        heard_key, r.expected,
                    )
        return corrections

    def _speak_safe(self, text: str) -> None:
        """Speak via TTS, silently skipping if no TTS is wired."""
        if self._speak:
            try:
                self._speak(text)
            except Exception as exc:
                log.warning("VoiceCalibrator TTS error: %s", exc)
