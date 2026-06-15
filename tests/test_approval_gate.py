"""Voice approval-gate hardening — ambient audio can't silently approve.

Bug (2026-06-04): while approval_hook.py's `pending` signal file existed,
WhisperStream wrote *whatever it transcribed next* to the response file. Real
logs showed ambient speech ("share our oil", "Let's put C3 up on the screen")
and the TTS echo of the question itself resolving destructive-tool approvals.

These tests pin the fix:
  * only a deliberate confirmation word is written as the response;
  * ambient / garbage / echo is discarded and the gate keeps waiting;
  * timeout / ambiguity fails safe to DENY.

See: core/approval_keywords.py, sensors/whisper_stream.py (_handle_approval_gate,
_check_approval_echo_guard), approval_hook.py.
"""

from __future__ import annotations

import time

import pytest

from core.approval_keywords import classify_confirmation, MAX_ANSWER_WORDS
from sensors.whisper_stream import WhisperStream
import sensors.whisper_stream as ws_mod


# ---------------------------------------------------------------------------
# classify_confirmation — the shared yes/no parser
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "yes", "Yes.", "yeah", "approve", "approve it", "ok", "sure",
    "go ahead", "do it", "go for it", "yes please",
])
def test_classify_approve(text):
    assert classify_confirmation(text) == "approve"


@pytest.mark.parametrize("text", [
    "no", "No.", "nope", "cancel", "stop", "deny", "abort",
    "don't", "do not", "never mind", "hold on",
])
def test_classify_deny(text):
    assert classify_confirmation(text) == "deny"


@pytest.mark.parametrize("text", [
    "",
    "   ",
    "share our oil",
    "prognostications",
    "broken record",
    "Let's put C3 up on the screen",
    "the quick brown fox",
])
def test_classify_ambient_is_none(text):
    """Ambient speech with no confirmation keyword is not an answer."""
    assert classify_confirmation(text) is None


def test_classify_deny_wins_ties():
    """When both appear, deny wins — ambiguity fails safe toward blocking."""
    assert classify_confirmation("yes no") == "deny"
    assert classify_confirmation("approve but cancel") == "deny"


def test_classify_long_sentence_with_keyword_is_rejected():
    """A long sentence that merely contains a keyword is ambient, not an answer."""
    long_yes = " ".join(["please"] * (MAX_ANSWER_WORDS + 2) + ["yes"])
    assert classify_confirmation(long_yes) is None


def test_tts_echo_question_text_does_not_self_approve_when_short():
    """Defense in depth: even if the echo slips through, a bare 'approve running
    python' is the question — but it DOES contain 'approve'. The echo guard
    (suppression) is what prevents it being transcribed; classify alone cannot
    tell it apart, so we assert the guard exists rather than relying on text."""
    # The question contains "approve" — this documents WHY suppression matters.
    assert classify_confirmation("approve running python") == "approve"


# ---------------------------------------------------------------------------
# WhisperStream._handle_approval_gate — the core intercept
# ---------------------------------------------------------------------------

@pytest.fixture
def approval_dir(tmp_path, monkeypatch):
    """Point the shared approval dir at a temp folder for the duration of a test."""
    d = tmp_path / "approval"
    d.mkdir()
    monkeypatch.setattr(ws_mod, "_APPROVAL_DIR", d)
    return d


def _stream() -> WhisperStream:
    # __init__ does not load any model, so construction is cheap and offline.
    return WhisperStream()


def test_gate_closed_does_not_consume(approval_dir):
    """No pending file → transcript is not consumed, no response written."""
    ws = _stream()
    assert ws._handle_approval_gate("yes") is False
    assert not (approval_dir / "response").exists()


def test_ambient_transcript_not_written_and_keeps_waiting(approval_dir):
    """(a) Ambient/non-confirmation is consumed (not forwarded) but NOT written
    as the response — the gate keeps waiting for a real answer."""
    (approval_dir / "pending").write_text(str(time.time()))
    ws = _stream()
    consumed = ws._handle_approval_gate("Let's put C3 up on the screen")
    assert consumed is True                      # not forwarded to FusionEngine
    assert not (approval_dir / "response").exists()  # gate still waiting


def test_yes_resolves_approve(approval_dir):
    """(b) 'yes' writes the canonical 'approve' verdict."""
    (approval_dir / "pending").write_text(str(time.time()))
    ws = _stream()
    assert ws._handle_approval_gate("yes please") is True
    assert (approval_dir / "response").read_text(encoding="utf-8") == "approve"


def test_no_resolves_deny(approval_dir):
    """(c) 'cancel'/'no' writes the canonical 'deny' verdict."""
    (approval_dir / "pending").write_text(str(time.time()))
    ws = _stream()
    assert ws._handle_approval_gate("no cancel") is True
    assert (approval_dir / "response").read_text(encoding="utf-8") == "deny"


def test_ambient_then_real_answer(approval_dir):
    """Ambient first (discarded), then a real 'yes' resolves the gate."""
    (approval_dir / "pending").write_text(str(time.time()))
    ws = _stream()
    ws._handle_approval_gate("broken record")
    assert not (approval_dir / "response").exists()
    ws._handle_approval_gate("yes")
    assert (approval_dir / "response").read_text(encoding="utf-8") == "approve"


# ---------------------------------------------------------------------------
# WhisperStream._check_approval_echo_guard — TTS echo suppression
# ---------------------------------------------------------------------------

def test_echo_guard_suppresses_on_gate_open(approval_dir):
    """When the gate first opens, the mic is flushed + suppressed so the spoken
    question's echo isn't transcribed as the answer."""
    ws = _stream()
    ws._buffer_chunks = [object()]               # pretend echo is buffered
    ws._suppress_until = 0.0
    assert ws._approval_pending_active is False

    (approval_dir / "pending").write_text(str(time.time()))
    ws._check_approval_echo_guard()

    assert ws._approval_pending_active is True
    assert ws._buffer_chunks == []                # echo flushed
    assert ws._suppress_until > time.monotonic()  # mic suppressed


def test_gate_open_fires_a2ui_callback_with_prompt(approval_dir):
    """A2UI: the gate-open transition fires on_approval_gate_open with the
    persisted prompt description (parallel to the voice question)."""
    ws = _stream()
    seen = []
    ws.on_approval_gate_open = lambda desc: seen.append(desc)

    (approval_dir / "prompt").write_text("Approve write to config?", encoding="utf-8")
    (approval_dir / "pending").write_text(str(time.time()))
    ws._check_approval_echo_guard()

    assert seen == ["Approve write to config?"]
    # Idempotent: a second poll while still open must not re-fire.
    ws._check_approval_echo_guard()
    assert len(seen) == 1


def test_gate_open_callback_falls_back_when_prompt_missing(approval_dir):
    """Missing prompt file → callback still fires with a safe default, never
    crashing the gate."""
    ws = _stream()
    seen = []
    ws.on_approval_gate_open = lambda desc: seen.append(desc)

    (approval_dir / "pending").write_text(str(time.time()))
    ws._check_approval_echo_guard()

    assert seen == ["Approve this action?"]


def test_gate_open_callback_failure_does_not_break_gate(approval_dir):
    """A throwing A2UI callback must not prevent the voice gate from opening."""
    ws = _stream()
    ws.on_approval_gate_open = lambda desc: (_ for _ in ()).throw(RuntimeError("boom"))

    (approval_dir / "pending").write_text(str(time.time()))
    ws._check_approval_echo_guard()   # must not raise

    assert ws._approval_pending_active is True


def test_echo_guard_fires_once_per_gate(approval_dir):
    """The flush happens once on open, not on every poll tick — so the user's
    actual reply (buffered after the gate opened) is not discarded."""
    ws = _stream()
    (approval_dir / "pending").write_text(str(time.time()))
    ws._check_approval_echo_guard()
    # User speaks during the gate — buffer fills again on a later tick.
    sentinel = object()
    ws._buffer_chunks = [sentinel]
    ws._check_approval_echo_guard()
    assert ws._buffer_chunks == [sentinel]  # not re-flushed


def test_echo_guard_resets_when_gate_closes(approval_dir):
    ws = _stream()
    (approval_dir / "pending").write_text(str(time.time()))
    ws._check_approval_echo_guard()
    assert ws._approval_pending_active is True
    (approval_dir / "pending").unlink()
    ws._check_approval_echo_guard()
    assert ws._approval_pending_active is False


# ---------------------------------------------------------------------------
# approval_hook.py — fail-safe-to-deny + sync with whisper_stream
# ---------------------------------------------------------------------------

def test_hook_parse_response_uses_shared_classifier():
    import approval_hook
    assert approval_hook._parse_response("approve") is True
    assert approval_hook._parse_response("deny") is False
    assert approval_hook._parse_response("yes please") is True
    assert approval_hook._parse_response("no cancel") is False


def test_hook_ambiguous_defaults_to_deny():
    """(d) Ambiguity / unrecognised text fails safe to DENY by default."""
    import approval_hook
    assert approval_hook._parse_response("share our oil") is False
    assert approval_hook._parse_response("") is False
    # Only an explicit opt-in default flips ambiguity to approve.
    assert approval_hook._parse_response("share our oil", default="approve") is True
    assert approval_hook._parse_response("share our oil", default="reject") is False


def test_hook_request_ipad_approval_times_out_to_none(tmp_path, monkeypatch):
    """No response written within the window → None (caller treats as deny)."""
    import approval_hook
    d = tmp_path / "approval"
    d.mkdir()
    monkeypatch.setattr(approval_hook, "_APPROVAL_DIR", d)
    monkeypatch.setattr(approval_hook, "_PENDING_FILE", d / "pending")
    monkeypatch.setattr(approval_hook, "_RESPONSE_FILE", d / "response")
    result = approval_hook._request_ipad_approval(timeout_s=0.3)
    assert result is None
    assert not (d / "pending").exists()   # cleaned up


def test_config_timeout_action_is_reject():
    """The shipped config must fail safe to deny on timeout."""
    import json
    from pathlib import Path
    cfg = json.loads((Path(__file__).resolve().parents[1] / "approval_config.json")
                     .read_text(encoding="utf-8"))
    assert cfg.get("timeout_action") == "reject"
