"""Voice approval gate — Claude Code PreToolUse hook.

Claude Code calls this before executing any tool that is listed as "approve"
in approval_config.json.  The script:

  1. Reads the tool name + input from stdin (JSON from Claude Code).
  2. Checks approval_config.json — if the tool is "silent", exits 0 immediately.
  3. Builds a short spoken description of the action.
  4. Calls Amazon Polly (Danielle neural) to speak it aloud.
  5. Records up to `record_s` seconds of audio from the default mic.
  6. Detects voice activity — if silence the whole time, auto-approves.
  7. Transcribes with faster-whisper "tiny" (CPU, loads in ~1s).
  8. Parses yes/no keywords and exits:
       0 → approved (or silence with timeout_action="approve")
       2 → rejected  (user said no/cancel/stop)

Configuration: approval_config.json (same directory as this file).

Usage (invoked by Claude Code hook — do not call directly):
    <stdin>  JSON: {tool_use_id, tool_name, tool_input}
    <stdout> On rejection: JSON {decision:"block", reason:"..."}
    <exit>   0 = allow, 2 = block
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# Must match _APPROVAL_DIR in whisper_stream.py
_APPROVAL_DIR = Path.home() / ".claude" / "approval"
_PENDING_FILE  = _APPROVAL_DIR / "pending"
_RESPONSE_FILE = _APPROVAL_DIR / "response"
# Spoken action description, persisted alongside "pending" so the running agent
# can render an A2UI Approve/Deny surface on the iPad (parallel to voice).
_PROMPT_FILE   = _APPROVAL_DIR / "prompt"

import logging
import numpy as np

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SAMPLE_RATE = 16_000
_DIR = Path(__file__).parent

# Shared confirmation vocabulary — single source of truth in core/ so this hook
# and sensors/whisper_stream.py parse identical yes/no language. Falls back to a
# minimal inline classifier if core isn't importable (hook must never crash on
# import), defaulting unknown text to None so the gate fails safe to DENY.
try:
    sys.path.insert(0, str(_DIR))
    from core.approval_keywords import classify_confirmation
except Exception:  # pragma: no cover - defensive import guard
    _APPROVE = {"yes", "yeah", "yep", "ok", "okay", "approve", "confirm", "sure"}
    _REJECT = {"no", "nope", "stop", "cancel", "deny", "reject", "abort", "dont"}

    def classify_confirmation(text: str):  # type: ignore[misc]
        words = {w.strip(".,!?'\"").replace("'", "") for w in (text or "").lower().split()}
        if words & _REJECT:
            return "deny"
        if words & _APPROVE:
            return "approve"
        return None


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    path = _DIR / "approval_config.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        # Fail safe: if the config can't be read, default to denying on timeout
        # so a destructive tool call is never auto-approved on silence/garbage.
        return {"tools": {}, "timeout_action": "reject", "record_s": 4.0}


def _needs_approval(tool_name: str, config: dict) -> bool:
    return config.get("tools", {}).get(tool_name, "silent") == "approve"


# ---------------------------------------------------------------------------
# Message builder
# ---------------------------------------------------------------------------

def _build_message(tool_name: str, tool_input: dict) -> str:
    """Return a short spoken description of the pending action."""
    folder = lambda p: Path(p).parent.name if p else "a folder"

    if tool_name in ("Edit", "Write"):
        return f"Approve write to {folder(tool_input.get('file_path'))}?"
    if tool_name in ("Bash", "PowerShell"):
        # First word of the command (the executable) gives enough context
        raw = tool_input.get("command", "a command").strip().split()[0]
        return f"Approve running {raw}?"
    if tool_name == "Agent":
        return "Approve launching a sub-agent?"
    if tool_name == "computer":
        return "Approve browser control?"
    return f"Approve {tool_name}?"


# ---------------------------------------------------------------------------
# Polly TTS
# ---------------------------------------------------------------------------

def _polly_speak(text: str, voice_id: str = "Danielle") -> None:
    """Speak text via Polly, block until speech finishes, then return."""
    try:
        import boto3
        import sounddevice as sd
        from botocore.config import Config

        cfg = Config(connect_timeout=5, read_timeout=5)
        polly = boto3.client("polly", region_name="us-east-1", config=cfg)
        resp = polly.synthesize_speech(
            Text=text,
            OutputFormat="pcm",
            SampleRate="16000",
            VoiceId=voice_id,
            Engine="neural",
            LanguageCode="en-US",
        )
        audio = (
            np.frombuffer(resp["AudioStream"].read(), dtype=np.int16)
            .astype(np.float32) / 32768.0
        )
        duration_s = len(audio) / SAMPLE_RATE
        sd.play(audio, samplerate=SAMPLE_RATE)
        deadline = time.monotonic() + duration_s + 1.5
        while sd.get_stream() and sd.get_stream().active:
            if time.monotonic() > deadline:
                sd.stop()
                break
            time.sleep(0.05)
    except Exception:
        # Polly unavailable — still run the approval flow silently
        time.sleep(1.0)


# ---------------------------------------------------------------------------
# Voice capture + transcription
# ---------------------------------------------------------------------------

def _record(seconds: float, device=None) -> "np.ndarray":
    """Record `seconds` of mono audio.

    `device` may be a device index or substring of a device name.
    Pass None to use the OS default input device.
    Set 'device' in approval_config.json to override (e.g. "Microphone Array").
    """
    import sounddevice as sd
    buf = sd.rec(int(seconds * SAMPLE_RATE), samplerate=SAMPLE_RATE,
                 channels=1, dtype="float32", device=device)
    sd.wait()
    return buf.flatten()


def _has_voice(audio: "np.ndarray", threshold: float = 0.005) -> bool:
    """Return True if the recording contains any audible speech (non-silence)."""
    return float(np.sqrt(np.mean(audio ** 2))) >= threshold


def _transcribe(audio: "np.ndarray") -> str:
    """Transcribe using faster-whisper tiny (CPU, ~1s cold start)."""
    try:
        from faster_whisper import WhisperModel
        model = WhisperModel("tiny", device="cpu", compute_type="int8")
        segments, _ = model.transcribe(audio, language="en", beam_size=1,
                                       vad_filter=True)
        return " ".join(s.text.strip() for s in segments).lower()
    except Exception:
        return ""


def _request_ipad_approval(timeout_s: float = 7.0, prompt: str = "") -> str | None:
    """Signal WhisperStream to intercept the next iPad utterance for approval.

    Writes a pending marker so _transcribe() reroutes the next voice segment
    here instead of to FusionEngine.  Returns the transcript or None on timeout
    (bridge not running or user stayed silent).
    """
    _APPROVAL_DIR.mkdir(parents=True, exist_ok=True)
    _RESPONSE_FILE.unlink(missing_ok=True)          # clear any stale response
    # Write the prompt BEFORE pending, so the agent sees a complete description
    # the instant it detects the not-open → open transition.
    try:
        _PROMPT_FILE.write_text(prompt or "", encoding="utf-8")
    except Exception:
        pass
    _PENDING_FILE.write_text(str(time.time()), encoding="utf-8")

    deadline = time.monotonic() + timeout_s
    try:
        while time.monotonic() < deadline:
            if _RESPONSE_FILE.exists():
                # utf-8-sig silently strips BOM (safe for both WhisperStream
                # plain-UTF-8 writes and PowerShell BOM-UTF-8 test writes)
                transcript = _RESPONSE_FILE.read_text(encoding="utf-8-sig").strip()
                return transcript
            time.sleep(0.1)
        return None  # timeout — bridge not running or no speech detected
    finally:
        # Always clean up so a stale pending file doesn't block future commands
        _PENDING_FILE.unlink(missing_ok=True)
        _RESPONSE_FILE.unlink(missing_ok=True)
        _PROMPT_FILE.unlink(missing_ok=True)


def _parse_response(transcript: str, default: str = "reject") -> bool:
    """Return True (approved) or False (rejected) from the transcript.

    Only a deliberate confirmation word approves. Ambiguous or unrecognised text
    fails safe to the configured default — which is "reject" (deny) so that
    ambient audio / garbage / silence can never grant consent.
    """
    verdict = classify_confirmation(transcript)
    if verdict == "approve":
        return True
    if verdict == "deny":
        return False
    # Nothing recognised → fall back to configured default (deny unless the
    # operator has explicitly opted into approve-on-ambiguity).
    return default == "approve"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    config = _load_config()

    # --- Parse Claude Code's stdin payload -----------------------------------
    try:
        # Read raw bytes; use utf-8-sig to silently strip BOM if present
        # (PowerShell Out-File adds a BOM; Claude Code sends plain UTF-8 — both work)
        raw_bytes = sys.stdin.buffer.read()
        for enc in ("utf-8-sig", "utf-16", "latin-1"):
            try:
                raw = raw_bytes.decode(enc)
                json.loads(raw) if raw.strip() else {}  # validate parse
                break
            except Exception:
                continue
        else:
            raw = ""
        data: dict = json.loads(raw) if raw.strip() else {}
    except Exception:
        sys.exit(0)  # can't parse → auto-approve

    tool_name: str = data.get("tool_name", "")
    tool_input: dict = data.get("tool_input", {})

    if not _needs_approval(tool_name, config):
        sys.exit(0)

    # --- Goal-session fast-path: silent auto-approve under an authorized goal --
    try:
        from core.goal_session import GoalSessionStore
        _gs = GoalSessionStore.get_active()
        if _gs and _gs.allows_action(tool_name, tool_input):
            GoalSessionStore.consume()
            log.info("approval_hook: auto-approved %r under goal session %r",
                     tool_name, _gs.goal[:60])
            sys.exit(0)
    except Exception as _gs_exc:
        log.debug("approval_hook: goal session check failed: %s", _gs_exc)

    # --- Speak the action description ----------------------------------------
    message = _build_message(tool_name, tool_input)
    voice = config.get("voice_id", "Danielle")
    _polly_speak(message, voice)

    # Fail safe: ambiguity, silence, and timeout default to DENY. Background
    # audio / the TTS echo / a stray word must never silently approve.
    timeout_action: str = config.get("timeout_action", "reject")

    # --- Prefer iPad mic via WhisperStream (bridge must be running) -----------
    # Signal the bridge; if it responds within 7s the iPad utterance is used
    # and not forwarded to FusionEngine (so "yes"/"no" won't trigger a command).
    transcript = _request_ipad_approval(timeout_s=7.0, prompt=message)

    if transcript is None:
        # Bridge not running or no speech — fall back to PC microphone
        log.debug("approval_hook: iPad path timed out, falling back to PC mic")
        record_s: float = float(config.get("record_s", 4.0))
        device = config.get("device")
        try:
            audio = _record(record_s, device=device)
        except Exception:
            sys.exit(0 if timeout_action == "approve" else 2)

        if not _has_voice(audio):
            sys.exit(0 if timeout_action == "approve" else 2)

        transcript = _transcribe(audio)

    approved = _parse_response(transcript, timeout_action)

    if approved:
        sys.exit(0)
    else:
        reason = f"Voice rejected: '{transcript}'" if transcript else "No clear approval heard"
        sys.stdout.write(json.dumps({"decision": "block", "reason": reason}) + "\n")
        sys.exit(2)


if __name__ == "__main__":
    main()
