"""Voice approval gate — Claude Code PreToolUse hook.

Claude Code calls this before executing any tool that is listed as "approve"
in approval_config.json.  The script:

  1. Reads the tool name + input from stdin (JSON from Claude Code).
  2. Checks approval_config.json — if the tool is "silent", exits 0 immediately.
  3. Builds a short spoken description of the action.
  4. Calls Amazon Polly (Danielle neural) to speak it aloud.
  5. Creates an exact request bound to tool name/input and opens the voice/A2UI
     responder window.
  6. Falls back to the PC mic only when no exact remote response arrives.
  7. Publishes ``{request_id, action_digest, decision, responded_at}``.
  8. Allows only when ApprovalAuthority consumes the matching one-use grant.

Configuration: approval_config.json (same directory as this file).

Usage (invoked by Claude Code hook — do not call directly):
    <stdin>  JSON: {tool_use_id, tool_name, tool_input}
    <stdout> On rejection: JSON {decision:"block", reason:"..."}
    <exit>   0 = allow, 2 = block
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

# Shared strict approval store. Responders use per-request markers and response
# files; the legacy global pending/response/prompt files are intentionally absent.
_APPROVAL_DIR = Path.home() / ".claude" / "approval"

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
    from core.approval_authority import (
        ApprovalAuthority,
        ApprovalRequest,
        ApprovalResponse,
        action_digest,
    )
    from core.approval_keywords import classify_confirmation
except Exception:  # pragma: no cover - defensive import guard
    ApprovalAuthority = ApprovalRequest = ApprovalResponse = None  # type: ignore[assignment]
    action_digest = None  # type: ignore[assignment]
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


def _try_audit_vibe_unavailable(exc: Exception) -> None:
    """Best-effort: write a warning to audit.db so the approval trail records
    that LLM feedback (vibe diff) was absent when the user approved the action.

    Uses asyncio.run() for a one-shot async write — runs in under ~5 ms when
    aiosqlite is available; no-ops silently on any failure so the approval flow
    is never blocked by this observability write.
    """
    try:
        import asyncio as _aio
        from storage.audit_log import AuditLog as _AL

        async def _write() -> None:
            _a = _AL()
            await _a.open(_DIR / "audit.db")
            await _a.log(
                "vibe_summary_unavailable",
                severity="warning",
                detail=str(exc)[:200],
                params={"fallback": "static_prompt"},
            )
            await _a.close()

        _aio.run(_write())
    except Exception as _ae:
        log.debug("approval_hook: audit for vibe_summary_unavailable failed: %s", _ae)


def _vibe_summary(tool_name: str, tool_input: dict, config: dict) -> str | None:
    """Plain-English "Vibe Diff" of a pending action (GAP-2 — Pillar 5).

    For the high-impact tools listed in approval_config.json ``vibe_diff_tools``,
    ask a fast local LLM to describe in one sentence what the command will DO to
    the user's system, so the spoken consent prompt conveys *intent* rather than
    a raw command string. Best-effort and fail-open: any failure (Ollama down,
    >3s timeout, malformed reply, key absent) returns None and the caller falls
    back to the static description — the gate is never blocked or slowed beyond
    the timeout. Uses stdlib urllib so the hook gains no new dependency.
    """
    vibe_tools = config.get("vibe_diff_tools") or []
    if tool_name not in vibe_tools:
        return None

    # Pull the most action-bearing text for each tool type.
    if tool_name in ("Bash", "PowerShell"):
        detail = tool_input.get("command", "")
    elif tool_name in ("Write", "Edit"):
        fp = tool_input.get("file_path", "")
        body = tool_input.get("content") or tool_input.get("new_string") or ""
        detail = f"write to file {fp}: {body[:800]}"
    else:
        detail = json.dumps(tool_input)[:800]
    detail = (detail or "").strip()
    if not detail:
        return None

    model = config.get("vibe_diff_model", "llama3.1:8b")
    prompt = (
        "In one short sentence, plainly describe what this action will DO to the "
        "user's computer. Be concrete and non-technical. Do not add warnings, "
        "preamble, or quotes.\n\nACTION:\n" + detail
    )
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.0, "num_predict": 60},
    }).encode("utf-8")

    try:
        import urllib.request
        req = urllib.request.Request(
            "http://localhost:11434/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            out = json.loads(resp.read().decode("utf-8"))
        text = (out.get("response") or "").strip()
    except Exception as exc:  # noqa: BLE001 - best-effort, fail open
        log.warning("approval_hook: vibe summary unavailable: %s (falling back to static prompt)", exc)
        _try_audit_vibe_unavailable(exc)
        return None

    if not text:
        return None
    # Collapse to a single line and cap length so TTS stays snappy.
    # Return just the plain-English effect — the caller PREPENDS it to the static
    # identity-bearing prompt ("Approve running rm?") rather than replacing it, so
    # a benign-sounding paraphrase of a destructive command can never hide the
    # exe/target the user is consenting to.
    return " ".join(text.split())[:240]


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


def _request_ipad_approval(authority, request, timeout_s: float = 7.0) -> bool:
    """Expose one exact request to Whisper/A2UI and await its response file."""
    opened = False
    try:
        authority.publish_pending_request(request)
        opened = True
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if authority.response_path(request.request_id).exists():
                return True
            time.sleep(0.1)
        return False
    finally:
        if opened:
            authority.close_pending_request(request)


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

    # Build the immutable authority material before exposing any responder UI.
    message = _build_message(tool_name, tool_input)
    vibe = _vibe_summary(tool_name, tool_input, config)
    if vibe:
        message = f"{vibe} {message}"
    if ApprovalAuthority is None or ApprovalResponse is None or action_digest is None:
        sys.exit(2)
    trace_id = data.get("tool_use_id")
    if not isinstance(trace_id, str):
        trace_id = ""
    try:
        digest = action_digest(
            action=tool_name,
            trace_id=trace_id,
            goal="local approval hook",
            security_args=tool_input,
            approval_context="local_hook",
        )
        authority = ApprovalAuthority(_APPROVAL_DIR)
        request = authority.create_request(
            trace_id=trace_id,
            action_digest=digest,
            description=message,
        )
        authority.open_response_window(request)
    except Exception as exc:
        log.info("approval_hook: could not create exact request (%s) — DENY", exc)
        sys.exit(2)

    voice = config.get("voice_id", "Danielle")
    _polly_speak(message, voice)

    responded = _request_ipad_approval(authority, request, timeout_s=7.0)
    response_window_sealed = False
    try:
        authority.close_response_window(request)
        response_window_sealed = True
    except Exception as exc:
        log.info("approval_hook: could not seal responders (%s) — DENY", exc)
    responded = responded or authority.response_path(request.request_id).exists()
    transcript = ""
    if not responded and response_window_sealed:
        log.debug("approval_hook: iPad path timed out, falling back to PC mic")
        record_s: float = float(config.get("record_s", 4.0))
        device = config.get("device")
        try:
            audio = _record(record_s, device=device)
            if _has_voice(audio):
                transcript = _transcribe(audio)
        except Exception:
            transcript = ""
        verdict = classify_confirmation(transcript)
        if verdict in ("approve", "deny"):
            try:
                authority.write_response(ApprovalResponse(
                    request_id=request.request_id,
                    action_digest=request.action_digest,
                    decision=verdict,
                    responded_at=time.time(),
                ))
            except Exception as exc:
                log.info("approval_hook: exact response rejected (%s)", exc)

    try:
        grant = authority.consume_response(request, approved_digests=(digest,))
        approved = authority.claim_grant(grant, digest)
    except Exception as exc:
        log.info("approval_hook: authority failed (%s) — DENY", exc)
        approved = False

    if approved:
        sys.exit(0)
    reason = f"Voice rejected: '{transcript}'" if transcript else "No clear approval heard"
    sys.stdout.write(json.dumps({"decision": "block", "reason": reason}) + "\n")
    sys.exit(2)


if __name__ == "__main__":
    main()
