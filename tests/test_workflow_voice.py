"""Unit tests for core/workflow_voice.py — the multi-agent workflow voice trigger.

Pure / synchronous: this module is deterministic and model-free (all inference,
TTS, and mic suppression live in HybridCoordinator and are out of scope here).
Covers trigger parsing, decomposition parsing, prompt builders, and config.
"""

import json

import pytest

from core.workflow_voice import (
    DEFAULT_FANOUT_N,
    WorkflowRequest,
    build_decompose_prompt,
    build_synthesis_prompt,
    fanout_n_from_config,
    parse_decomposition,
    parse_workflow_request,
    verify_enabled,
    workflow_voice_config,
)


# --------------------------------------------------------------------------- #
# parse_workflow_request — positive
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("utterance,goal", [
    ("think hard about quantum error correction", "quantum error correction"),
    ("Think hard about the trolley problem.", "the trolley problem"),
    ("think deeply about climate policy", "climate policy"),
    ("research the best laptop for ML", "the best laptop for ml"),
    ("do some research on rust async runtimes", "rust async runtimes"),
    ("do research on sleep hygiene", "sleep hygiene"),
    ("brainstorm names for my project", "names for my project"),
    ("brainstorm ideas for a birthday party", "a birthday party"),
    ("weigh the pros and cons of remote work", "remote work"),
])
def test_parse_positive(utterance, goal):
    req = parse_workflow_request(utterance)
    assert req is not None
    assert req.goal == goal


def test_parse_returns_workflow_request_with_trigger():
    req = parse_workflow_request("research transformers")
    assert isinstance(req, WorkflowRequest)
    assert req.trigger == "research"
    assert req.goal == "transformers"


def test_parse_strips_leading_filler():
    req = parse_workflow_request("hey agent, could you research neural nets")
    assert req is not None
    assert req.goal == "neural nets"


def test_parse_strips_trailing_filler():
    req = parse_workflow_request("research vector databases please")
    assert req is not None
    assert req.goal == "vector databases"


def test_parse_longest_trigger_wins():
    # "do some research on" must win over a hypothetical shorter "research" match.
    req = parse_workflow_request("do some research on graph theory")
    assert req is not None
    assert req.trigger == "do some research on"
    assert req.goal == "graph theory"


# --------------------------------------------------------------------------- #
# parse_workflow_request — negative
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("utterance", [
    "",
    "   ",
    "open chrome",
    "scroll down",
    "pain day on",
    "what's the weather like",
    "i did some research yesterday",   # trigger not at the start
    "research",                         # bare trigger, no goal
    "think hard about",                 # bare trigger, no goal
    "brainstorm please",                # trigger + only filler → empty goal
    "let's talk",                       # conversation-mode wake, not a workflow
])
def test_parse_negative(utterance):
    assert parse_workflow_request(utterance) is None


# --------------------------------------------------------------------------- #
# parse_decomposition
# --------------------------------------------------------------------------- #
def test_decomposition_basic_lines():
    text = "What is X?\nHow does X work?\nWhy does X matter?"
    assert parse_decomposition(text, 3) == [
        "What is X?", "How does X work?", "Why does X matter?",
    ]


def test_decomposition_strips_numbering_and_bullets():
    text = "1. First angle\n2) Second angle\n- Third angle\n* Fourth angle\n• Fifth angle"
    out = parse_decomposition(text, 5)
    assert out == ["First angle", "Second angle", "Third angle",
                   "Fourth angle", "Fifth angle"]


def test_decomposition_drops_blank_lines():
    text = "Angle one\n\n   \nAngle two\n"
    assert parse_decomposition(text, 5) == ["Angle one", "Angle two"]


def test_decomposition_dedupes_preserving_order():
    text = "Same angle\nSAME ANGLE\nDifferent angle"
    assert parse_decomposition(text, 5) == ["Same angle", "Different angle"]


def test_decomposition_caps_at_n():
    text = "a\nb\nc\nd\ne"
    assert parse_decomposition(text, 3) == ["a", "b", "c"]


def test_decomposition_empty_input():
    assert parse_decomposition("", 3) == []
    assert parse_decomposition(None, 3) == []


def test_decomposition_n_floor_is_one():
    # A degenerate n<=0 still returns at least the first line rather than nothing.
    assert parse_decomposition("only line\nsecond", 0) == ["only line"]


# --------------------------------------------------------------------------- #
# prompt builders
# --------------------------------------------------------------------------- #
def test_build_decompose_prompt_contains_goal_and_n():
    p = build_decompose_prompt("dark matter", 4)
    assert "dark matter" in p
    assert "4" in p


def test_build_synthesis_prompt_contains_goal_and_angles():
    p = build_synthesis_prompt("dark matter", ["finding A", "finding B"])
    assert "dark matter" in p
    assert "finding A" in p and "finding B" in p
    assert "Angle 1" in p and "Angle 2" in p


def test_build_synthesis_prompt_skips_empty_results():
    p = build_synthesis_prompt("g", ["real", "   ", ""])
    assert "real" in p
    # Only one non-empty angle, so there is no "Angle 2" block.
    assert "Angle 2" not in p


# --------------------------------------------------------------------------- #
# config helpers
# --------------------------------------------------------------------------- #
def _write_cfg(tmp_path, monkeypatch, block):
    cfg_dir = tmp_path / ".claude" / "ipad_bridge"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.json").write_text(
        json.dumps({"workflow_orchestration": block}), encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))


def test_config_default_disabled_when_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert workflow_voice_config() == {"enabled": False}


def test_config_reads_block(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch, {"enabled": True, "fanout_n": 5, "verify": True})
    cfg = workflow_voice_config()
    assert cfg.get("enabled") is True
    assert fanout_n_from_config(cfg) == 5
    assert verify_enabled(cfg) is True


def test_fanout_n_default_and_clamp():
    assert fanout_n_from_config(None) == DEFAULT_FANOUT_N
    assert fanout_n_from_config({}) == DEFAULT_FANOUT_N
    assert fanout_n_from_config({"fanout_n": 0}) == 1          # floor
    assert fanout_n_from_config({"fanout_n": 99}) == 6         # ceiling
    assert fanout_n_from_config({"fanout_n": "nope"}) == DEFAULT_FANOUT_N


def test_verify_disabled_by_default():
    assert verify_enabled(None) is False
    assert verify_enabled({}) is False
    assert verify_enabled({"verify": False}) is False
