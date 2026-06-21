"""Tests for inference/edit_format.py — edit-format ACI lint gate + applier.

Spec: specs/edit-format-aci/requirements.md. Each test cites the acceptance
criterion it covers. All pure-function — no model, no DevAgent loop.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from inference.dev_agent import DevAgent
from inference.edit_format import (
    EditApplier,
    EditError,
    HASHLINE,
    UDIFF,
    WHOLE_FILE,
)

GOOD_PY = "def f(x):\n    return x + 1\n"
BAD_PY = "def f(x):\n    return x +\n"  # dangling operator → SyntaxError


# --- R1.2 / R1.3: lint gate ---------------------------------------------------

def test_r1_2_rejects_invalid_python_with_editerror():
    """R1.2: a result that fails ast.parse raises EditError(reason='syntax')."""
    applier = EditApplier()
    with pytest.raises(EditError) as ei:
        applier.apply("", BAD_PY, edit_format=WHOLE_FILE, path="mod.py")
    assert ei.value.reason == "syntax"


def test_r1_2_diagnostic_names_file_and_says_not_modified():
    """R2.2: the message carries the validator detail + 'NOT modified' + path."""
    applier = EditApplier()
    with pytest.raises(EditError) as ei:
        applier.apply("", BAD_PY, edit_format=WHOLE_FILE, path="pkg/mod.py")
    msg = str(ei.value)
    assert "pkg/mod.py" in msg
    assert "NOT modified" in msg
    assert "SyntaxError" in msg


def test_r1_2_valid_python_passes_through_unchanged():
    """R1: a syntactically valid Python edit returns the payload verbatim."""
    applier = EditApplier()
    assert applier.apply("old\n", GOOD_PY, path="mod.py") == GOOD_PY


def test_r1_3_non_python_path_is_not_linted():
    """R1.3: a path with no registered validator writes through (no lint).

    The 'content' would be invalid Python but the .txt extension is not linted.
    """
    applier = EditApplier()
    assert applier.apply("", BAD_PY, path="notes.txt") == BAD_PY


def test_r1_3_extensionless_path_is_not_linted():
    """R1.3: an extensionless path has no validator → pass-through."""
    applier = EditApplier()
    assert applier.apply("", BAD_PY, path="Makefile") == BAD_PY


# --- R3.1 / R3.3: format knob + graceful fallback -----------------------------

def test_r3_1_default_format_is_whole_file_byte_identical():
    """R3.1: default edit_format returns payload byte-identical for valid text."""
    applier = EditApplier()
    payload = "hello\nworld\n"
    assert applier.apply("prev\n", payload, path="x.txt") == payload


def test_r3_3_unknown_format_falls_back_to_whole_file(caplog):
    """R3.3: an unknown edit_format warns and degrades to whole_file (no crash)."""
    applier = EditApplier()
    with caplog.at_level("WARNING"):
        out = applier.apply("", GOOD_PY, edit_format="nonsense", path="mod.py")
    assert out == GOOD_PY
    assert any("falling back" in r.message for r in caplog.records)


@pytest.mark.parametrize("fmt", [UDIFF, HASHLINE])
def test_r3_3_reserved_formats_degrade_until_implemented(fmt, caplog):
    """R3.3: reserved (not-yet-implemented) formats degrade to whole_file."""
    applier = EditApplier()
    with caplog.at_level("WARNING"):
        out = applier.apply("", GOOD_PY, edit_format=fmt, path="mod.py")
    assert out == GOOD_PY
    assert any("not yet implemented" in r.message for r in caplog.records)


# --- validator injection (test seam) ------------------------------------------

def test_custom_validator_registry_overrides_default():
    """A caller-supplied validator map replaces the default (.py un-linted here)."""
    applier = EditApplier(validators={})  # no validators registered
    # BAD_PY would normally be rejected; with an empty registry it passes.
    assert applier.apply("", BAD_PY, path="mod.py") == BAD_PY


def test_custom_validator_is_invoked_and_can_reject():
    """A registered validator for a new extension fires and can raise EditError."""
    def reject_all(text, path):
        raise EditError("mismatch", f"nope for {path}")

    applier = EditApplier(validators={".q": reject_all})
    with pytest.raises(EditError) as ei:
        applier.apply("", "anything", path="file.q")
    assert ei.value.reason == "mismatch"


# --- R1.4 / R2.4: DevAgent wiring (file untouched on reject) -------------------

def test_r1_4_apply_edit_leaves_existing_file_untouched_on_reject(tmp_path):
    """R1.4: a lint-failed _apply_edit raises and never mutates the file.

    The DevAgent WRITE_FILE branch calls _apply_edit BEFORE snapshot/write, so a
    raise here means no snapshot is taken and no bytes are written (fail-closed).
    """
    agent = DevAgent(router=MagicMock())
    target = tmp_path / "mod.py"
    target.write_text(GOOD_PY, encoding="utf-8")

    with pytest.raises(EditError):
        agent._apply_edit(str(target), BAD_PY)

    # File on disk is unchanged — the broken edit never landed.
    assert target.read_text(encoding="utf-8") == GOOD_PY


def test_r1_apply_edit_returns_text_for_valid_edit(tmp_path):
    """R1: a valid _apply_edit returns the new text (caller then writes it)."""
    agent = DevAgent(router=MagicMock())
    target = tmp_path / "mod.py"
    target.write_text("old\n", encoding="utf-8")

    out = agent._apply_edit(str(target), GOOD_PY)
    assert out == GOOD_PY
    # _apply_edit itself does not write — the file is still the old content.
    assert target.read_text(encoding="utf-8") == "old\n"


def test_r1_apply_edit_handles_new_file(tmp_path):
    """R1: _apply_edit on a non-existent path reads '' as current, returns body."""
    agent = DevAgent(router=MagicMock())
    target = tmp_path / "new.py"
    out = agent._apply_edit(str(target), GOOD_PY)
    assert out == GOOD_PY
    assert not target.exists()  # _apply_edit does not create the file
