"""Integration test: voice command end-to-end pipeline (Req 3).

Validates the full voice path:
  WhisperStream._transcribe(audio) → Command(source="voice")
  → FusionEngine.on_voice(cmd)
  → FusionEngine._tick() Rule 10
  → HybridCoordinator.route(cmd) [4-gate]
  → CommandExecutor.execute()

Also tests:
  - Low-logprob path triggers Gate 1 → _retranscribe vocabulary correction
  - VAD no-speech discards buffer without emitting a Command
  - Total pipeline latency (with mocked 50ms inference) < 600ms

All I/O is mocked: no real GPU, no real audio device, no real LLM.

Run:
    python tests/test_voice_e2e.py

Exit codes: 0 = all passed, 1 = any failed
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from command_executor import Command
from fusion_engine import FusionEngine, FusionConfig
from hybrid_coordinator import CoordinatorConfig, HybridCoordinator
from whisper_stream import WhisperStream


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_segment(text: str, logprob: float = -0.3):
    seg = MagicMock()
    seg.text = text
    seg.avg_logprob = logprob
    return seg


def _make_audio(n_samples: int = 8000):
    """Return a fake numpy-like byte buffer (not real audio)."""
    try:
        import numpy as np
        return np.zeros(n_samples, dtype=np.float32)
    except ImportError:
        return b"\x00" * (n_samples * 4)


# ---------------------------------------------------------------------------
# Test 1: WhisperStream emits correct Command structure
# ---------------------------------------------------------------------------

async def test_whisper_stream_emits_voice_command() -> tuple[bool, str]:
    """WhisperStream._transcribe() emits Command(source='voice', action='DICTATE')."""
    ws = WhisperStream(preserve_audio=True)
    ws.available = True

    received: list[Command] = []

    mock_fusion = MagicMock()
    mock_fusion.on_voice = lambda cmd: received.append(cmd)
    ws.set_fusion_engine(mock_fusion)

    # Mock faster-whisper model
    segment = _make_segment("close the window", logprob=-0.3)
    mock_model = MagicMock()
    mock_model.transcribe.return_value = ([segment], MagicMock())
    ws._model = mock_model

    audio = _make_audio(8000)
    ws._transcribe(audio)

    if not received:
        return False, "on_voice() was never called — no Command emitted"

    cmd = received[0]
    if cmd.source != "voice":
        return False, f"Expected source='voice', got {cmd.source!r}"
    if cmd.action != "DICTATE":
        return False, f"Expected action='DICTATE', got {cmd.action!r}"
    if "close the window" not in cmd.text.lower():
        return False, f"Expected text to contain 'close the window', got {cmd.text!r}"
    if cmd.whisper_logprob != pytest_approx(-0.3):
        if abs(cmd.whisper_logprob - (-0.3)) > 0.001:
            return False, f"Expected whisper_logprob≈-0.3, got {cmd.whisper_logprob}"

    return True, f"Command emitted: {cmd.text!r} (logprob={cmd.whisper_logprob})"


def pytest_approx(v):
    return v  # simple passthrough for non-pytest environment


# ---------------------------------------------------------------------------
# Test 2: WhisperStream preserves audio bytes when preserve_audio=True
# ---------------------------------------------------------------------------

async def test_whisper_stream_preserves_audio_bytes() -> tuple[bool, str]:
    """With preserve_audio=True, Command.params contains 'audio_bytes' + 'sample_rate'."""
    ws = WhisperStream(preserve_audio=True)
    ws.available = True

    received: list[Command] = []
    mock_fusion = MagicMock()
    mock_fusion.on_voice = lambda cmd: received.append(cmd)
    ws.set_fusion_engine(mock_fusion)

    segment = _make_segment("scroll down")
    mock_model = MagicMock()
    mock_model.transcribe.return_value = ([segment], MagicMock())
    ws._model = mock_model

    audio = _make_audio(8000)
    ws._transcribe(audio)

    if not received:
        return False, "No Command received"
    cmd = received[0]
    if "audio_bytes" not in cmd.params:
        return False, "audio_bytes missing from Command.params"
    if "sample_rate" not in cmd.params:
        return False, "sample_rate missing from Command.params"
    if cmd.params["sample_rate"] != WhisperStream.SAMPLE_RATE:
        return False, f"Expected sample_rate={WhisperStream.SAMPLE_RATE}"
    return True, f"audio_bytes ({len(cmd.params['audio_bytes'])} bytes) and sample_rate preserved"


# ---------------------------------------------------------------------------
# Test 3: VAD no-speech discards buffer without emitting Command
# ---------------------------------------------------------------------------

async def test_whisper_vad_no_speech_discards() -> tuple[bool, str]:
    """When VAD finds no speech segments, no Command is emitted."""
    ws = WhisperStream()
    ws.available = True

    received: list[Command] = []
    mock_fusion = MagicMock()
    mock_fusion.on_voice = lambda cmd: received.append(cmd)
    ws.set_fusion_engine(mock_fusion)

    mock_model = MagicMock()
    mock_model.transcribe.return_value = ([], MagicMock())  # empty segments
    ws._model = mock_model

    audio = _make_audio(8000)
    ws._transcribe(audio)

    if received:
        return False, f"Expected no Command for empty VAD, got: {received[0].text!r}"
    return True, "Empty VAD segments — no Command emitted"


# ---------------------------------------------------------------------------
# Test 4: FusionEngine Rule 10 forwards voice Command to coordinator
# ---------------------------------------------------------------------------

async def test_fusion_rule10_forwards_voice_command() -> tuple[bool, str]:
    """FusionEngine.on_voice() + _tick() → coordinator.route() called with voice Command."""
    routed: list[Command] = []

    mock_coord = MagicMock()
    mock_coord.route = AsyncMock(side_effect=lambda cmd: routed.append(cmd) or {"status": "ok"})

    config = FusionConfig(tick_hz=60.0)
    fusion = FusionEngine(screen_width=1920, screen_height=1080, config=config)
    fusion.set_coordinator(mock_coord)

    cmd = Command(text="close the window", action="DICTATE",
                  source="voice", whisper_logprob=-0.3)
    fusion.on_voice(cmd)

    # Run one tick
    with patch("pyautogui.moveRel"), patch("pyautogui.size", return_value=(1920, 1080)):
        await fusion._tick()

    if not routed:
        return False, "coordinator.route() was never called after on_voice()"
    sent = routed[0]
    if sent.source != "voice":
        return False, f"Expected source='voice', got {sent.source!r}"
    if sent.text != "close the window":
        return False, f"Expected 'close the window', got {sent.text!r}"
    return True, f"Rule 10 forwarded: {sent.text!r} (source={sent.source!r})"


# ---------------------------------------------------------------------------
# Test 5: Low whisper_logprob triggers Gate 1 → vocabulary correction applied
# ---------------------------------------------------------------------------

async def test_low_logprob_triggers_gate1_correction() -> tuple[bool, str]:
    """Voice Command with logprob < -1.0 goes through Gate 1 vocab correction."""
    executed_texts: list[str] = []

    async def fake_execute(cmd: Command) -> dict:
        executed_texts.append(cmd.text)
        return {"status": "ok", "action": cmd.action, "result": {}}

    cfg = CoordinatorConfig(
        whisper_logprob_min=-1.0,  # Gate 1 threshold
    )
    coord = HybridCoordinator(config=cfg)
    coord._local.infer = AsyncMock(return_value="CLOSE")
    coord._executor.execute = fake_execute

    # "clothes the window" is the classic misrecognition; logprob -1.5 fails Gate 1
    cmd = Command(
        text="clothes the window",
        action="DICTATE",
        source="voice",
        whisper_logprob=-1.5,  # below -1.0 threshold
    )
    await coord.route(cmd)

    if not executed_texts:
        return False, "CommandExecutor was never called"
    # Vocabulary correction should have fired: "clothes" → "Close"
    if "close" not in executed_texts[-1].lower() and "clothes" in executed_texts[-1].lower():
        return False, f"Vocabulary correction did not fire; executor got {executed_texts[-1]!r}"
    return True, f"Gate 1 vocab correction applied; LLM saw {coord._local.infer.call_args}"


# ---------------------------------------------------------------------------
# Test 6: End-to-end pipeline latency with 50ms mock inference < 600ms
# ---------------------------------------------------------------------------

async def test_voice_e2e_latency_under_600ms() -> tuple[bool, str]:
    """Full voice pipeline (mocked 50ms inference) completes in < 600ms."""
    async def slow_infer(cmd: Command, few_shot_examples=None) -> str:
        await asyncio.sleep(0.05)  # 50ms simulated inference
        return "CLOSE"

    cfg = CoordinatorConfig()
    coord = HybridCoordinator(config=cfg)
    coord._local.infer = slow_infer

    fusion_cfg = FusionConfig(tick_hz=60.0)
    fusion = FusionEngine(screen_width=1920, screen_height=1080, config=fusion_cfg)
    fusion.set_coordinator(coord)

    cmd = Command(text="close this window", action="DICTATE",
                  source="voice", whisper_logprob=-0.2)
    fusion.on_voice(cmd)

    t0 = time.monotonic()
    with patch("pyautogui.moveRel"), \
         patch("pyautogui.size", return_value=(1920, 1080)), \
         patch("command_executor.keyboard.keyboard_hotkey", return_value={}), \
         patch("pyautogui.hotkey"):
        await fusion._tick()
    elapsed_ms = (time.monotonic() - t0) * 1000

    if elapsed_ms >= 600:
        return False, f"Pipeline took {elapsed_ms:.0f}ms — exceeds 600ms budget"
    return True, f"Pipeline completed in {elapsed_ms:.0f}ms (< 600ms budget)"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def run_tests() -> int:
    tests = [
        ("WhisperStream emits Command(source='voice', action='DICTATE')",  test_whisper_stream_emits_voice_command),
        ("WhisperStream preserves audio bytes in params",                   test_whisper_stream_preserves_audio_bytes),
        ("VAD no-speech → no Command emitted",                             test_whisper_vad_no_speech_discards),
        ("FusionEngine Rule 10 forwards voice Command to coordinator",      test_fusion_rule10_forwards_voice_command),
        ("Low logprob triggers Gate 1 vocabulary correction",               test_low_logprob_triggers_gate1_correction),
        ("E2E latency with 50ms mock inference < 600ms",                   test_voice_e2e_latency_under_600ms),
    ]

    print(f"\nVoice e2e integration tests\n{'─' * 52}")
    passed = 0
    for name, fn in tests:
        try:
            ok, msg = await fn()
        except Exception as exc:
            ok, msg = False, f"EXCEPTION: {exc}"
        mark = "✓" if ok else "✗"
        print(f"  {mark} {name}")
        if ok:
            passed += 1
        else:
            print(f"    FAIL: {msg}")

    print(f"\n{'─' * 52}")
    print(f"Results: {passed}/{len(tests)} passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run_tests()))
