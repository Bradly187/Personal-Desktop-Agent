"""Tests for trajectory read-deduplication (specs/trajectory-read-dedup, Gap B).

One assertion per numbered acceptance criterion (cited in the test name). Dedup is
independent of DA_TRAJECTORY_REDUCE (R4.2): it runs as a pre-pass and composes with
reduction on or off.
"""

from __future__ import annotations

import pytest

from inference.dev_agent import AgentStep, DevAgent
from inference.trajectory import render_trajectory, _dedup_reads

RO = DevAgent._PARALLEL_VERBS


def _step(action, args="", result="", success=True):
    return AgentStep(action=action, args=args, result=result, success=success)


def _render(steps, *, dedup, enabled=False, keep_verbatim=2):
    return render_trajectory(
        steps, style="replan", keep_verbatim=keep_verbatim,
        readonly_verbs=RO, enabled=enabled, dedup_reads=dedup,
    )


# -- R1: deduplicate superseded older reads ---------------------------------- #

def test_r1_1_older_duplicate_read_collapsed_last_kept():
    steps = [
        _step("READ_FILE", "foo.py", result="VERSION ONE of foo"),   # 0 older, superseded
        _step("GREP", "bar baz", result="grep hit"),                 # 1 older
        _step("READ_FILE", "foo.py", result="VERSION TWO of foo"),   # 2 older, last read of foo
        _step("EXPLAIN", "done", result="ok"),                       # 3 verbatim
        _step("EXPLAIN", "done2", result="ok"),                      # 4 verbatim
    ]
    sup = _dedup_reads(steps, keep_verbatim=2, readonly_verbs=RO)
    assert sup == {0}                       # first foo read superseded, second kept
    text, _ = _render(steps, dedup=True)
    assert "(superseded by later read)" in text
    assert "VERSION ONE of foo" not in text     # earlier read's body dropped
    assert "VERSION TWO of foo" in text         # latest read kept verbatim (R1.3)


def test_r1_2_writes_never_collapsed():
    steps = [
        _step("WRITE_FILE", "a.py", result="ok"),
        _step("WRITE_FILE", "a.py", result="ok"),
        _step("EXPLAIN", "x"),
        _step("EXPLAIN", "y"),
    ]
    sup = _dedup_reads(steps, keep_verbatim=2, readonly_verbs=RO)
    assert sup == set()                      # writes are never superseded


# -- R2: a write invalidates prior reads of its target ----------------------- #

def test_r2_1_read_after_write_not_deduped():
    steps = [
        _step("READ_FILE", "foo.py", result="before edit"),   # 0
        _step("WRITE_FILE", "foo.py", result="ok"),           # 1 clears foo
        _step("READ_FILE", "foo.py", result="after edit"),    # 2
        _step("EXPLAIN", "x"),                                 # 3 verbatim
        _step("EXPLAIN", "y"),                                 # 4 verbatim
    ]
    sup = _dedup_reads(steps, keep_verbatim=2, readonly_verbs=RO)
    assert sup == set()       # the write reset tracking; both reads survive


def test_r2_2_unresolvable_write_clears_all():
    steps = [
        _step("READ_FILE", "foo.py", result="r1"),    # 0
        _step("RUN_TERMINAL", "make", result="ok"),   # 1 unresolvable → clear all
        _step("READ_FILE", "foo.py", result="r2"),    # 2
        _step("EXPLAIN", "x"),                         # 3 verbatim
        _step("EXPLAIN", "y"),                         # 4 verbatim
    ]
    sup = _dedup_reads(steps, keep_verbatim=2, readonly_verbs=RO)
    assert sup == set()       # terminal cleared the seen-set; no dedup across it


# -- R3: failures and recent steps exempt; order/no-mutation ----------------- #

def test_r3_1_failed_read_exempt():
    steps = [
        _step("READ_FILE", "foo.py", result="boom", success=False),  # 0 failed
        _step("GREP", "x"),                                          # 1
        _step("READ_FILE", "foo.py", result="ok now"),              # 2
        _step("EXPLAIN", "x"),                                       # 3 verbatim
        _step("EXPLAIN", "y"),                                       # 4 verbatim
    ]
    sup = _dedup_reads(steps, keep_verbatim=2, readonly_verbs=RO)
    assert 0 not in sup       # the failed read is recovery signal, never collapsed


def test_r3_2_verbatim_window_untouched():
    # Two reads of foo.py, but the second is INSIDE the verbatim window — the
    # older one is still superseded, the verbatim one stays full.
    steps = [
        _step("READ_FILE", "foo.py", result="old"),   # 0 older
        _step("EXPLAIN", "pad"),                       # 1 older
        _step("READ_FILE", "foo.py", result="new"),   # 2 verbatim (keep_verbatim=2 → cutoff=1... )
    ]
    # n=3, keep_verbatim=2 → cutoff=1; only index 0 is "older".
    sup = _dedup_reads(steps, keep_verbatim=2, readonly_verbs=RO)
    assert sup == {0}         # older read superseded by the verbatim-window read
    text, _ = _render(steps, dedup=True, keep_verbatim=2)
    assert "new" in text      # verbatim read never stubbed


def test_r3_3_does_not_mutate_steps():
    steps = [
        _step("READ_FILE", "foo.py", result="one"),
        _step("READ_FILE", "foo.py", result="two"),
        _step("EXPLAIN", "x"),
        _step("EXPLAIN", "y"),
    ]
    before = [(s.action, s.args, s.result, s.success) for s in steps]
    _dedup_reads(steps, keep_verbatim=2, readonly_verbs=RO)
    _render(steps, dedup=True)
    after = [(s.action, s.args, s.result, s.success) for s in steps]
    assert before == after


def test_r4_4_paths_intact_in_stub():
    p = "deep/nested/path/to/module.py"
    steps = [
        _step("READ_FILE", p, result="v1"),
        _step("READ_FILE", p, result="v2"),
        _step("EXPLAIN", "x"),
        _step("EXPLAIN", "y"),
    ]
    text, _ = _render(steps, dedup=True)
    assert p in text          # full path never truncated mid-string


# -- R4: independent flag, byte-identical when off, composes ----------------- #

def test_r4_1_disabled_is_byte_identical():
    steps = [
        _step("READ_FILE", "foo.py", result="one"),
        _step("READ_FILE", "foo.py", result="two"),
        _step("GREP", "x", result="hit"),
        _step("EXPLAIN", "done"),
    ]
    legacy, _ = render_trajectory(steps, style="replan", readonly_verbs=RO,
                                  enabled=False, dedup_reads=False)
    deduped, _ = render_trajectory(steps, style="replan", readonly_verbs=RO,
                                   enabled=False, dedup_reads=True)
    assert legacy != deduped                # dedup on actually changes output
    # and dedup-off reproduces the pre-feature legacy rendering exactly:
    manual = "\n".join(
        f"  {i}. [{'ok' if s.success else 'FAILED'}] {s.action} {s.args[:60]} → {s.result or ''}"
        for i, s in enumerate(steps, 1)
    )
    assert legacy == manual


def test_r4_2_composes_with_reduce():
    # A long prefix: dup reads + a failure; dedup on with reduction both on and off.
    steps = [
        _step("READ_FILE", "foo.py", result="old foo"),     # 0 superseded
        _step("READ_FILE", "bar.py", result="bar body"),    # 1
        _step("READ_FILE", "foo.py", result="new foo"),     # 2 last foo
        _step("GREP", "needle", result="boom", success=False),  # 3 failure
        _step("EXPLAIN", "a"),                               # 4
        _step("EXPLAIN", "b"),                               # 5 verbatim window
    ]
    for enabled in (False, True):
        text, stats = render_trajectory(
            steps, style="replan", keep_verbatim=1, readonly_verbs=RO,
            enabled=enabled, dedup_reads=True,
        )
        assert "(superseded by later read)" in text       # dedup ran
        assert "old foo" not in text                        # earlier foo dropped
        assert "boom" in text                               # failure preserved (R3.1)
        assert stats["chars_saved"] >= 0
