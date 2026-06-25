"""Tests for inference/edit_format.py — edit-format ACI lint gate + applier.

Spec: specs/edit-format-aci/requirements.md. Each test cites the acceptance
criterion it covers. All pure-function — no model, no DevAgent loop.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from inference.dev_agent import AgentStep, DevAgent
from inference.edit_format import (
    EditApplier,
    EditError,
    HASHLINE,
    SEARCH_REPLACE,
    SEARCH_REPLACE_PROMPT_INSTRUCTIONS,
    UDIFF,
    WHOLE_FILE,
    hash_line,
    render_hashline,
)

GOOD_PY = "def f(x):\n    return x + 1\n"
BAD_PY = "def f(x):\n    return x +\n"  # dangling operator → SyntaxError


def _dev_agent(edit_format="whole_file"):
    """A DevAgent whose router resolves a fixed edit_format (no model loaded)."""
    agent = DevAgent(router=MagicMock())
    agent._router.edit_format_for = MagicMock(return_value=edit_format)
    return agent


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


def test_r3_3_reserved_udiff_degrades_until_implemented(caplog):
    """R3.3: the not-yet-implemented udiff format degrades to whole_file."""
    applier = EditApplier()
    with caplog.at_level("WARNING"):
        out = applier.apply("", GOOD_PY, edit_format=UDIFF, path="mod.py")
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
    agent = _dev_agent()
    target = tmp_path / "mod.py"
    target.write_text(GOOD_PY, encoding="utf-8")

    with pytest.raises(EditError):
        agent._apply_edit(str(target), BAD_PY)

    # File on disk is unchanged — the broken edit never landed.
    assert target.read_text(encoding="utf-8") == GOOD_PY


def test_r1_apply_edit_returns_text_for_valid_edit(tmp_path):
    """R1: a valid _apply_edit returns the new text (caller then writes it)."""
    agent = _dev_agent()
    target = tmp_path / "mod.py"
    target.write_text("old\n", encoding="utf-8")

    out = agent._apply_edit(str(target), GOOD_PY)
    assert out == GOOD_PY
    # _apply_edit itself does not write — the file is still the old content.
    assert target.read_text(encoding="utf-8") == "old\n"


def test_r1_apply_edit_handles_new_file(tmp_path):
    """R1: _apply_edit on a non-existent path reads '' as current, returns body."""
    agent = _dev_agent()
    target = tmp_path / "new.py"
    out = agent._apply_edit(str(target), GOOD_PY)
    assert out == GOOD_PY
    assert not target.exists()  # _apply_edit does not create the file


def test_r3_2_apply_edit_uses_active_plan_model_format(tmp_path):
    """R3.2: _apply_edit resolves the format from the active plan model name."""
    agent = _dev_agent()
    agent._active_plan_model = "qwen3-coder:30b"
    target = tmp_path / "mod.py"
    agent._apply_edit(str(target), GOOD_PY)
    agent._router.edit_format_for.assert_called_once_with("qwen3-coder:30b")


# --- R3.1: ModelRouter.edit_format_for resolution -----------------------------

def test_r3_1_resolver_defaults_to_whole_file_for_known_model():
    """R3.1: a profile with no override resolves to its edit_format default."""
    from inference.model_router import ModelRouter
    router = ModelRouter()
    # qwen3-coder:30b is the plan profile's model; default field is whole_file.
    assert router.edit_format_for("qwen3-coder:30b") == "whole_file"


def test_r3_1_resolver_unknown_model_falls_back_to_whole_file():
    """R3.1 step 3: an unrecognized model name resolves to whole_file."""
    from inference.model_router import ModelRouter
    router = ModelRouter()
    assert router.edit_format_for("not-a-real-model") == "whole_file"
    assert router.edit_format_for("") == "whole_file"


def test_r3_1_config_override_wins(monkeypatch):
    """R3.1 step 1: a config per_model override beats the profile default."""
    import inference.model_router as mr
    monkeypatch.setattr(
        mr, "_EDIT_FORMAT_OVERRIDES", {"qwen3-coder:30b": "hashline"}
    )
    router = mr.ModelRouter()
    assert router.edit_format_for("qwen3-coder:30b") == "hashline"


# --- R4: hashline format ------------------------------------------------------

SAMPLE = "def f(x):\n    return x + 1\n\nclass C:\n    pass\n"


def _op(verb, lineno, content_lines, text):
    """Build one @@ hashline op, taking the hash from `text`'s actual line."""
    line = text.split("\n")[lineno - 1]
    header = f"@@ {verb} {lineno}:{hash_line(line)}"
    return "\n".join([header, *content_lines])


def test_r4_hash_line_is_whitespace_insensitive():
    """R4: the anchor hash is computed on stripped content (handoff §7)."""
    assert hash_line("    return x  ") == hash_line("return x")
    assert len(hash_line("anything")) == 2


def test_r4_render_hashline_shape():
    """R4: render is 'lineno:hash|content', hash agreeing with hash_line."""
    rendered = render_hashline("alpha\nbeta")
    lines = rendered.split("\n")
    assert lines[0] == f"1:{hash_line('alpha')}|alpha"
    assert lines[1] == f"2:{hash_line('beta')}|beta"


def test_r4_replace_applies_at_anchor():
    """R4.1: a REPLACE op swaps the anchored line."""
    applier = EditApplier()
    payload = _op("REPLACE", 2, ["    return x + 2"], SAMPLE)
    out = applier.apply(SAMPLE, payload, edit_format=HASHLINE, path="mod.py")
    assert out == SAMPLE.replace("return x + 1", "return x + 2")


def test_r4_delete_removes_anchored_line():
    """R4.1: a DELETE op drops the anchored line (here the blank separator)."""
    applier = EditApplier()
    payload = _op("DELETE", 3, [], SAMPLE)  # the blank line between f and C
    out = applier.apply(SAMPLE, payload, edit_format=HASHLINE, path="mod.py")
    assert "return x + 1\nclass C:" in out  # blank separator gone
    assert "    pass" in out                # rest intact


def test_r4_insert_after_and_before():
    """R4.1: INSERT_AFTER / INSERT_BEFORE add lines without removing the anchor."""
    applier = EditApplier()
    after = applier.apply(
        SAMPLE, _op("INSERT_AFTER", 1, ['    """doc"""'], SAMPLE),
        edit_format=HASHLINE, path="mod.py",
    )
    assert after.split("\n")[1] == '    """doc"""'
    assert after.split("\n")[0] == "def f(x):"

    before = applier.apply(
        SAMPLE, _op("INSERT_BEFORE", 4, ["# a class"], SAMPLE),
        edit_format=HASHLINE, path="mod.py",
    )
    assert "# a class\nclass C:" in before


def test_r2_1_stale_anchor_rejected_with_diagnostic():
    """R2.1/R4: a wrong hash → mismatch EditError naming the stale anchor."""
    applier = EditApplier()
    # Anchor line 2 but with a deliberately wrong hash.
    payload = "@@ REPLACE 2:zz\n    return x + 2".replace("zz", "ff")
    # Ensure 'ff' is actually wrong for line 2.
    if hash_line(SAMPLE.split("\n")[1]) == "ff":
        payload = payload.replace("2:ff", "2:00")
    with pytest.raises(EditError) as ei:
        applier.apply(SAMPLE, payload, edit_format=HASHLINE, path="pkg/mod.py")
    assert ei.value.reason == "mismatch"
    assert "stale" in str(ei.value)
    assert "pkg/mod.py" in str(ei.value)


def test_r4_2_fuzzy_relocates_shifted_anchor():
    """R4.2: when lines shifted, a nearby line with the matching hash relocates."""
    applier = EditApplier()
    text = "a\nb\nc\n"
    payload = _op("REPLACE", 2, ["B"], text)  # anchored on "b" at line 2
    shifted = "x\n" + text                      # "b" is now at line 3
    out = applier.apply(shifted, payload, edit_format=HASHLINE, path="f.txt")
    assert out == "x\na\nB\nc\n"


def test_r4_3_overlapping_edits_rejected():
    """R4.3: two edits resolving to the same line fail atomically."""
    applier = EditApplier()
    payload = (
        _op("REPLACE", 2, ["    return x + 2"], SAMPLE)
        + "\n"
        + _op("INSERT_AFTER", 2, ["    # note"], SAMPLE)
    )
    with pytest.raises(EditError) as ei:
        applier.apply(SAMPLE, payload, edit_format=HASHLINE, path="mod.py")
    assert ei.value.reason == "overlap"


def test_r4_3_bottom_up_multi_edit_no_anchor_shift():
    """R4.3: a multi-line replace earlier must not shift a later anchor."""
    applier = EditApplier()
    text = "1\n2\n3\n4\n5\n"
    payload = (
        _op("REPLACE", 2, ["2a", "2b"], text)   # grows the file
        + "\n"
        + _op("REPLACE", 4, ["4x"], text)        # later anchor must still hit "4"
    )
    out = applier.apply(text, payload, edit_format=HASHLINE, path="f.txt")
    assert out == "1\n2a\n2b\n3\n4x\n5\n"


def test_r4_no_parseable_ops_rejected():
    """R4: a hashline payload with no @@ headers is a mismatch EditError."""
    applier = EditApplier()
    with pytest.raises(EditError) as ei:
        applier.apply(SAMPLE, "just some prose", edit_format=HASHLINE, path="mod.py")
    assert ei.value.reason == "mismatch"
    assert "no parseable edit ops" in str(ei.value)


def test_r4_lint_gate_runs_after_hashline_apply():
    """R1+R4: a hashline edit that yields invalid Python is rejected by the lint."""
    applier = EditApplier()
    good = "def f(x):\n    return x + 1\n"
    payload = _op("REPLACE", 2, ["    return x +"], good)  # dangling → SyntaxError
    with pytest.raises(EditError) as ei:
        applier.apply(good, payload, edit_format=HASHLINE, path="mod.py")
    assert ei.value.reason == "syntax"


def test_r4_prompt_instructions_describe_the_ops():
    """R4: the hashline prompt block names every op so the model can emit them."""
    from inference.edit_format import HASHLINE_PROMPT_INSTRUCTIONS as instr
    for marker in ("@@ REPLACE", "@@ DELETE", "@@ INSERT_AFTER", "@@ INSERT_BEFORE"):
        assert marker in instr


# --- R4: READ_FILE hashline rendering (DevAgent wiring) ------------------------

async def test_r4_read_file_renders_hashline_for_hashline_model(tmp_path):
    """R4: a hashline plan model gets line:hash-anchored READ_FILE output."""
    agent = _dev_agent(edit_format="hashline")
    agent._active_plan_model = "qwen3-coder:30b"
    f = tmp_path / "m.py"
    f.write_text("alpha\nbeta\n", encoding="utf-8")
    out = await agent._execute_step(AgentStep(action="READ_FILE", args=str(f)))
    assert out == render_hashline("alpha\nbeta\n")


async def test_r4_read_file_raw_for_whole_file_model(tmp_path):
    """R3.1: a whole_file model gets raw READ_FILE output (no anchors)."""
    agent = _dev_agent(edit_format="whole_file")
    f = tmp_path / "m.py"
    f.write_text("alpha\nbeta\n", encoding="utf-8")
    out = await agent._execute_step(AgentStep(action="READ_FILE", args=str(f)))
    assert out == "alpha\nbeta\n"


def test_r3_1_profile_default_honored_when_set(monkeypatch):
    """R3.1 step 2: a profile whose edit_format is set is returned (no override).

    Uses deepseek-r1:8b (math), whose name is unique to one profile — avoids the
    qwen3-coder:30b collision (shared by the code + plan profiles).
    """
    import inference.model_router as mr
    router = mr.ModelRouter()
    router._profiles["math"].edit_format = "udiff"
    try:
        assert router.edit_format_for("deepseek-r1:8b") == "udiff"
    finally:
        router._profiles["math"].edit_format = "whole_file"


# --- R5: EDIT_FILE / SEARCH_REPLACE surgical edits ----------------------------
#
# EDIT_FILE applies aider-style SEARCH/REPLACE blocks. The core contract is
# fail-closed: a SEARCH that doesn't match the current text EXACTLY ONCE aborts
# the whole batch and nothing is written. It reuses the same lint gate as
# WRITE_FILE, so a broken-Python result is still rejected pre-write.

_SR_SRC = "def foo():\n    return 1\n\n\ndef bar():\n    return 2\n"


def _sr_block(search: str, replace: str) -> str:
    return f"<<<<<<< SEARCH\n{search}\n=======\n{replace}\n>>>>>>> REPLACE"


def _dev_agent_no_critic(edit_format="whole_file"):
    """A DevAgent with Critic + Tester forced OFF (no model calls in dispatch)."""
    agent = _dev_agent(edit_format)
    agent._critic = None
    agent._critic_enabled = False
    agent._tester = None
    agent._tester_enabled = False
    return agent


def test_r5_search_replace_applies_unique_block():
    """R5: a SEARCH that matches exactly once is replaced; rest of file intact."""
    applier = EditApplier()
    out = applier.apply(
        _SR_SRC, _sr_block("    return 1", "    return 42"),
        edit_format=SEARCH_REPLACE, path="m.py",
    )
    assert "return 42" in out and "return 2" in out
    assert "return 1\n" not in out


def test_r5_multi_block_applies_in_order():
    """R5: multiple blocks each apply against the running text."""
    applier = EditApplier()
    payload = (
        _sr_block("    return 1", "    return 11") + "\n"
        + _sr_block("    return 2", "    return 22")
    )
    out = applier.apply(_SR_SRC, payload, edit_format=SEARCH_REPLACE, path="m.py")
    assert "return 11" in out and "return 22" in out


def test_r5_search_not_found_fails_closed():
    """R5: a SEARCH absent from the file raises EditError(mismatch) — no write."""
    applier = EditApplier()
    with pytest.raises(EditError) as ei:
        applier.apply(
            _SR_SRC, _sr_block("    return 999", "    return 0"),
            edit_format=SEARCH_REPLACE, path="m.py",
        )
    assert ei.value.reason == "mismatch"
    assert "not found" in str(ei.value).lower()


def test_r5_ambiguous_search_fails_closed():
    """R5: a SEARCH matching >1 location is rejected (must be unique)."""
    applier = EditApplier()
    src = "x = 1\nx = 1\n"
    with pytest.raises(EditError) as ei:
        applier.apply(
            src, _sr_block("x = 1", "x = 2"),
            edit_format=SEARCH_REPLACE, path="m.py",
        )
    assert ei.value.reason == "mismatch"
    assert "match" in str(ei.value).lower()


def test_r5_broken_python_result_rejected_pre_write():
    """R5 + R1.2: a SEARCH/REPLACE whose result is invalid Python is rejected."""
    applier = EditApplier()
    with pytest.raises(EditError) as ei:
        applier.apply(
            _SR_SRC, _sr_block("    return 1", "    return ("),
            edit_format=SEARCH_REPLACE, path="m.py",
        )
    assert ei.value.reason == "syntax"


def test_r5_non_python_result_not_linted():
    """R1.3: a non-.py SEARCH/REPLACE result is not lint-gated."""
    applier = EditApplier()
    out = applier.apply(
        "key: 1\n", _sr_block("key: 1", "key: 2"),
        edit_format=SEARCH_REPLACE, path="conf.yaml",
    )
    assert out == "key: 2\n"


def test_r5_empty_replace_deletes():
    """R5: an empty REPLACE section deletes the matched region."""
    applier = EditApplier()
    src = "a\nDELETE ME\nb\n"
    out = applier.apply(
        src, _sr_block("DELETE ME\n", ""),
        edit_format=SEARCH_REPLACE, path="m.txt",
    )
    assert "DELETE ME" not in out and "a\n" in out and "b\n" in out


def test_r5_empty_search_creates_empty_file_only():
    """R5: empty SEARCH is creation on an empty file, refused on a non-empty one."""
    applier = EditApplier()
    # Empty file → creation.
    out = applier.apply("", _sr_block("", "hello\n"), edit_format=SEARCH_REPLACE,
                        path="new.txt")
    assert out == "hello\n"
    # Non-empty file → refuse (use WRITE_FILE to rewrite).
    with pytest.raises(EditError) as ei:
        applier.apply("existing\n", _sr_block("", "x\n"),
                      edit_format=SEARCH_REPLACE, path="m.txt")
    assert ei.value.reason == "mismatch"


def test_r5_no_parseable_blocks_rejected():
    """R5: a payload with no well-formed blocks fails closed (never a no-op)."""
    applier = EditApplier()
    with pytest.raises(EditError) as ei:
        applier.apply(_SR_SRC, "just some prose, no blocks",
                      edit_format=SEARCH_REPLACE, path="m.py")
    assert ei.value.reason == "mismatch"


def test_r5_lenient_marker_lengths():
    """R5: 6- or 8-char marker fences still parse (Aider leniency)."""
    applier = EditApplier()
    payload = "<<<<<< SEARCH\n    return 1\n======\n    return 7\n>>>>>> REPLACE"
    out = applier.apply(_SR_SRC, payload, edit_format=SEARCH_REPLACE, path="m.py")
    assert "return 7" in out


def test_r5_apply_edit_override_forces_search_replace(tmp_path):
    """R5: EDIT_FILE passes SEARCH_REPLACE explicitly — ignores the model's knob."""
    # Model knob is whole_file, but the override wins.
    agent = _dev_agent(edit_format="whole_file")
    target = tmp_path / "m.py"
    target.write_text(_SR_SRC, encoding="utf-8")
    out = agent._apply_edit(str(target), _sr_block("    return 1", "    return 9"),
                            SEARCH_REPLACE)
    assert "return 9" in out
    # The per-model resolver is NOT consulted when an override is given.
    agent._router.edit_format_for.assert_not_called()
    # _apply_edit does not write — file still original.
    assert target.read_text(encoding="utf-8") == _SR_SRC


async def test_r5_execute_step_edit_file_writes_and_snapshots(tmp_path):
    """R5: EDIT_FILE through _execute_step edits the file and captures a snapshot."""
    agent = _dev_agent_no_critic()
    agent._confirm_destructive_op = AsyncMock(return_value=True)
    target = tmp_path / "m.py"
    target.write_text(_SR_SRC, encoding="utf-8")
    step = AgentStep(action="EDIT_FILE", args=str(target),
                     body=_sr_block("    return 1", "    return 5"))
    result = await agent._execute_step(step)
    assert "Written" in result
    assert "return 5" in target.read_text(encoding="utf-8")
    # Saga snapshot captured before the write so a rollback can restore it: the
    # snapshot records that the file pre-existed (RESTORE_FILE, not DELETE_FILE).
    assert step.comp_args is not None
    import json as _json
    assert _json.loads(step.comp_args)["existed"] is True


async def test_r5_execute_step_edit_file_mismatch_leaves_file_untouched(tmp_path):
    """R5: a non-matching EDIT_FILE fails closed — file unchanged, no snapshot."""
    agent = _dev_agent_no_critic()
    agent._confirm_destructive_op = AsyncMock(return_value=True)
    target = tmp_path / "m.py"
    target.write_text(_SR_SRC, encoding="utf-8")
    step = AgentStep(action="EDIT_FILE", args=str(target),
                     body=_sr_block("    return 404", "    return 5"))
    with pytest.raises(EditError):
        await agent._execute_step(step)
    assert target.read_text(encoding="utf-8") == _SR_SRC
    assert step.comp_args is None  # apply failed before snapshot


def test_r5_prompt_instructions_describe_blocks():
    """R5: the planner instructions document the SEARCH/REPLACE block syntax."""
    txt = SEARCH_REPLACE_PROMPT_INSTRUCTIONS
    assert "SEARCH" in txt and "REPLACE" in txt
    assert "EDIT_FILE" in txt
