"""Integration tests for the cloud fallback path (Gate 2 → Anthropic API).

Covers:
  1. _CloudInference.infer() — real Anthropic API call on a complex command
  2. HybridCoordinator.route() Gate 2 trigger — complexity keyword routes to cloud
  3. Voice-misrecognition disambiguation (cloud-specific guidance)
  4. Graceful degradation — bad API key → CLARIFY (no crash)
  5. Gate 0 (privacy) force-local — cloud is never invoked for sensitive text
  6. ContentFilter scrubs secrets before cloud transmission

Run:
    python tests/test_cloud_path.py

Requires ANTHROPIC_API_KEY in the environment.

Exit codes: 0 = all passed, 1 = any failed
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.command_executor import Command
from core.hybrid_coordinator import (
    CoordinatorConfig,
    HybridCoordinator,
    _CloudInference,
    _CLOUD_SYSTEM_PROMPT,
    _apply_vocabulary_corrections,
    _retranscribe,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cmd(text: str, source: str = "voice", logprob: float = 0.0) -> Command:
    return Command(text=text, action="", source=source, whisper_logprob=logprob)


def _complex_cmd(text: str) -> Command:
    """A command that will fail Gate 2 (contains 'and then')."""
    return _cmd(text, logprob=0.0)


# ---------------------------------------------------------------------------
# Test 1: _CloudInference.infer() — real Bedrock call
# ---------------------------------------------------------------------------

async def test_cloud_inference_real_anthropic() -> tuple[bool, str]:
    """_CloudInference sends a complex command to the Anthropic API and gets a valid action."""
    cloud = _CloudInference(model="claude-haiku-4-5-20251001")
    cmd = _cmd("close the window and then open Chrome")
    result = await cloud.infer(cmd)

    first_word = result.split()[0].upper() if result.split() else ""
    valid_verbs = {"CLICK", "SCROLL", "TYPE", "OPEN", "CLOSE", "HOTKEY",
                   "DICTATE", "SCREENSHOT", "CLARIFY"}

    if first_word not in valid_verbs:
        return False, f"Got unrecognised verb in {result!r}"
    return True, f"Bedrock returned: {result!r}"


# ---------------------------------------------------------------------------
# Test 2: Gate 2 routing — complexity keyword sends command to cloud
# ---------------------------------------------------------------------------

async def test_gate2_routes_to_cloud() -> tuple[bool, str]:
    """A command containing 'and then' fails Gate 2 and calls _run_cloud."""
    mock_cloud = AsyncMock(return_value="CLOSE")
    mock_execute = AsyncMock(return_value={"status": "ok"})

    cfg = CoordinatorConfig()
    coord = HybridCoordinator(config=cfg)
    coord._cloud.infer = mock_cloud
    coord._execute_action = mock_execute

    cmd = _complex_cmd("close the window and then open Chrome")
    await coord.route(cmd)

    if not mock_cloud.called:
        return False, "_run_cloud was not called — Gate 2 did not trigger"
    return True, f"Gate 2 triggered cloud; got: {mock_cloud.call_args}"


# ---------------------------------------------------------------------------
# Test 3: Voice misrecognition resolved by cloud system prompt
# ---------------------------------------------------------------------------

async def test_cloud_resolves_voice_misrecognition() -> tuple[bool, str]:
    """Cloud correctly resolves 'clothes' → CLOSE via misrecognition guidance."""
    cloud = _CloudInference(model="claude-haiku-4-5-20251001")
    cmd = _cmd("clothes the window", source="voice", logprob=-0.9)
    result = await cloud.infer(cmd)

    if not result.upper().startswith("CLOSE"):
        return False, f"Expected CLOSE, got: {result!r}"
    return True, f"Misrecognition resolved: {result!r}"


# ---------------------------------------------------------------------------
# Test 4: Graceful degradation — bad credentials → CLARIFY
# ---------------------------------------------------------------------------

async def test_cloud_bad_credentials_returns_clarify() -> tuple[bool, str]:
    """When the Anthropic client raises an exception, cloud returns CLARIFY (no crash)."""
    cloud = _CloudInference(model="claude-haiku-4-5-20251001")

    with patch.object(cloud, "_get_client") as mock_client:
        mock_client.return_value.messages.create = MagicMock(
            side_effect=Exception("invalid x-api-key")
        )
        cmd = _cmd("close the window")
        result = await cloud.infer(cmd)

    if not result.startswith("CLARIFY"):
        return False, f"Expected CLARIFY prefix, got: {result!r}"
    return True, f"Graceful degradation: {result!r}"


# ---------------------------------------------------------------------------
# Test 5: Gate 0 — privacy blocks cloud for sensitive text
# ---------------------------------------------------------------------------

async def test_gate0_blocks_cloud_for_sensitive_text() -> tuple[bool, str]:
    """Gate 0 prevents cloud routing when command text contains sensitive patterns."""
    mock_cloud = AsyncMock(return_value="TYPE secret")
    mock_local = AsyncMock(return_value="TYPE ****")
    mock_execute = AsyncMock(return_value={"status": "ok"})

    cfg = CoordinatorConfig()
    coord = HybridCoordinator(config=cfg)
    coord._cloud.infer = mock_cloud
    coord._local.infer = mock_local
    coord._execute_action = mock_execute

    # "password" is in gate0_sensitive_patterns → must stay local
    cmd = _cmd("type my password into the login field", source="voice")
    await coord.route(cmd)

    if mock_cloud.called:
        return False, "Cloud was called for sensitive text — Gate 0 failed!"
    if not mock_local.called:
        return False, "Local was not called — command was dropped"
    return True, "Gate 0 correctly blocked cloud for sensitive text"


# ---------------------------------------------------------------------------
# Test 6: AgentCore fall-through to raw Bedrock
# ---------------------------------------------------------------------------

async def test_cloud_content_filter_scrubs_secrets() -> tuple[bool, str]:
    """ContentFilter scrubs secrets before cloud transmission."""
    from unittest.mock import AsyncMock as _AM
    from adaptive.content_filter import ContentFilter

    mock_bedrock = _AM(return_value="CLARIFY")
    mock_execute = _AM(return_value={"status": "ok"})

    coord = HybridCoordinator(config=CoordinatorConfig())
    coord._cloud.infer = mock_bedrock
    coord._execute_action = mock_execute
    coord._content_filter = ContentFilter()

    cmd = _complex_cmd("my password is secret123 close the window and open Chrome")
    await coord.route(cmd)

    if not mock_bedrock.called:
        return False, "Bedrock was never called"
    transmitted_text = mock_bedrock.call_args[0][0].text
    if "secret123" in transmitted_text:
        return False, f"Secret not scrubbed — Bedrock received: {transmitted_text!r}"
    return True, "ContentFilter scrubbed secret before cloud transmission"


# ---------------------------------------------------------------------------
# Test 7: Gate 1 vocabulary correction
# ---------------------------------------------------------------------------

async def test_gate1_vocab_correction() -> tuple[bool, str]:
    """_apply_vocabulary_corrections fixes known voice misrecognitions instantly."""
    cases = [
        ("clothes the window",  "close"),
        ("scroll done please",  "scroll"),
        ("oh pen notepad",      "open"),
        ("hot key control zee", "hotkey"),
        ("take a screen shot",  "take"),   # no change — "take" stays
    ]
    for inp, expected_first in cases:
        got, _ = _apply_vocabulary_corrections(inp)
        if not got.lower().startswith(expected_first):
            return False, f"{inp!r} => {got!r} (expected to start with {expected_first!r})"
    return True, "All 5 vocabulary corrections applied correctly"


async def test_gate1_retranscribe_no_audio() -> tuple[bool, str]:
    """_retranscribe with no audio bytes still applies vocabulary correction."""
    cmd = _cmd("clothes the window", source="voice", logprob=-1.5)
    result = await _retranscribe(cmd)
    if not result.text.lower().startswith("close"):
        return False, f"Expected 'Close ...', got {result.text!r}"
    if result.whisper_logprob != 0.0:
        return False, f"logprob should be reset to 0.0, got {result.whisper_logprob}"
    return True, f"Corrected to {result.text!r} with logprob reset"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def run_tests() -> int:
    tests = [
        ("_CloudInference real Anthropic API call",     test_cloud_inference_real_anthropic),
        ("Gate 2 routes complex command to cloud",     test_gate2_routes_to_cloud),
        ("Cloud resolves voice misrecognition",        test_cloud_resolves_voice_misrecognition),
        ("Bad credentials → CLARIFY (no crash)",       test_cloud_bad_credentials_returns_clarify),
        ("Gate 0 blocks cloud for sensitive text",     test_gate0_blocks_cloud_for_sensitive_text),
        ("ContentFilter scrubs secrets before cloud",  test_cloud_content_filter_scrubs_secrets),
        ("Gate 1 vocabulary correction (5 cases)",     test_gate1_vocab_correction),
        ("Gate 1 retranscribe — no audio fallback",    test_gate1_retranscribe_no_audio),
    ]

    print(f"\nCloud path integration tests\n{'─' * 50}")
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

    print(f"\n{'─' * 50}")
    print(f"Results: {passed}/{len(tests)} passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run_tests()))
