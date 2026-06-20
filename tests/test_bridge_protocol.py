"""Tests for inference.bridge_protocol — the shared VS Code bridge file contract.

Covers token generation/idempotency, override read tolerance, and roster
publishing. All filesystem state is redirected to a tmp dir so the real
~/.claude is never touched.
"""

from __future__ import annotations

import json

import pytest

from inference import bridge_protocol as bp


@pytest.fixture
def bridge_tmp(tmp_path, monkeypatch):
    """Redirect every bridge_protocol path into an isolated tmp dir."""
    d = tmp_path / "desktop_agent_bridge"
    monkeypatch.setattr(bp, "BRIDGE_DIR", d)
    monkeypatch.setattr(bp, "TOKEN_FILE", d / "token")
    monkeypatch.setattr(bp, "ROSTER_FILE", d / "roster.json")
    monkeypatch.setattr(bp, "OVERRIDE_FILE", d / "override.json")
    return d


# ── token ────────────────────────────────────────────────────────────────

def test_ensure_token_generates_and_is_stable(bridge_tmp):
    t1 = bp.ensure_token()
    assert t1 and len(t1) == 64          # 32 bytes hex
    t2 = bp.ensure_token()
    assert t2 == t1                       # never regenerates once written
    assert bp.read_token() == t1


def test_read_token_absent_is_none(bridge_tmp):
    assert bp.read_token() is None        # no generation side-effect


def test_ensure_token_reuses_externally_written(bridge_tmp):
    bridge_tmp.mkdir(parents=True)
    (bridge_tmp / "token").write_text("deadbeef", encoding="utf-8")
    assert bp.ensure_token() == "deadbeef"


# ── override ───────────────────────────────────────────────────────────────

def test_read_override_absent_is_none(bridge_tmp):
    assert bp.read_override() is None


def test_write_then_read_override(bridge_tmp):
    bp.write_override("qwen3-coder:30b")
    assert bp.read_override() == "qwen3-coder:30b"


def test_override_null_means_auto(bridge_tmp):
    bp.write_override(None)
    assert bp.read_override() is None     # explicit Auto


def test_override_malformed_is_none(bridge_tmp):
    bridge_tmp.mkdir(parents=True)
    (bridge_tmp / "override.json").write_text("{not json", encoding="utf-8")
    assert bp.read_override() is None      # corrupt file can't wedge routing


def test_override_blank_string_is_none(bridge_tmp):
    bp.write_override("   ")
    assert bp.read_override() is None


# ── roster ─────────────────────────────────────────────────────────────────

def test_write_roster_dedups_preserving_order(bridge_tmp):
    bp.write_roster(["a", "b", "a", "c", "b"])
    data = json.loads((bridge_tmp / "roster.json").read_text(encoding="utf-8"))
    assert data["models"] == ["a", "b", "c"]
    assert isinstance(data["updated"], int)
