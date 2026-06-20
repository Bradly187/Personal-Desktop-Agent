"""WhisperStream error handling — a non-critical failure must never drop a
voice command or vanish silently.

`_transcribe` runs in a worker thread; the only thing catching exceptions around
it is `_loop`'s generic `except`, which logs "loop error" and moves on. So any
unguarded call between transcription and the FusionEngine dispatch (the acoustic
profiler write, the calibration callback) would, on failure, abort `_transcribe`
*before the command is delivered* — silently dropping the user's command. These
tests pin that those side-effects are isolated from the command path.
"""

from __future__ import annotations

from concurrent.futures import Future

import numpy as np
import pytest

from sensors.whisper_stream import WhisperStream, _log_future_exc
import sensors.whisper_stream as ws_mod


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

class _Seg:
    """Minimal faster-whisper segment stand-in for the hallucination filter."""
    def __init__(self, text: str, no_speech_prob: float = 0.1, avg_logprob: float = -0.2):
        self.text = text
        self.no_speech_prob = no_speech_prob
        self.avg_logprob = avg_logprob


class _Fusion:
    def __init__(self):
        self.cmds = []

    def on_voice(self, cmd):
        self.cmds.append(cmd)


@pytest.fixture
def quiet_approval_dir(tmp_path, monkeypatch):
    """No approval gate active during these tests."""
    d = tmp_path / "approval"
    d.mkdir()
    monkeypatch.setattr(ws_mod, "_APPROVAL_DIR", d)
    return d


def _ready_stream(monkeypatch, segments) -> WhisperStream:
    """A WhisperStream wired to bypass the model and emit `segments`."""
    ws = WhisperStream()
    ws._fusion = _Fusion()
    ws._model = object()  # bypass the "no model loaded" early return
    monkeypatch.setattr(ws, "_run_whisper", lambda audio, prompt: (list(segments), None))
    return ws


# ---------------------------------------------------------------------------
# Profiler failure must not drop the command
# ---------------------------------------------------------------------------

def test_profiler_failure_does_not_drop_command(quiet_approval_dir, monkeypatch):
    class _BadProfiler:
        def record(self, **kwargs):
            raise RuntimeError("profiler boom")

    ws = _ready_stream(monkeypatch, [_Seg("hey agent open vscode")])
    ws._profiler = _BadProfiler()

    ws._transcribe(np.zeros(16_000, dtype=np.float32))   # must not raise

    assert len(ws._fusion.cmds) == 1                     # command still delivered
    assert ws._fusion.cmds[0].text == "open vscode"


def test_working_profiler_still_records(quiet_approval_dir, monkeypatch):
    class _GoodProfiler:
        def __init__(self):
            self.calls = 0

        def record(self, **kwargs):
            self.calls += 1

    ws = _ready_stream(monkeypatch, [_Seg("hey agent open vscode")])
    ws._profiler = _GoodProfiler()

    ws._transcribe(np.zeros(16_000, dtype=np.float32))

    assert ws._profiler.calls == 1
    assert len(ws._fusion.cmds) == 1


# ---------------------------------------------------------------------------
# Calibration callback failure must be swallowed
# ---------------------------------------------------------------------------

def test_calibration_callback_failure_is_swallowed(quiet_approval_dir, monkeypatch):
    def _bad_cb(text, logprob, dur):
        raise RuntimeError("cb boom")

    ws = _ready_stream(monkeypatch, [_Seg("some calibration phrase")])
    ws._calibration_capture = _bad_cb

    ws._transcribe(np.zeros(16_000, dtype=np.float32))   # must not raise

    assert ws._calibration_capture is None               # one-shot consumed
    assert ws._fusion.cmds == []                          # not routed to fusion


def test_calibration_callback_success_receives_transcript(quiet_approval_dir, monkeypatch):
    seen = {}

    def _cb(text, logprob, dur):
        seen["text"] = text

    ws = _ready_stream(monkeypatch, [_Seg("calibrate me")])
    ws._calibration_capture = _cb

    ws._transcribe(np.zeros(16_000, dtype=np.float32))

    assert seen.get("text") == "calibrate me"
    assert ws._calibration_capture is None


# ---------------------------------------------------------------------------
# _log_future_exc — fire-and-forget exception surfacing
# ---------------------------------------------------------------------------

def test_log_future_exc_handles_all_states():
    failed = Future()
    failed.set_exception(RuntimeError("background boom"))
    _log_future_exc(failed)          # logs, does not raise

    ok = Future()
    ok.set_result("done")
    _log_future_exc(ok)              # no-op, does not raise

    cancelled = Future()
    cancelled.cancel()
    _log_future_exc(cancelled)       # cancelled — does not raise


def test_log_future_exc_logs_warning_on_failure(caplog):
    import logging
    failed = Future()
    failed.set_exception(ValueError("specific failure"))
    with caplog.at_level(logging.WARNING, logger="sensors.whisper_stream"):
        _log_future_exc(failed)
    assert any("specific failure" in r.message for r in caplog.records)
