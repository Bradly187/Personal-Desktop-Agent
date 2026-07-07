"""Startup schema validation for approval_config.json (IG-9, 2026-07-05 audit).

approval_config.json encodes the per-tool voice-approval policy — the
safety-critical config. Before this module, it was loaded as a raw dict in
three places (approval_hook, hybrid_coordinator, polly_stream), all tolerant:
a typo'd key or wrong-typed value silently became default behavior.

Contract (mirrors core/flags.py, stdlib-only, no pydantic):

- ``validate_approval_config(cfg)`` returns ``(errors, warnings)``.
- *Errors* are approval-affecting problems: unknown top-level keys (the typo
  catch), a non-dict ``tools`` section, tool policies other than
  ``approve``/``silent``, or an invalid ``timeout_action``. main.py refuses
  to start on errors — a misspelled policy key must never boot into
  weaker-than-intended gating.
- *Warnings* are cosmetic (wrong-typed TTS/voice fields); logged, never fatal.
- Keys starting with ``_`` are documentation and ignored.

The runtime loaders stay tolerant (fail-safe to DENY per AGENTS.md #4); this
check runs once at boot so problems surface loudly instead of silently.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_VALID_POLICIES = {"approve", "silent"}
_VALID_TIMEOUT_ACTIONS = {"reject", "approve"}
_VALID_TTS_BACKENDS = {"kokoro", "polly", "sapi"}

# Known top-level keys → expected type(s). Anything else (not "_"-prefixed)
# is an error: it is either a typo of one of these or dead config.
_KNOWN_KEYS: dict[str, type | tuple[type, ...]] = {
    "tools": dict,
    "tts_backend": str,
    "kokoro_voice": str,
    "kokoro_speed": (int, float),
    "sapi_rate": (int, float),
    "sapi_voice": str,
    "voice_id": str,
    "record_s": (int, float),
    "timeout_action": str,
    "device": str,
    "vibe_diff_tools": list,
    "vibe_diff_model": str,
    "goal_session_duration_s": (int, float),
    "goal_session_max_actions": int,
    "cloud_call_budget": int,
}

_APPROVAL_CRITICAL = {"tools", "timeout_action", "vibe_diff_tools"}


def validate_approval_config(cfg: dict) -> tuple[list[str], list[str]]:
    """Return (errors, warnings) for a parsed approval_config.json dict."""
    errors: list[str] = []
    warnings: list[str] = []

    for key, value in cfg.items():
        if key.startswith("_"):
            continue  # documentation keys
        if key not in _KNOWN_KEYS:
            errors.append(
                f"unknown key {key!r} — typo? Known keys: {sorted(_KNOWN_KEYS)}"
            )
            continue
        expected = _KNOWN_KEYS[key]
        if not isinstance(value, expected):
            msg = (
                f"key {key!r} has type {type(value).__name__}, "
                f"expected {expected.__name__ if isinstance(expected, type) else '/'.join(t.__name__ for t in expected)}"
            )
            (errors if key in _APPROVAL_CRITICAL else warnings).append(msg)
            continue

        if key == "tools":
            for tool, policy in value.items():
                if not isinstance(policy, str) or policy not in _VALID_POLICIES:
                    errors.append(
                        f"tools[{tool!r}] = {policy!r} — must be one of {sorted(_VALID_POLICIES)}"
                    )
        elif key == "timeout_action" and value not in _VALID_TIMEOUT_ACTIONS:
            errors.append(
                f"timeout_action = {value!r} — must be one of {sorted(_VALID_TIMEOUT_ACTIONS)}"
            )
        elif key == "tts_backend" and value not in _VALID_TTS_BACKENDS:
            warnings.append(
                f"tts_backend = {value!r} — not one of {sorted(_VALID_TTS_BACKENDS)}; "
                "get_client() will fall back to Polly"
            )
        elif key == "vibe_diff_tools":
            for i, tool in enumerate(value):
                if not isinstance(tool, str):
                    errors.append(f"vibe_diff_tools[{i}] = {tool!r} — must be a string")

    return errors, warnings


def check_approval_config_at_startup(path: Path | None = None) -> None:
    """Validate approval_config.json; raise SystemExit on approval-critical errors.

    A missing file is fine (approval_hook falls back to fail-safe defaults).
    Unparseable JSON is fatal: the file exists but says nothing — the operator
    almost certainly did not intend to run with empty policy.
    """
    if path is None:
        path = Path(__file__).resolve().parent.parent / "approval_config.json"
    if not path.exists():
        log.info("approval_config.json not found at %s — fail-safe defaults apply", path)
        return

    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(
            f"approval_config.json is unreadable/invalid JSON ({exc}). "
            "Fix or delete the file — refusing to start with an unknown approval policy."
        ) from exc

    if not isinstance(cfg, dict):
        raise SystemExit("approval_config.json must contain a JSON object at top level.")

    errors, warnings = validate_approval_config(cfg)
    for w in warnings:
        log.warning("approval_config.json: %s", w)
    if errors:
        for e in errors:
            log.error("approval_config.json: %s", e)
        raise SystemExit(
            f"approval_config.json has {len(errors)} approval-affecting error(s) — "
            "refusing to start (a typo here weakens the voice-approval gate)."
        )
