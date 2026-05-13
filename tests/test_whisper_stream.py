"""Unit tests for WhisperStream — VAD, transcription dispatch, edge cases.

Runs without GPU or installed ML libraries by injecting minimal mocks for
numpy, faster-whisper, and command_executor into sys.modules before importing
whisper_stream.  command_executor is mocked because it pulls in pyautogui and
win32 bindings at import time; replacing it with a stub that exposes just
Command is sufficient for these tests.

Run:
    python tests/test_whisper_stream.py

Exit codes:
    0 — all tests passed
    1 — one or more tests failed
"""

from __future__ import annotations

import asyncio
import base64
import math
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Add project root to path
# ---------------------------------------------------------------------------

sys.path.insert(0, str(Path(__file__).parent.parent))

# ---------------------------------------------------------------------------
# Stub Command (mirrors command_executor.Command fields exactly)
# ---------------------------------------------------------------------------

@dataclass
class _Command:
    text: str
    action: str
    source: str
    whisper_logprob: float = 0.0
    gesture_confidence: float = 1.0
    session_context: list = field(default_factory=list)
    gaze_coords: tuple = None
    params: dict = field(default_factory=dict)


_mock_ce = MagicMock()
_mock_ce.Command = _Command
sys.modules["command_executor"] = _mock_ce  # prevents pyautogui/win32 import

# ---------------------------------------------------------------------------
# Minimal numpy mock — supports every operation used in whisper_stream.py
# ---------------------------------------------------------------------------

class _FakeArray:
    """List-backed array implementing the numpy subset used in whisper_stream."""

    def __init__(self, data: list) -> None:
        self._data: list = list(data)

    def __len__(self) -> int:
        return len(self._data)

    def __getitem__(self, key):
        result = self._data[key]
        return _FakeArray(result) if isinstance(result, list) else result

    def astype(self, _dtype) -> "_FakeArray":
        return _FakeArray([float(x) for x in self._data])

    def __truediv__(self, scalar) -> "_FakeArray":
        return _FakeArray([x / scalar for x in self._data])

    def __pow__(self, exp) -> "_FakeArray":
        return _FakeArray([x ** exp for x in self._data])


class _NumpyMock:
    float32 = "float32"
    float64 = "float64"
    int16 = "int16"

    def frombuffer(self, data: bytes, dtype=None) -> _FakeArray:
        if dtype in (self.int16, "int16"):
            n = len(data) // 2
            return _FakeArray(list(struct.unpack(f"<{n}h", data[: n * 2])))
        n = len(data) // 4
        return _FakeArray(list(struct.unpack(f"<{n}f", data[: n * 4])))

    def concatenate(self, arrays) -> _FakeArray:
        combined: list = []
        for a in arrays:
            combined.extend(a._data if isinstance(a, _FakeArray) else list(a))
        return _FakeArray(combined)

    def sqrt(self, x):
        if isinstance(x, _FakeArray):
            return _FakeArray([math.sqrt(abs(v)) for v in x._data])
        return math.sqrt(abs(float(x)))

    def mean(self, arr) -> float:
        data = arr._data if isinstance(arr, _FakeArray) else [float(arr)]
        return sum(data) / len(data) if data else 0.0


sys.modules["numpy"] = _NumpyMock()  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Minimal faster-whisper mock
# ---------------------------------------------------------------------------

class _FakeSegment:
    def __init__(self, text: str, logprob: float = -0.3) -> None:
        self.text = text
        self.avg_logprob = logprob


class _FakeWhisperModel:
    """Mock WhisperModel: records calls, returns configurable segments."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.transcribe_calls: list[dict] = []
        self._segments: list[_FakeSegment] = [_FakeSegment("scroll down")]

    def set_segments(self, segments: list[_FakeSegment]) -> None:
        self._segments = segments

    def transcribe(self, audio, **kwargs) -> tuple[list, object]:
        self.transcribe_calls.append({"audio_len": len(audio), **kwargs})
        return list(self._segments), object()


_fw_mock = MagicMock()
_fw_mock.WhisperModel = _FakeWhisperModel
sys.modules["faster_whisper"] = _fw_mock

# ---------------------------------------------------------------------------
# Now safe to import whisper_stream
# ---------------------------------------------------------------------------

import whisper_stream  # noqa: E402
from whisper_stream import WhisperStream

# ---------------------------------------------------------------------------
# Audio fixtures
# ---------------------------------------------------------------------------

_FRAMES = 800   # 50 ms at 16 kHz (matches iPad AudioStreamer)


def _make_chunk_b64(amplitude: int) -> str:
    raw = struct.pack(f"<{_FRAMES}h", *([amplitude] * _FRAMES))
    return base64.b64encode(raw).decode()


SPEECH_CHUNK = _make_chunk_b64(15_000)  # RMS ≈ 0.46  >> silence threshold
SILENCE_CHUNK = _make_chunk_b64(30)     # RMS ≈ 0.001 << silence threshold


# ---------------------------------------------------------------------------
# Mock FusionEngine
# ---------------------------------------------------------------------------

class _MockFusion:
    def __init__(self) -> None:
        self.received: list = []

    def on_voice(self, cmd) -> None:
        self.received.append(cmd)


# ---------------------------------------------------------------------------
# Factory helper — fast settings for tests
# ---------------------------------------------------------------------------

async def _start_ws(
    *,
    silence_duration_s: float = 0.1,   # 1 600 samples — 2 chunks of 800
    min_speech_s: float = 0.05,        # 800 samples — 1 chunk
    max_buffer_s: float = 2.0,
    poll_interval_s: float = 0.05,
) -> tuple[WhisperStream, _MockFusion, _FakeWhisperModel]:
    ws = WhisperStream(
        silence_duration_s=silence_duration_s,
        min_speech_s=min_speech_s,
        max_buffer_s=max_buffer_s,
        poll_interval_s=poll_interval_s,
    )
    fusion = _MockFusion()
    ws.set_fusion_engine(fusion)
    await ws.start()
    model: _FakeWhisperModel = ws._model  # type: ignore[assignment]
    return ws, fusion, model


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_vad_flush_emits_voice_command() -> tuple[bool, str]:
    """Speech then trailing silence → on_voice() called with the transcription."""
    ws, fusion, model = await _start_ws()
    model.set_segments([_FakeSegment("open terminal", logprob=-0.2)])

    for _ in range(3):
        ws.on_audio_chunk(SPEECH_CHUNK, _FRAMES)
    for _ in range(3):   # 2 400 samples > silence_duration_s=0.1s (1 600 samples)
        ws.on_audio_chunk(SILENCE_CHUNK, _FRAMES)

    await asyncio.sleep(0.3)
    await ws.stop()

    if not fusion.received:
        return False, "on_voice() was never called"
    cmd = fusion.received[0]
    checks = [
        (cmd.source == "voice",       f"source={cmd.source!r} (want 'voice')"),
        (cmd.text == "open terminal", f"text={cmd.text!r}"),
        (cmd.action == "DICTATE",     f"action={cmd.action!r}"),
        (cmd.whisper_logprob == -0.2, f"logprob={cmd.whisper_logprob}"),
    ]
    for ok, msg in checks:
        if not ok:
            return False, msg
    return True, f"on_voice({cmd.text!r}, logprob={cmd.whisper_logprob})"


async def test_pure_silence_not_transcribed() -> tuple[bool, str]:
    """All-silence audio never reaches Whisper."""
    ws, fusion, model = await _start_ws()
    model.set_segments([_FakeSegment("ghost text")])

    for _ in range(12):
        ws.on_audio_chunk(SILENCE_CHUNK, _FRAMES)

    await asyncio.sleep(0.3)
    await ws.stop()

    if fusion.received:
        return False, f"on_voice() called for silence: {fusion.received[0].text!r}"
    return True, "pure silence correctly discarded"


async def test_short_clip_below_min_speech_discarded() -> tuple[bool, str]:
    """Buffer shorter than min_speech_s is returned before Whisper is invoked.

    Strategy: set min_speech_s=0.5s (8 000 samples).  Feed 1 speech chunk
    (800 samples) + 3 silence chunks (2 400 samples) = 3 200 samples = 0.2s
    which is well below 0.5s.  The trailing-silence condition is satisfied but
    the min_speech guard fires first, returning without a transcribe call.
    """
    ws, fusion, model = await _start_ws(min_speech_s=0.5)

    ws.on_audio_chunk(SPEECH_CHUNK, _FRAMES)   # 800 speech samples
    for _ in range(3):                          # 2 400 silence samples
        ws.on_audio_chunk(SILENCE_CHUNK, _FRAMES)

    await asyncio.sleep(0.3)
    await ws.stop()

    if model.transcribe_calls:
        return False, f"Whisper called for sub-min_speech clip: {model.transcribe_calls}"
    return True, "sub-min_speech_s clip correctly skipped"


async def test_hard_cap_forces_flush() -> tuple[bool, str]:
    """Buffer at max_buffer_s is force-flushed even without trailing silence."""
    # max_buffer_s=0.5s → 8 000 samples; 11 speech chunks → 8 800 samples → force
    ws, fusion, model = await _start_ws(max_buffer_s=0.5)
    model.set_segments([_FakeSegment("hard cap text")])

    for _ in range(11):
        ws.on_audio_chunk(SPEECH_CHUNK, _FRAMES)   # all speech, no silence

    await asyncio.sleep(0.3)
    await ws.stop()

    if not fusion.received:
        return False, "Hard-cap flush did not call on_voice()"
    return True, f"Force-flush: {fusion.received[0].text!r}"


async def test_hotwords_in_initial_prompt() -> tuple[bool, str]:
    """update_hotwords() causes words to appear in Whisper's initial_prompt."""
    ws, fusion, model = await _start_ws()
    ws.update_hotwords(["click", "scroll", "open"])
    model.set_segments([_FakeSegment("click here")])

    for _ in range(3):
        ws.on_audio_chunk(SPEECH_CHUNK, _FRAMES)
    for _ in range(3):
        ws.on_audio_chunk(SILENCE_CHUNK, _FRAMES)

    await asyncio.sleep(0.3)
    await ws.stop()

    if not model.transcribe_calls:
        return False, "Whisper was never called"
    prompt = model.transcribe_calls[0].get("initial_prompt", "")
    for word in ("click", "scroll", "open"):
        if word not in prompt:
            return False, f"Hotword '{word}' missing from initial_prompt: {prompt!r}"
    return True, f"initial_prompt={prompt!r}"


async def test_available_false_before_start() -> tuple[bool, str]:
    """available is False before start() is called."""
    ws = WhisperStream()
    if ws.available:
        return False, "available should be False before start()"
    return True, "available=False before start() ✓"


async def test_available_true_after_start() -> tuple[bool, str]:
    """available is True after a successful start() (stop() resets it, so check before)."""
    ws, _, _ = await _start_ws()
    available_while_running = ws.available
    await ws.stop()
    if not available_while_running:
        return False, "available should be True while running (was False)"
    return True, "available=True while running ✓"


async def test_available_false_when_model_load_fails() -> tuple[bool, str]:
    """available stays False if WhisperModel.__init__ raises."""

    class _BrokenModel:
        def __init__(self, *a, **kw):
            raise RuntimeError("GPU out of memory")

    original = whisper_stream.WhisperModel
    whisper_stream.WhisperModel = _BrokenModel  # type: ignore[attr-defined]
    try:
        ws = WhisperStream()
        await ws.start()
        await ws.stop()
    finally:
        whisper_stream.WhisperModel = original  # type: ignore[attr-defined]

    if ws.available:
        return False, "available should remain False after failed model load"
    return True, "available=False after model load failure ✓"


async def test_chunks_dropped_when_unavailable() -> tuple[bool, str]:
    """on_audio_chunk() is a no-op before start() (available=False)."""
    ws = WhisperStream()
    fusion = _MockFusion()
    ws.set_fusion_engine(fusion)
    # Do NOT call start() — available stays False

    ws.on_audio_chunk(SPEECH_CHUNK, _FRAMES)
    ws.on_audio_chunk(SILENCE_CHUNK, _FRAMES)

    if ws._buffer is not None:
        return False, "Buffer should remain None when available=False"
    if fusion.received:
        return False, "on_voice() should not be called when unavailable"
    return True, "Chunks silently dropped when unavailable ✓"


async def test_multiple_segments_combined() -> tuple[bool, str]:
    """Multiple Whisper segments are joined and their logprobs averaged."""
    ws, fusion, model = await _start_ws()
    model.set_segments([
        _FakeSegment("open the",       logprob=-0.1),
        _FakeSegment("terminal please", logprob=-0.3),
    ])

    for _ in range(3):
        ws.on_audio_chunk(SPEECH_CHUNK, _FRAMES)
    for _ in range(3):
        ws.on_audio_chunk(SILENCE_CHUNK, _FRAMES)

    await asyncio.sleep(0.3)
    await ws.stop()

    if not fusion.received:
        return False, "on_voice() was never called"
    cmd = fusion.received[0]
    if cmd.text != "open the terminal please":
        return False, f"Joined text wrong: {cmd.text!r}"
    expected_logprob = (-0.1 + -0.3) / 2
    if abs(cmd.whisper_logprob - expected_logprob) > 1e-9:
        return False, f"Avg logprob wrong: {cmd.whisper_logprob} (want {expected_logprob})"
    return True, f"{cmd.text!r}  logprob={cmd.whisper_logprob:.2f}"


async def test_empty_segments_not_emitted() -> tuple[bool, str]:
    """If Whisper returns only empty-text segments, on_voice() is not called."""
    ws, fusion, model = await _start_ws()
    model.set_segments([_FakeSegment("", logprob=-0.9), _FakeSegment("  ", logprob=-0.9)])

    for _ in range(3):
        ws.on_audio_chunk(SPEECH_CHUNK, _FRAMES)
    for _ in range(3):
        ws.on_audio_chunk(SILENCE_CHUNK, _FRAMES)

    await asyncio.sleep(0.3)
    await ws.stop()

    if fusion.received:
        return False, f"on_voice() called for empty segments: {fusion.received[0].text!r}"
    return True, "Empty Whisper output correctly suppressed ✓"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def run_tests() -> int:
    tests = [
        ("VAD flush emits voice Command",          test_vad_flush_emits_voice_command),
        ("Pure silence not transcribed",           test_pure_silence_not_transcribed),
        ("Short clip below min_speech discarded",  test_short_clip_below_min_speech_discarded),
        ("Hard-cap forces flush",                  test_hard_cap_forces_flush),
        ("Hotwords in initial_prompt",             test_hotwords_in_initial_prompt),
        ("available=False before start",           test_available_false_before_start),
        ("available=True after start",             test_available_true_after_start),
        ("available=False on model load failure",  test_available_false_when_model_load_fails),
        ("Chunks dropped when unavailable",        test_chunks_dropped_when_unavailable),
        ("Multiple segments combined",             test_multiple_segments_combined),
        ("Empty segments not emitted",             test_empty_segments_not_emitted),
    ]

    passed = failed = 0
    print(f"\nWhisperStream — {len(tests)} tests\n")

    for name, fn in tests:
        try:
            ok, detail = await fn()
        except Exception as exc:
            ok, detail = False, f"EXCEPTION: {exc}"

        marker = "✓" if ok else "✗"
        print(f"  [{marker}] {name}")
        print(f"        {detail}")
        passed += ok
        failed += not ok

    print(f"\n{'─' * 55}")
    print(f"  {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


def main() -> None:
    sys.exit(asyncio.run(run_tests()))


if __name__ == "__main__":
    main()
