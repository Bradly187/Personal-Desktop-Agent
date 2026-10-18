"""Strict request-bound chat approval protocol tests."""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock

from core import chat_server as cs_mod
from core.approval_authority import ApprovalAuthority
from core.chat_server import ChatServer, _ChatClient


def _request(tmp_path):
    authority = ApprovalAuthority(tmp_path / "approval")
    request = authority.create_request(
        trace_id="T1", action_digest="d" * 64, description="Approve all?"
    )
    authority.open_response_window(request)
    return authority, request


def test_approval_event_becomes_correlated_card_frame():
    frame = ChatServer._to_frame({
        "topic": "dag.approval_requested",
        "payload": {
            "message": "I'll run 2 steps. Approve all?",
            "destructive": True,
            "request_id": "r" * 32,
            "action_digest": "d" * 64,
        },
        "trace_id": "T1",
    })
    assert frame == {
        "type": "approval",
        "message": "I'll run 2 steps. Approve all?",
        "destructive": True,
        "request_id": "r" * 32,
        "action_digest": "d" * 64,
    }


def test_uncorrelated_or_non_boolean_card_payload_is_rejected():
    assert ChatServer._to_frame({
        "topic": "dag.approval_requested",
        "payload": {"message": "Approve?", "destructive": True},
    }) is None
    assert ChatServer._to_frame({
        "topic": "dag.approval_requested",
        "payload": {
            "message": "Approve?", "destructive": "true",
            "request_id": "r" * 32, "action_digest": "d" * 64,
        },
    }) is None


async def test_browser_response_publishes_exact_approval(tmp_path, monkeypatch):
    authority, request = _request(tmp_path)
    monkeypatch.setattr(cs_mod, "_APPROVAL_DIR", tmp_path / "approval")
    server = ChatServer(token="test-token")
    assert await server._write_approval(
        request.request_id,
        request.action_digest,
        "approve",
        time.time(),
    )
    grant = authority.consume_response(
        request, approved_digests=(request.action_digest,)
    )
    assert grant is not None


async def test_browser_rejects_legacy_truthy_bool_and_missing_binding(tmp_path, monkeypatch):
    authority, request = _request(tmp_path)
    monkeypatch.setattr(cs_mod, "_APPROVAL_DIR", tmp_path / "approval")
    server = ChatServer(token="test-token")
    assert not await server._write_approval(
        request.request_id, request.action_digest, True, time.time()
    )
    assert not await server._write_approval(None, None, "approve", time.time())
    assert not authority.response_path(request.request_id).exists()


def test_web_client_echoes_exact_response_contract():
    source = (Path(__file__).parents[1] / "web_client_chat" / "chat.js").read_text(
        encoding="utf-8"
    )
    for field in ("request_id", "action_digest", "decision", "responded_at"):
        assert field in source
    assert 'approve: approve' not in source


async def test_chat_ack_is_correlated_and_reports_rejection(tmp_path, monkeypatch):
    authority, request = _request(tmp_path)
    monkeypatch.setattr(cs_mod, "_APPROVAL_DIR", tmp_path / "approval")
    server = ChatServer(token="test-token")
    client = _ChatClient(ws=MagicMock())
    payload = {
        "type": "approval_response",
        "trace_id": "T1",
        "request_id": request.request_id,
        "action_digest": request.action_digest,
        "decision": "approve",
        "responded_at": time.time(),
    }
    await server._on_client_message(client, json.dumps(payload))
    ack = client._q.get_nowait()
    assert ack == {
        "type": "approval_ack", "trace_id": "T1",
        "request_id": request.request_id, "status": "ok",
    }
    authority.close_response_window(request)
    late = authority.create_request(
        trace_id="T2", action_digest="e" * 64, description="late"
    )
    payload.update({
        "trace_id": "T2", "request_id": late.request_id,
        "action_digest": late.action_digest,
    })
    await server._on_client_message(client, json.dumps(payload))
    assert client._q.get_nowait()["status"] == "error"


def test_approval_closed_frame_is_request_correlated():
    assert ChatServer._to_frame({
        "topic": "dag.approval_closed",
        "payload": {"request_id": "r" * 32},
    }) == {"type": "approval_closed", "request_id": "r" * 32}
