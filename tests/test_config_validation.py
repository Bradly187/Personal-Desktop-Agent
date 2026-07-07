"""Tests for core/config_validation.py — approval_config.json schema gate (IG-9).

The gate must: pass the shipped config unchanged, catch unknown-key typos and
bad policy values as errors (SystemExit at startup), treat cosmetic type
problems as warnings only, and stay fail-safe on a missing file.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.config_validation import (
    check_approval_config_at_startup,
    validate_approval_config,
)

_REPO = Path(__file__).resolve().parent.parent


def test_shipped_config_validates_clean():
    cfg = json.loads((_REPO / "approval_config.json").read_text(encoding="utf-8"))
    errors, warnings = validate_approval_config(cfg)
    assert errors == [], errors
    assert warnings == [], warnings


def test_underscore_keys_are_ignored():
    errors, warnings = validate_approval_config({"_comment": "x", "_anything": [1]})
    assert errors == [] and warnings == []


def test_unknown_key_is_error():
    errors, _ = validate_approval_config({"tool": {"Bash": "approve"}})  # typo of "tools"
    assert len(errors) == 1 and "unknown key 'tool'" in errors[0]


def test_bad_tool_policy_is_error():
    errors, _ = validate_approval_config({"tools": {"Bash": "aprove"}})
    assert len(errors) == 1 and "aprove" in errors[0]


def test_non_dict_tools_is_error():
    errors, _ = validate_approval_config({"tools": ["Bash"]})
    assert len(errors) == 1


def test_bad_timeout_action_is_error():
    errors, _ = validate_approval_config({"timeout_action": "ignore"})
    assert len(errors) == 1 and "timeout_action" in errors[0]


def test_cosmetic_type_problem_is_warning_not_error():
    errors, warnings = validate_approval_config({"kokoro_speed": "fast"})
    assert errors == []
    assert len(warnings) == 1 and "kokoro_speed" in warnings[0]


def test_unknown_tts_backend_is_warning():
    errors, warnings = validate_approval_config({"tts_backend": "chatterbox"})
    assert errors == []
    assert len(warnings) == 1 and "chatterbox" in warnings[0]


def test_startup_missing_file_is_ok(tmp_path):
    check_approval_config_at_startup(tmp_path / "nope.json")  # must not raise


def test_startup_invalid_json_exits(tmp_path):
    p = tmp_path / "approval_config.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(SystemExit):
        check_approval_config_at_startup(p)


def test_startup_typo_key_exits(tmp_path):
    p = tmp_path / "approval_config.json"
    p.write_text(json.dumps({"toolz": {}}), encoding="utf-8")
    with pytest.raises(SystemExit):
        check_approval_config_at_startup(p)


def test_startup_valid_config_passes(tmp_path):
    p = tmp_path / "approval_config.json"
    p.write_text(
        json.dumps({"tools": {"Bash": "approve"}, "timeout_action": "reject"}),
        encoding="utf-8",
    )
    check_approval_config_at_startup(p)  # must not raise
