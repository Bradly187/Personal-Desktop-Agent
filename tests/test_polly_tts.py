"""Integration tests for Amazon Polly TTS clarification (Req 7).

Covers:
  1. _polly_speak() real AWS call — synthesizes audio and plays it
  2. CLARIFY with route=cloud → spoken=True in result
  3. CLARIFY with route=local → spoken=False (Polly not called)
  4. Empty message → False (no API call)
  5. Message > 3000 chars → truncated to exactly 3000 before API call
  6. Graceful degradation — bad credentials → False, no exception
  7. Graceful degradation — sounddevice unavailable → False, no exception
  8. HybridCoordinator cloud CLARIFY → Polly invoked via _execute_action route_label

Run:
    python tests/test_polly_tts.py

Requires active AWS credentials with Polly access.
Exit codes: 0 = all passed, 1 = any failed
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from command_executor import Command, CommandExecutor, _polly_speak, _POLLY_MAX_CHARS
from hybrid_coordinator import CoordinatorConfig, HybridCoordinator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cmd(text: str, action: str = "CLARIFY", source: str = "voice",
         params: dict | None = None) -> Command:
    return Command(text=text, action=action, source=source,
                   params=params or {})


# ---------------------------------------------------------------------------
# Test 1: Real Polly call — synthesizes and plays audio
# ---------------------------------------------------------------------------

async def test_polly_real_call() -> tuple[bool, str]:
    """_polly_speak() makes a real AWS Polly call and returns True."""
    result = _polly_speak("Did you mean close the window?")
    if not result:
        return False, "_polly_speak returned False — check AWS credentials and Polly access"
    return True, "Polly synthesized and played audio successfully"


# ---------------------------------------------------------------------------
# Test 2: CLARIFY + route=cloud → spoken=True
# ---------------------------------------------------------------------------

async def test_clarify_cloud_route_spoken() -> tuple[bool, str]:
    """CommandExecutor CLARIFY with route=cloud calls Polly and sets spoken=True."""
    executor = CommandExecutor()
    cmd = _cmd("Unclear", params={"message": "Did you mean close?", "route": "cloud"})

    # Mock Polly so we don't make a real call in this unit test
    with patch("command_executor._polly_speak", return_value=True) as mock_polly:
        result = await executor.execute(cmd)

    if not mock_polly.called:
        return False, "_polly_speak was not called for cloud-route CLARIFY"
    if result["result"].get("spoken") is not True:
        return False, f"Expected spoken=True, got: {result['result']}"
    return True, "cloud CLARIFY correctly calls Polly and reports spoken=True"


# ---------------------------------------------------------------------------
# Test 3: CLARIFY + route=local → spoken=False (Polly not called)
# ---------------------------------------------------------------------------

async def test_clarify_local_route_not_spoken() -> tuple[bool, str]:
    """CommandExecutor CLARIFY with route=local never calls Polly."""
    executor = CommandExecutor()
    cmd = _cmd("Unclear", params={"message": "Did you mean close?", "route": "local"})

    with patch("command_executor._polly_speak", return_value=True) as mock_polly:
        result = await executor.execute(cmd)

    if mock_polly.called:
        return False, "_polly_speak should NOT be called for local-route CLARIFY"
    if result["result"].get("spoken") is not False:
        return False, f"Expected spoken=False, got: {result['result']}"
    return True, "local CLARIFY correctly skips Polly"


# ---------------------------------------------------------------------------
# Test 4: Empty message → False immediately
# ---------------------------------------------------------------------------

async def test_polly_empty_message() -> tuple[bool, str]:
    """_polly_speak('') returns False without making an API call."""
    with patch("boto3.client") as mock_boto:
        result = _polly_speak("")

    if mock_boto.called:
        return False, "boto3.client was called for empty message — should short-circuit"
    if result is not False:
        return False, f"Expected False for empty message, got {result!r}"
    return True, "Empty message returns False without API call"


# ---------------------------------------------------------------------------
# Test 5: Long message gets truncated to _POLLY_MAX_CHARS
# ---------------------------------------------------------------------------

async def test_polly_truncates_long_message() -> tuple[bool, str]:
    """Messages longer than 3000 chars are truncated before calling Polly."""
    long_msg = "x" * (_POLLY_MAX_CHARS + 500)
    captured: list[str] = []

    def fake_synthesize(**kwargs):
        captured.append(kwargs.get("Text", ""))
        return {"AudioStream": MagicMock(read=lambda: b"\x00\x01" * 100)}

    mock_sd = MagicMock()
    mock_np = MagicMock()
    mock_np.frombuffer.return_value = mock_np
    mock_np.astype.return_value = mock_np

    with patch("boto3.client") as mock_boto, \
         patch("sounddevice.play"), \
         patch("numpy.frombuffer", return_value=MagicMock(astype=lambda *a: MagicMock())):
        mock_client = MagicMock()
        mock_client.synthesize_speech.side_effect = fake_synthesize
        mock_boto.return_value = mock_client
        _polly_speak(long_msg)

    if not captured:
        return False, "synthesize_speech was not called"
    if len(captured[0]) != _POLLY_MAX_CHARS:
        return False, f"Expected {_POLLY_MAX_CHARS} chars, got {len(captured[0])}"
    return True, f"Long message correctly truncated to {_POLLY_MAX_CHARS} chars"


# ---------------------------------------------------------------------------
# Test 6: Bad credentials → False, no exception
# ---------------------------------------------------------------------------

async def test_polly_bad_credentials_returns_false() -> tuple[bool, str]:
    """Bad AWS credentials cause _polly_speak to return False without raising."""
    import botocore.exceptions
    fake_error = botocore.exceptions.NoCredentialsError()

    with patch("boto3.client") as mock_boto:
        mock_client = MagicMock()
        mock_client.synthesize_speech.side_effect = fake_error
        mock_boto.return_value = mock_client

        try:
            result = _polly_speak("Hello")
        except Exception as exc:
            return False, f"_polly_speak raised an exception: {exc}"

    if result is not False:
        return False, f"Expected False on credential error, got {result!r}"
    return True, "Bad credentials handled gracefully — returns False, no exception"


# ---------------------------------------------------------------------------
# Test 7: sounddevice missing → False, no exception
# ---------------------------------------------------------------------------

async def test_polly_sounddevice_missing_returns_false() -> tuple[bool, str]:
    """Missing sounddevice causes _polly_speak to return False without raising."""
    original_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__

    import builtins
    real_import = builtins.__import__

    def blocking_import(name, *args, **kwargs):
        if name == "sounddevice":
            raise ImportError("No module named 'sounddevice'")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=blocking_import):
        try:
            result = _polly_speak("Hello")
        except Exception as exc:
            return False, f"_polly_speak raised with missing sounddevice: {exc}"

    if result is not False:
        return False, f"Expected False when sounddevice missing, got {result!r}"
    return True, "Missing sounddevice handled gracefully — returns False"


# ---------------------------------------------------------------------------
# Test 8: HybridCoordinator sets route=cloud in params for cloud CLARIFYs
# ---------------------------------------------------------------------------

async def test_coordinator_sets_cloud_route_in_params() -> tuple[bool, str]:
    """When HybridCoordinator routes to cloud and gets CLARIFY, Polly is invoked."""
    captured_params: list[dict] = []

    def fake_dispatch(action: str, cmd: Command) -> dict:
        captured_params.append(dict(cmd.params))
        return {"clarify": True, "message": "test", "spoken": False}

    cfg = CoordinatorConfig()
    coord = HybridCoordinator(config=cfg)

    # Make cloud inference return CLARIFY
    coord._cloud.infer = AsyncMock(return_value="CLARIFY Did you mean close?")
    coord._executor._dispatch = fake_dispatch

    # Send a command that will fail Gate 2 (complexity keyword "and then")
    cmd = Command(
        text="close the window and then open Chrome",
        action="", source="voice", whisper_logprob=0.0,
    )
    await coord.route(cmd)

    if not captured_params:
        return False, "CommandExecutor._dispatch was never called"
    params = captured_params[0]
    if params.get("route") != "cloud":
        return False, f"Expected route='cloud' in params, got: {params}"
    return True, "Cloud CLARIFY correctly tagged with route='cloud' in params"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def run_tests() -> int:
    tests = [
        ("Real Polly call — synthesizes + plays",              test_polly_real_call),
        ("CLARIFY route=cloud → spoken=True",                  test_clarify_cloud_route_spoken),
        ("CLARIFY route=local → spoken=False, Polly skipped",  test_clarify_local_route_not_spoken),
        ("Empty message → False, no API call",                 test_polly_empty_message),
        ("Long message truncated to 3000 chars",               test_polly_truncates_long_message),
        ("Bad credentials → False, no exception",              test_polly_bad_credentials_returns_false),
        ("Missing sounddevice → False, no exception",          test_polly_sounddevice_missing_returns_false),
        ("Coordinator sets route=cloud for cloud CLARIFYs",    test_coordinator_sets_cloud_route_in_params),
    ]

    print(f"\nPolly TTS integration tests\n{'─' * 52}")
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
