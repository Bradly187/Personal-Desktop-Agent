"""Tests for inference/trajectory.py — DevAgent trajectory reduction.

Spec: specs/trajectory-reduction/requirements.md. Each test cites the acceptance
criterion it covers.
"""

from __future__ import annotations

import copy


from inference.dev_agent import AgentStep, DevAgent
from inference.trajectory import render_trajectory, reduction_enabled

RO = DevAgent._PARALLEL_VERBS


def _step(action, args="", result="", success=True):
    return AgentStep(action=action, args=args, result=result, success=success)


# --- Legacy renderers (the exact inline loops render_trajectory replaces) ------

def _legacy_replan_block(steps):
    out = []
    for i, s in enumerate(steps, 1):
        status = "ok" if s.success else "FAILED"
        snippet = (s.result or "")[:300]
        out.append(f"  {i}. [{status}] {s.action} {s.args[:60]} → {snippet}")
    return "\n".join(out)


def _legacy_reflect_block(steps):
    out = []
    for i, s in enumerate(steps, 1):
        status = "✓" if s.success else "✗"
        snippet = (s.result or "")
        snippet = snippet[:200] if s.success else snippet[:600]
        out.append(f"  {i}. {status} {s.action} {s.args[:60]}\n     → {snippet}")
    return "\n".join(out)


# --- R2.4: disabled == legacy, byte-identical ---------------------------------

def test_r2_4_disabled_replan_is_byte_identical_to_legacy():
    steps = [
        _step("READ_FILE", "a.py", "contents of a"),
        _step("GREP", "TODO", "3 matches"),
        _step("RUN_TERMINAL", "pytest", "FAILED 1 test", success=False),
        _step("WRITE_FILE", "b.py", "ok"),
        _step("EXPLAIN", "", "done"),
    ]
    text, stats = render_trajectory(steps, style="replan", enabled=False)
    assert text == _legacy_replan_block(steps)
    assert stats["chars_saved"] == 0


def test_r2_4_disabled_reflect_is_byte_identical_to_legacy():
    steps = [
        _step("READ_FILE", "a.py", "x" * 500),                 # success → [:200]
        _step("RUN_TERMINAL", "pytest", "y" * 1000, success=False),  # fail → [:600]
    ]
    text, _ = render_trajectory(
        steps, style="reflect", success_chars=200, failure_chars=600, enabled=False
    )
    assert text == _legacy_reflect_block(steps)


# --- R3.2: pass-through when <= keep_verbatim ---------------------------------

def test_r3_2_passthrough_when_within_window():
    steps = [_step("READ_FILE", "a.py", "x"), _step("EXPLAIN", "", "y")]
    text, stats = render_trajectory(steps, keep_verbatim=3, enabled=True)
    assert text == _legacy_replan_block(steps)
    assert stats["chars_saved"] == 0


# --- R1.2: most recent keep_verbatim steps verbatim ---------------------------

def test_r1_2_keeps_recent_verbatim():
    steps = [_step("READ_FILE", f"f{i}.py", "RESULT" + str(i)) for i in range(6)]
    text, _ = render_trajectory(steps, keep_verbatim=3, readonly_verbs=RO, enabled=True)
    # The last three steps (indices 4,5,6) appear in full replan form.
    for i in (4, 5, 6):
        s = steps[i - 1]
        assert f"  {i}. [ok] {s.action} {s.args} → {s.result}" in text


# --- R1.3: older successes abstracted to a single line ------------------------

def test_r1_3_abstracts_old_success_to_one_line():
    steps = [
        _step("RUN_TERMINAL", "build", "huge\noutput\n" + "z" * 400),  # old success
        _step("EXPLAIN", "", "note"),                                  # breaks RO run
        _step("WRITE_FILE", "x", "ok"),
        _step("WRITE_FILE", "y", "ok"),
        _step("WRITE_FILE", "zz", "ok"),
    ]
    text, stats = render_trajectory(steps, keep_verbatim=3, readonly_verbs=RO, enabled=True)
    line0 = text.splitlines()[0]
    assert line0.startswith("  1. [ok] RUN_TERMINAL build")
    assert "\n" not in line0 and len(line0) < 130   # outcome capped (≤80) + no newlines
    assert stats["chars_saved"] > 0


# --- R1.4: failure signal preserved regardless of age ------------------------

def test_r1_4_failure_signal_preserved_when_old():
    err = "Traceback: NameError flooble is not defined"
    steps = [
        _step("RUN_TERMINAL", "pytest", err, success=False),   # OLD failure
        _step("READ_FILE", "a", "x"),
        _step("READ_FILE", "b", "y"),
        _step("READ_FILE", "c", "z"),
    ]
    text, _ = render_trajectory(steps, keep_verbatim=3, readonly_verbs=RO, enabled=True)
    assert "FAILED" in text and err in text   # full error survives abstraction


# --- R1.5: consecutive older read-only successes collapse --------------------

def test_r1_5_collapses_readonly_runs_keeping_last_verbatim():
    steps = [
        _step("READ_FILE", "alpha.py", "AAA"),
        _step("READ_FILE", "beta.py", "BBB"),
        _step("GREP", "needle", "NEEDLE-DIAGNOSTIC"),   # most-recent read of the run
        _step("WRITE_FILE", "out.py", "ok"),
        _step("EXPLAIN", "", "done"),
        _step("RUN_TERMINAL", "pytest", "passed"),
    ]
    text, _ = render_trajectory(steps, keep_verbatim=3, readonly_verbs=RO, enabled=True)
    lines = text.splitlines()
    # The HEAD of the run (alpha, beta) collapses to one summary line ...
    assert lines[0].startswith("  1–2. [ok] 2 read-only steps:")
    assert "alpha.py" in lines[0] and "beta.py" in lines[0]
    # ... but the LAST read (needle) is kept verbatim with its diagnostic result.
    assert lines[1].startswith("  3. [ok] GREP needle")
    assert "NEEDLE-DIAGNOSTIC" in lines[1]


# --- R1.6: file paths never truncated mid-string in abstracted lines ---------

def test_r1_6_paths_not_truncated_in_abstract():
    long_path = "src/very/deeply/nested/package/module/submodule/handler_impl.py"  # >60 chars
    assert len(long_path) > 60
    steps = [
        _step("READ_FILE", long_path, "loaded"),   # lone RO (next breaks the run)
        _step("EXPLAIN", "", "note"),
        _step("WRITE_FILE", "x", "ok"),
        _step("WRITE_FILE", "y", "ok"),
        _step("WRITE_FILE", "z", "ok"),
    ]
    text, _ = render_trajectory(steps, keep_verbatim=3, readonly_verbs=RO, enabled=True)
    assert long_path in text   # full path intact, not args[:60]


# --- R3.3: never mutates input steps -----------------------------------------

def test_r3_3_does_not_mutate_steps():
    steps = [_step("READ_FILE", f"f{i}", "r" * 500) for i in range(6)]
    before = copy.deepcopy(steps)
    render_trajectory(steps, keep_verbatim=2, readonly_verbs=RO, enabled=True)
    for a, b in zip(steps, before):
        assert (a.action, a.args, a.result, a.success) == (b.action, b.args, b.result, b.success)


# --- R1.1: deterministic (identical input → identical output) ----------------

def test_r1_1_deterministic():
    steps = [_step("READ_FILE", f"f{i}", f"res{i}") for i in range(8)]
    t1, s1 = render_trajectory(steps, keep_verbatim=3, readonly_verbs=RO, enabled=True)
    t2, s2 = render_trajectory(steps, keep_verbatim=3, readonly_verbs=RO, enabled=True)
    assert t1 == t2 and s1 == s2


# --- R2.4 flag default: reduction OFF unless explicitly enabled ---------------

def test_flag_default_on(monkeypatch):
    monkeypatch.delenv("DA_TRAJECTORY_REDUCE", raising=False)
    assert reduction_enabled() is True
    monkeypatch.setenv("DA_TRAJECTORY_REDUCE", "0")
    assert reduction_enabled() is False
    monkeypatch.setenv("DA_TRAJECTORY_REDUCE", "off")
    assert reduction_enabled() is False
