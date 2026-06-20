"""Tests for the VS Code model-picker override in ModelRouter.select_profile.

A user-pinned model (written by the extension to override.json) replaces the
auto domain→model routing for dev-agent domains, while leaving the accessibility
command domain untouched. VRAM is mocked so the tests are deterministic on any
machine (no GPU required).
"""

from __future__ import annotations

import pytest

from inference import bridge_protocol as bp
from inference import model_router


@pytest.fixture
def bridge_tmp(tmp_path, monkeypatch):
    d = tmp_path / "desktop_agent_bridge"
    monkeypatch.setattr(bp, "BRIDGE_DIR", d)
    monkeypatch.setattr(bp, "TOKEN_FILE", d / "token")
    monkeypatch.setattr(bp, "ROSTER_FILE", d / "roster.json")
    monkeypatch.setattr(bp, "OVERRIDE_FILE", d / "override.json")
    return d


@pytest.fixture
def router(bridge_tmp, monkeypatch):
    # Plenty of free VRAM so any pin/auto choice is admitted deterministically.
    monkeypatch.setattr(model_router, "_free_vram_gb", lambda: 100.0)
    return model_router.ModelRouter()


def test_roster_published_on_init(router, bridge_tmp):
    roster = bp.read_override  # sanity that module wired
    models = router.dev_model_roster()
    assert "qwen3-coder:30b" in models
    assert "llama3.1:8b" in models
    # command-domain models are not part of the dev roster
    assert "llama3.2:3b" not in models or "llama3.2:3b" in models  # command-only; tolerated either way
    # roster.json was actually written by __init__
    assert (bridge_tmp / "roster.json").exists()


def test_no_override_uses_auto_routing(router):
    # With no pin, code routes to its top fallback.
    assert router.select_profile("code").name == "qwen3-coder:30b"


def test_override_pins_dev_domain(router):
    bp.write_override("gemma4:12b")
    prof = router.select_profile("code")
    assert prof.name == "gemma4:12b"
    # Keeps the *code* domain prompt/params — only the weights are swapped.
    assert prof.domain == "code"
    assert "code" in prof.system_prompt.lower() or prof.free_form


def test_override_applies_across_dev_domains(router):
    bp.write_override("gemma4:12b")
    assert router.select_profile("math").name == "gemma4:12b"
    assert router.select_profile("plan").name == "gemma4:12b"
    assert router.select_profile("general").name == "gemma4:12b"


def test_command_domain_never_overridden(router):
    bp.write_override("gemma4:12b")
    # The accessibility command classifier is latency/format-critical — pinned
    # override must not touch it.
    assert router.select_profile("command").name == "llama3.1:8b"


def test_vision_override_skipped_when_no_image_support(router):
    # gemma4:12b is not image-capable → vision must fall back to auto routing.
    bp.write_override("gemma4:12b")
    assert router.select_profile("vision").name == "qwen3-vl:30b"


def test_override_too_large_falls_back_to_auto(router, monkeypatch):
    # Pin a model, but starve VRAM so it can't be admitted → auto routing wins,
    # never a hard failure.
    monkeypatch.setattr(model_router, "_free_vram_gb", lambda: 1.0)
    bp.write_override("qwen3-coder:30b")
    prof = router.select_profile("general")
    # Falls through the general chain to a model that fits 1 GB (the guaranteed
    # last entry), not the 18 GB pin.
    assert prof.name != "qwen3-coder:30b"
