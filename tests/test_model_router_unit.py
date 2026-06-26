"""Dedicated unit tests for inference/model_router.py contract methods.

ModelRouter is exercised indirectly by the eval/VRAM/infer-stream suites, but
several pure, contract-defining methods had no direct tripwire. These pin the
deterministic, VRAM-independent surface: edit-format resolution, the dev model
roster, heavy-model enumeration, RouterResult, and reasoning-preamble stripping.

Kept fast and non-flaky — nothing here calls Ollama or reads real VRAM.
"""

import pytest

from inference import model_router as mr
from inference.model_router import ModelRouter, RouterResult
from inference.model_router import VLLMSpecialistPool


@pytest.fixture
def router():
    return ModelRouter()


# --------------------------------------------------------------------------- #
# edit_format_for — resolution order (specs/edit-format-aci R3.1)
# --------------------------------------------------------------------------- #
def test_edit_format_default_is_whole_file_for_known_model(router):
    # Every shipped ModelProfile defaults to whole_file.
    assert router.edit_format_for("qwen3-coder:30b") == "whole_file"
    assert router.edit_format_for("llama3.1:8b") == "whole_file"


def test_edit_format_unknown_model_falls_back_to_whole_file(router):
    assert router.edit_format_for("some-model-we-never-heard-of") == "whole_file"


def test_edit_format_config_override_wins(router, monkeypatch):
    # A per-model config override (step 1) beats the profile default (step 2).
    monkeypatch.setitem(mr._EDIT_FORMAT_OVERRIDES, "qwen3-coder:30b", "hashline")
    assert router.edit_format_for("qwen3-coder:30b") == "hashline"


def test_edit_format_override_only_affects_named_model(router, monkeypatch):
    monkeypatch.setitem(mr._EDIT_FORMAT_OVERRIDES, "qwen3-coder:30b", "hashline")
    # A different model is untouched by the override.
    assert router.edit_format_for("llama3.1:8b") == "whole_file"


# --------------------------------------------------------------------------- #
# dev_model_roster — command domain excluded
# --------------------------------------------------------------------------- #
def test_dev_roster_excludes_command_only_models(router):
    roster = router.dev_model_roster()
    # llama3.2:3b appears ONLY in the command fallback chain → must be absent.
    assert "llama3.2:3b" not in roster


def test_dev_roster_includes_dev_specialists(router):
    roster = router.dev_model_roster()
    assert "qwen3-coder:30b" in roster   # code + plan
    assert "gemma4:12b" in roster        # general + fallbacks


# --------------------------------------------------------------------------- #
# heavy_model_names — flare-eviction roster
# --------------------------------------------------------------------------- #
def test_heavy_models_include_30b_specialists(router):
    heavy = router.heavy_model_names()
    assert "qwen3-coder:30b" in heavy
    assert "qwen3-vl:30b" in heavy


def test_heavy_models_exclude_light_models(router):
    heavy = router.heavy_model_names()
    # Light models are never evicted, so they must not appear in the heavy set.
    for light in ("llama3.1:8b", "llama3.2:3b", "deepseek-r1:8b", "gemma4:e4b-it-qat"):
        assert light not in heavy


def test_heavy_models_sorted_and_deduped(router):
    heavy = router.heavy_model_names()
    assert heavy == sorted(heavy)
    assert len(heavy) == len(set(heavy))


def test_heavy_models_threshold_is_belt_and_suspenders(router):
    # gemma4:12b is 9.1 GB (below the 12 GB profile threshold) but is a non-light
    # fallback-chain entry, so it is still treated as heavy/evictable.
    assert "gemma4:12b" in router.heavy_model_names(min_vram_gb=12.0)


# --------------------------------------------------------------------------- #
# RouterResult — pure properties
# --------------------------------------------------------------------------- #
def _result(text="", error=None):
    return RouterResult(text=text, model="m", domain="d", latency_ms=1.0,
                        free_form=True, error=error)


def test_router_result_ok_true_when_no_error():
    assert _result(text="hi").ok is True


def test_router_result_ok_false_when_error():
    assert _result(error="boom").ok is False


def test_router_result_first_line_skips_blank_lines():
    assert _result(text="\n   \nCLICK\nmore").first_line() == "CLICK"


def test_router_result_first_line_empty_for_blank_text():
    assert _result(text="   \n  ").first_line() == ""


# --------------------------------------------------------------------------- #
# _strip_thinking — reasoning-preamble removal (static, pure)
# --------------------------------------------------------------------------- #
def test_strip_think_tags():
    raw = "<think>let me reason about this</think>FINAL ANSWER"
    assert VLLMSpecialistPool._strip_thinking(raw) == "FINAL ANSWER"


def test_strip_thinking_tags_variant():
    raw = "<thinking>reasoning here</thinking>the answer"
    assert VLLMSpecialistPool._strip_thinking(raw) == "the answer"


def test_strip_unclosed_think_tag():
    raw = "<think>reasoning that never closes\nstill thinking"
    assert VLLMSpecialistPool._strip_thinking(raw) == ""


def test_strip_channel_format_keeps_final_segment():
    raw = "<|channel>thought: pondering<|channel>final: the result"
    assert VLLMSpecialistPool._strip_thinking(raw) == "the result"


def test_strip_thinking_preserves_code_with_angle_brackets():
    # No think/channel markers → only residual harmony tokens are scrubbed, real
    # code with comparison operators must survive untouched.
    raw = "if a < b and c > d:\n    return a"
    assert VLLMSpecialistPool._strip_thinking(raw) == raw


def test_strip_thinking_no_markers_is_identity():
    assert VLLMSpecialistPool._strip_thinking("plain answer") == "plain answer"
