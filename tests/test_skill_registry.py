"""SkillRegistry — end-to-end MCP client loop against the echo proof server.

Spawns the real `skills.servers.echo_server` over stdio (no mocks) and exercises
discover → call → result, intent matching, the send-tool flag, lifecycle, and
classifier-keyword registration.

Run:
    python -m pytest tests/test_skill_registry.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from skills.registry import SkillRegistry
from core.domain_classifier import DomainClassifier


@pytest.fixture(autouse=True)
def _reset_skill_keywords():
    yield
    DomainClassifier.register_skill_keywords(set())  # don't leak into other tests


def _manifest_dir(tmp_path, enabled: bool = True) -> Path:
    mdir = tmp_path / "manifests"
    mdir.mkdir()
    (mdir / "echo.json").write_text(json.dumps({
        "skill_id": "echo",
        "enabled": enabled,
        "transport": "stdio",
        "server": {"command": sys.executable, "args": ["-m", "skills.servers.echo_server"]},
        "tools": {"allow": ["echo_text", "record_note"], "send_tools": ["record_note"]},
        "intents": {
            "echo": {"keywords": ["echo this"], "tool": "echo_text", "send": False},
            "record": {"keywords": ["record a note"], "tool": "record_note", "send": True},
        },
    }), encoding="utf-8")
    return mdir


async def test_start_discovers_tools_and_calls(tmp_path):
    reg = SkillRegistry(manifest_dir=_manifest_dir(tmp_path))
    await reg.start()
    try:
        assert reg.has_skills()
        assert any(a["tool"] == "echo_text" for a in reg.list_actions())
        res = await reg.call("echo", "echo_text", {"text": "hello skill"})
        assert res["status"] == "ok"
        assert "hello skill" in res["text"]
        # describe_for_prompt lists the discovered tools for the planner.
        desc = reg.describe_for_prompt()
        assert "echo_text" in desc and "record_note" in desc
    finally:
        await reg.stop()
    assert not reg.started


async def test_match_intent_and_send_flag(tmp_path):
    reg = SkillRegistry(manifest_dir=_manifest_dir(tmp_path))
    await reg.start()
    try:
        m = reg.match_intent("please echo this back to me")
        assert m and m["skill_id"] == "echo" and m["tool"] == "echo_text"
        assert m["send"] is False
        m2 = reg.match_intent("record a note for later")
        assert m2 and m2["send"] is True
        assert reg.is_send_tool("echo", "record_note") is True
        assert reg.is_send_tool("echo", "echo_text") is False
    finally:
        await reg.stop()


async def test_forbidden_tool_rejected(tmp_path):
    reg = SkillRegistry(manifest_dir=_manifest_dir(tmp_path))
    await reg.start()
    try:
        res = await reg.call("echo", "not_a_tool", {})
        assert res["status"] == "error"
        res2 = await reg.call("no_such_skill", "echo_text", {})
        assert res2["status"] == "error"
    finally:
        await reg.stop()


async def test_disabled_skill_not_started(tmp_path):
    reg = SkillRegistry(manifest_dir=_manifest_dir(tmp_path, enabled=False))
    await reg.start()
    try:
        assert not reg.has_skills()
        assert reg.describe_for_prompt() == ""
    finally:
        await reg.stop()


async def test_start_registers_classifier_keywords(tmp_path):
    reg = SkillRegistry(manifest_dir=_manifest_dir(tmp_path))
    await reg.start()
    try:
        assert "echo this" in DomainClassifier._SKILL_KEYWORDS
    finally:
        await reg.stop()
