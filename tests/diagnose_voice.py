"""Voice pipeline diagnostics — run while main.py is NOT running.

Checks each stage of the voice pipeline independently and reports exactly
where it breaks. Run from the project root:

    python tests/diagnose_voice.py

Exit codes:
    0 — all stages pass
    1 — one or more stages fail
"""

from __future__ import annotations

import asyncio
import base64
import sys
import time
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "mcp_server"))

PASS = "[OK]"
FAIL = "[FAIL]"
WARN = "[WARN]"

results: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, note: str = "") -> None:
    results.append((label, ok, note))
    marker = PASS if ok else FAIL
    print(f"  {marker}  {label}", f"— {note}" if note else "")


# ---------------------------------------------------------------------------
# Stage 1 — Dependencies
# ---------------------------------------------------------------------------

print("\n-- Stage 1: Dependencies -----------------------------------------")

try:
    import numpy as np
    check("numpy", True, np.__version__)
except ImportError as e:
    check("numpy", False, str(e))

try:
    from faster_whisper import WhisperModel
    check("faster-whisper", True, "importable")
except ImportError as e:
    check("faster-whisper", False, f"pip install faster-whisper  ({e})")

try:
    import torch
    cuda_ok = torch.cuda.is_available()
    check("CUDA (torch)", cuda_ok, f"device={torch.cuda.get_device_name(0)}" if cuda_ok else "CPU only — transcription will be slow")
except ImportError:
    check("CUDA (torch)", False, "torch not installed — whisper will try cuda anyway")


# ---------------------------------------------------------------------------
# Stage 2 — Whisper model load
# ---------------------------------------------------------------------------

print("\n-- Stage 2: Whisper model load -----------------------------------")

model = None
try:
    import numpy as np
    from faster_whisper import WhisperModel
    t0 = time.monotonic()
    print("   Loading large-v3 on CUDA (may take 10–30s on first load) ...")
    model = WhisperModel("large-v3", device="cuda", compute_type="float16")
    elapsed = time.monotonic() - t0
    check("WhisperModel large-v3 cuda", True, f"{elapsed:.1f}s load time")
except Exception as exc:
    check("WhisperModel large-v3 cuda", False, str(exc))
    print(f"\n   {WARN} Trying CPU fallback ...")
    try:
        model = WhisperModel("large-v3", device="cpu", compute_type="int8")
        check("WhisperModel large-v3 cpu (fallback)", True, "SLOW — fix CUDA for production")
    except Exception as exc2:
        check("WhisperModel large-v3 cpu (fallback)", False, str(exc2))


# ---------------------------------------------------------------------------
# Stage 3 — Transcription with synthetic speech audio
# ---------------------------------------------------------------------------

print("\n-- Stage 3: Transcription with 440 Hz sine tone -----------------")
# Note: a sine tone won't produce meaningful text but it tests the pipeline.
# If Silero VAD filters it out, that's expected — we check for no crash.

if model is not None:
    try:
        sr = 16_000
        duration = 2.0  # seconds
        t = np.linspace(0, duration, int(sr * duration), endpoint=False)
        # 440 Hz tone — Silero VAD will likely reject this as non-speech (correct)
        tone = (np.sin(2 * np.pi * 440 * t) * 0.5).astype(np.float32)

        segments_iter, info = model.transcribe(
            tone,
            language="en",
            beam_size=5,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 600},
        )
        segments = list(segments_iter)
        if not segments:
            check("Transcription pipeline", True, "VAD correctly rejected non-speech sine tone")
        else:
            text = " ".join(s.text.strip() for s in segments)
            check("Transcription pipeline", True, f"transcribed: {text!r}")
    except Exception as exc:
        check("Transcription pipeline", False, str(exc))
else:
    check("Transcription pipeline", False, "model not loaded — skipped")


# ---------------------------------------------------------------------------
# Stage 4 — Ollama LLM (command classification)
# ---------------------------------------------------------------------------

print("\n-- Stage 4: Ollama LLM (command classification) -----------------")

async def _test_ollama():
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            # Check tags first
            async with session.get("http://localhost:11434/api/tags", timeout=aiohttp.ClientTimeout(total=5)) as r:
                data = await r.json()
            models = [m["name"] for m in data.get("models", [])]
            if not models:
                check("Ollama server", False, "running but no models pulled — run: ollama pull llama3.1:8b")
                return

            llama = next((m for m in models if "llama3.1:8b" in m), models[0])
            check("Ollama server", True, f"{len(models)} model(s) — using {llama}")

            # Quick inference test
            body = {
                "model": llama,
                "prompt": "System: Reply with ONE action verb only.\nUser: click the save button\nAssistant:",
                "stream": False,
                "options": {"num_predict": 8, "temperature": 0},
            }
            t0 = time.monotonic()
            async with session.post(
                "http://localhost:11434/api/generate",
                json=body,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as r:
                result = await r.json()
            elapsed_ms = (time.monotonic() - t0) * 1000
            response = result.get("response", "").strip()
            check("Ollama inference", True, f"{elapsed_ms:.0f}ms -> {response!r}")
    except aiohttp.ClientConnectorError:
        check("Ollama server", False, "not running — start with: ollama serve")
    except Exception as exc:
        check("Ollama server", False, str(exc))

asyncio.run(_test_ollama())


# ---------------------------------------------------------------------------
# Stage 5 — Full pipeline injection (WhisperStream → FusionEngine → Coordinator)
# ---------------------------------------------------------------------------

print("\n-- Stage 5: End-to-end pipeline injection ------------------------")

async def _test_pipeline():
    try:
        from core.command_executor import Command
        from sensors.whisper_stream import WhisperStream
        from core.fusion_engine import FusionEngine

        received: list[Command] = []

        # Stub coordinator that captures commands instead of executing them
        class _CaptureCoordinator:
            async def route(self, cmd: Command) -> dict:
                received.append(cmd)
                return {"status": "ok", "action": "CLICK", "captured": True}

        fusion = FusionEngine(screen_width=1920, screen_height=1080)
        fusion.set_coordinator(_CaptureCoordinator())

        ws = WhisperStream()
        ws.set_fusion_engine(fusion)
        await ws.start()

        if not ws.available:
            check("WhisperStream.available after start()", False,
                  "model failed to load — check Stage 2 errors above")
            return

        check("WhisperStream.available after start()", True)

        # Inject the transcription result directly (bypasses audio → VAD path)
        from core.command_executor import Command as Cmd
        test_cmd = Cmd(
            text="click the submit button",
            action="DICTATE",
            source="voice",
            whisper_logprob=-0.3,
        )
        fusion.on_voice(test_cmd)

        # Run one FusionEngine tick, then yield to let the fire-and-forget task run
        await fusion._tick()
        await asyncio.sleep(0.05)

        if received:
            check("FusionEngine -> Coordinator routing", True,
                  f"received command text={received[0].text!r}")
        else:
            check("FusionEngine -> Coordinator routing", False,
                  "command was not delivered - FusionEngine tick may not be running")

        await ws.stop()

    except Exception as exc:
        check("End-to-end pipeline injection", False, str(exc))
        import traceback
        traceback.print_exc()

asyncio.run(_test_pipeline())


# ---------------------------------------------------------------------------
# Stage 6 — Audio chunk flow (base64 decode + buffer accumulation)
# ---------------------------------------------------------------------------

print("\n-- Stage 6: Audio chunk decode (simulates iPad audio_stream msg) -")

try:
    import numpy as np
    from sensors.whisper_stream import WhisperStream

    ws2 = WhisperStream.__new__(WhisperStream)
    ws2._preserve_audio = False
    ws2._buffer_chunks = []
    ws2._buffer_start_ts = None
    ws2.available = True  # bypass availability check
    ws2._silence_thresh = 0.008
    ws2._silence_s = 0.6
    ws2._min_speech_s = 0.3
    ws2._max_buffer_s = 30.0
    ws2._poll_s = 0.15

    # Simulate a 100ms 16kHz int16 audio chunk (1600 samples)
    samples = (np.sin(np.linspace(0, np.pi * 2, 1600)) * 8000).astype(np.int16)
    b64 = base64.b64encode(samples.tobytes()).decode()
    ws2.on_audio_chunk(b64, 1600)

    buf_s = sum(len(c) for c in ws2._buffer_chunks) / 16000
    check("Audio chunk decode + buffer", len(ws2._buffer_chunks) == 1, f"{buf_s:.3f}s buffered")
except Exception as exc:
    check("Audio chunk decode + buffer", False, str(exc))


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

print("\n-- Summary -------------------------------------------------------")
passed = sum(1 for _, ok, _ in results if ok)
failed = sum(1 for _, ok, _ in results if not ok)
print(f"  {passed} passed  /  {failed} failed\n")

if failed:
    print("  Fix the FAIL items above, then re-run. If all pass here but voice")
    print("  still doesn't work in main.py, run with --debug and look for:")
    print("    'WhisperStream: transcribing'   — audio is being processed")
    print("    'WhisperStream: <text>'         — transcript produced")
    print("    'HybridCoordinator.route'       — command routed")
    print("    'Executed CLICK'                — action fired\n")
    sys.exit(1)

sys.exit(0)
