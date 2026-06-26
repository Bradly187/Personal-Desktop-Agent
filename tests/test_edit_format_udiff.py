"""Tests for the udiff edit format (specs/edit-format-aci R4, task 4).

udiff = no-line-number unified diff: each hunk's context+removed lines are
located in the file (layered exact -> whitespace -> fuzzy, R4.2) and replaced by
its context+added lines, atomically with overlap rejection and bottom-up apply
(R4.3). Fail-closed on any unlocatable/ambiguous hunk (R2.1).
"""

from __future__ import annotations

import pytest

from inference.edit_format import (
    UDIFF,
    EditApplier,
    EditError,
    _parse_udiff_hunks,
)


@pytest.fixture
def applier():
    return EditApplier()


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #
def test_parse_splits_context_removed_added():
    payload = "@@ @@\n ctx\n-old\n+new\n"
    hunks = _parse_udiff_hunks(payload)
    assert len(hunks) == 1
    assert hunks[0].before == ["ctx", "old"]
    assert hunks[0].after == ["ctx", "new"]


def test_parse_ignores_file_headers_and_hunk_line_numbers():
    payload = "--- a/x.py\n+++ b/x.py\n@@ -3,2 +3,2 @@\n a\n-b\n+c\n"
    hunks = _parse_udiff_hunks(payload)
    assert hunks[0].before == ["a", "b"]
    assert hunks[0].after == ["a", "c"]


def test_parse_multiple_hunks():
    payload = "@@ @@\n a\n-b\n+B\n@@ @@\n y\n-z\n+Z\n"
    hunks = _parse_udiff_hunks(payload)
    assert len(hunks) == 2
    assert hunks[1].before == ["y", "z"] and hunks[1].after == ["y", "Z"]


def test_parse_no_header_is_single_hunk():
    hunks = _parse_udiff_hunks(" keep\n-drop\n+add\n")
    assert len(hunks) == 1
    assert hunks[0].before == ["keep", "drop"]


# --------------------------------------------------------------------------- #
# apply — happy paths
# --------------------------------------------------------------------------- #
def test_apply_replaces_single_line_with_context(applier):
    src = "def dec(n):\n    # off by one\n    return n - 1\n"
    diff = "@@ @@\n     # off by one\n-    return n - 1\n+    return n\n"
    out = applier.apply(src, diff, edit_format=UDIFF, path="m.py")
    assert "return n\n" in out and "return n - 1" not in out


def test_apply_pure_deletion(applier):
    src = "a = 1\nb = 2\nc = 3\n"
    diff = "@@ @@\n a = 1\n-b = 2\n c = 3\n"
    out = applier.apply(src, diff, edit_format=UDIFF, path="m.py")
    assert out == "a = 1\nc = 3\n"


def test_apply_insertion_with_context(applier):
    src = "x = 1\nz = 3\n"
    diff = "@@ @@\n x = 1\n+y = 2\n z = 3\n"
    out = applier.apply(src, diff, edit_format=UDIFF, path="m.py")
    assert out == "x = 1\ny = 2\nz = 3\n"


def test_apply_two_nonoverlapping_hunks(applier):
    src = "a = 1\nb = 2\nc = 3\nd = 4\n"
    diff = "@@ @@\n a = 1\n-b = 2\n+b = 20\n@@ @@\n c = 3\n-d = 4\n+d = 40\n"
    out = applier.apply(src, diff, edit_format=UDIFF, path="m.py")
    assert out == "a = 1\nb = 20\nc = 3\nd = 40\n"


def test_apply_creates_empty_file_from_pure_insertion(applier):
    diff = "@@ @@\n+hello = 1\n+world = 2\n"
    out = applier.apply("", diff, edit_format=UDIFF, path="m.py")
    assert out == "hello = 1\nworld = 2"


def test_apply_whitespace_normalized_match(applier):
    # File has trailing whitespace the model's hunk lacks — ws-normalized layer hits.
    src = "def f():  \n    return 1  \n"
    diff = "@@ @@\n def f():\n-    return 1\n+    return 2\n"
    out = applier.apply(src, diff, edit_format=UDIFF, path="m.py")
    assert "return 2" in out


# --------------------------------------------------------------------------- #
# apply — fail-closed (R2.1 / R4.2 / R4.3)
# --------------------------------------------------------------------------- #
def test_unlocatable_hunk_raises_and_writes_nothing(applier):
    src = "a = 1\nb = 2\n"
    diff = "@@ @@\n totally = 'absent'\n-nope = 0\n+nope = 1\n"
    with pytest.raises(EditError) as ei:
        applier.apply(src, diff, edit_format=UDIFF, path="m.py")
    assert ei.value.reason == "mismatch"


def test_ambiguous_context_raises(applier):
    src = "dup\ndup\n"
    diff = "@@ @@\n-dup\n+changed\n"
    with pytest.raises(EditError) as ei:
        applier.apply(src, diff, edit_format=UDIFF, path="m.py")
    assert ei.value.reason == "mismatch"
    assert "matches" in str(ei.value)


def test_empty_payload_raises(applier):
    with pytest.raises(EditError):
        applier.apply("a = 1\n", "   \n", edit_format=UDIFF, path="m.py")


def test_pure_insertion_into_nonempty_file_rejected(applier):
    # All-'+' hunk with no context can't anchor into a non-empty file.
    with pytest.raises(EditError):
        applier.apply("existing = 1\n", "@@ @@\n+new = 2\n", edit_format=UDIFF, path="m.py")


def test_overlapping_hunks_rejected(applier):
    src = "a = 1\nb = 2\nc = 3\n"
    # Both hunks target the b=2 line region.
    diff = ("@@ @@\n a = 1\n-b = 2\n+b = 20\n"
            "@@ @@\n-b = 2\n+b = 99\n c = 3\n")
    with pytest.raises(EditError) as ei:
        applier.apply(src, diff, edit_format=UDIFF, path="m.py")
    assert ei.value.reason in ("overlap", "mismatch")


def test_udiff_result_is_lint_gated(applier):
    # A udiff that yields broken Python must fail the ast.parse validator (R1).
    src = "def f():\n    return 1\n"
    diff = "@@ @@\n def f():\n-    return 1\n+    return (\n"
    with pytest.raises(EditError) as ei:
        applier.apply(src, diff, edit_format=UDIFF, path="m.py")
    assert ei.value.reason == "syntax"


def test_udiff_non_python_not_linted(applier):
    src = "name: old\n"
    diff = "@@ @@\n-name: old\n+name: new\n"
    out = applier.apply(src, diff, edit_format=UDIFF, path="cfg.yaml")
    assert out == "name: new\n"
