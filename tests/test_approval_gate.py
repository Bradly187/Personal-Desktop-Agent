"""Voice approval classifier, structured responder, and echo-guard tests."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from core.approval_authority import ApprovalAuthority
from core.approval_keywords import MAX_ANSWER_WORDS, classify_confirmation
from sensors.whisper_stream import WhisperStream
import sensors.whisper_stream as ws_mod


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
    "", "   ", "share our oil", "prognostications", "broken record",
    "Let's put C3 up on the screen", "the quick brown fox",
])
def test_classify_ambient_is_none(text):
    assert classify_confirmation(text) is None


def test_classify_deny_wins_ties():
    assert classify_confirmation("yes no") == "deny"
    assert classify_confirmation("approve but cancel") == "deny"


def test_classify_long_sentence_with_keyword_is_rejected():
    long_yes = " ".join(["please"] * (MAX_ANSWER_WORDS + 2) + ["yes"])
    assert classify_confirmation(long_yes) is None


def test_tts_echo_question_text_requires_guard():
    assert classify_confirmation("approve running python") == "approve"


@pytest.fixture
def approval_dir(tmp_path, monkeypatch):
    directory = tmp_path / "approval"
    directory.mkdir()
    monkeypatch.setattr(ws_mod, "_APPROVAL_DIR", directory)
    return directory


def _stream() -> WhisperStream:
    return WhisperStream()


def _open_request(directory: Path, *, digest: str = "d" * 64):
    authority = ApprovalAuthority(directory)
    request = authority.create_request(
        trace_id="t", action_digest=digest, description="Approve write?"
    )
    authority.open_response_window(request)
    authority.publish_pending_request(request)
    return authority, request


def test_gate_closed_does_not_consume(approval_dir):
    ws = _stream()
    assert ws._handle_approval_gate("yes") is False
    assert not list((approval_dir / "responses").glob("*.json"))


def test_ambient_transcript_not_written_and_keeps_waiting(approval_dir):
    authority, request = _open_request(approval_dir)
    assert _stream()._handle_approval_gate("Let's put C3 up on the screen") is True
    assert not authority.response_path(request.request_id).exists()


def test_yes_resolves_exact_approve(approval_dir):
    authority, request = _open_request(approval_dir)
    assert _stream()._handle_approval_gate("yes please") is True
    payload = json.loads(authority.response_path(request.request_id).read_text())
    assert payload["request_id"] == request.request_id
    assert payload["action_digest"] == request.action_digest
    assert payload["decision"] == "approve"
    assert authority.consume_response(
        request, approved_digests=(request.action_digest,)
    ) is not None


def test_no_resolves_exact_deny(approval_dir):
    authority, request = _open_request(approval_dir)
    assert _stream()._handle_approval_gate("no cancel") is True
    payload = json.loads(authority.response_path(request.request_id).read_text())
    assert payload["decision"] == "deny"
    assert authority.consume_response(
        request, approved_digests=(request.action_digest,)
    ) is None


def test_overlapping_voice_requests_are_ambiguous_and_ignored(approval_dir):
    first_authority, first = _open_request(approval_dir, digest="a" * 64)
    second = first_authority.create_request(
        trace_id="t2", action_digest="b" * 64, description="second"
    )
    first_authority.publish_pending_request(second)
    assert _stream()._handle_approval_gate("yes") is True
    assert not first_authority.response_path(first.request_id).exists()
    assert not first_authority.response_path(second.request_id).exists()


def test_echo_guard_suppresses_once_and_fires_exact_callback(approval_dir):
    authority, request = _open_request(approval_dir)
    ws = _stream()
    ws._buffer_chunks = [object()]
    ws._suppress_until = 0.0
    seen = []
    ws.on_approval_gate_open = seen.append

    ws._check_approval_echo_guard()
    assert ws._approval_pending_ids == frozenset({request.request_id})
    assert ws._buffer_chunks == []
    assert ws._suppress_until > time.monotonic()
    assert seen == [request]

    sentinel = object()
    ws._buffer_chunks = [sentinel]
    ws._check_approval_echo_guard()
    assert ws._buffer_chunks == [sentinel]
    assert seen == [request]

    authority.close_pending_request(request)
    ws._check_approval_echo_guard()
    assert ws._approval_pending_ids == frozenset()


def test_echo_guard_callback_failure_does_not_break_gate(approval_dir):
    _, request = _open_request(approval_dir)
    ws = _stream()
    ws.on_approval_gate_open = lambda req: (_ for _ in ()).throw(RuntimeError("boom"))
    ws._check_approval_echo_guard()
    assert request.request_id in ws._approval_pending_ids


def test_hook_structured_request_times_out_and_cleans_marker(tmp_path):
    import approval_hook

    authority = ApprovalAuthority(tmp_path / "approval")
    request = authority.create_request(
        trace_id="hook", action_digest="a" * 64, description="Approve hook?"
    )
    assert approval_hook._request_ipad_approval(
        authority, request, timeout_s=0.1
    ) is False
    assert not authority.pending_path(request.request_id).exists()


def test_config_timeout_action_is_reject():
    cfg = json.loads((Path(__file__).resolve().parents[1] / "approval_config.json")
                     .read_text(encoding="utf-8"))
    assert cfg.get("timeout_action") == "reject"


def test_local_hook_accepts_only_exact_structured_responder(tmp_path):
    import approval_hook
    from core.approval_authority import ApprovalResponse

    authority = ApprovalAuthority(tmp_path / "approval")
    request = authority.create_request(
        trace_id="hook", action_digest="a" * 64, description="Approve hook?"
    )
    authority.open_response_window(request)

    def answer():
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if authority.active_pending_requests() == (request,):
                authority.write_response(ApprovalResponse(
                    request_id=request.request_id,
                    action_digest=request.action_digest,
                    decision="approve",
                    responded_at=time.time(),
                ), require_open_window=True)
                return
            time.sleep(0.01)
        raise AssertionError("hook request never became pending")

    worker = threading.Thread(target=answer)
    worker.start()
    assert approval_hook._request_ipad_approval(
        authority, request, timeout_s=1.0
    ) is True
    worker.join(timeout=2.0)
    grant = authority.consume_response(
        request, approved_digests=(request.action_digest,)
    )
    assert grant is not None
