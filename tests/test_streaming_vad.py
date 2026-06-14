"""Streaming neural VAD — the end-of-utterance segmentation gate.

`WhisperStream` now decides when an utterance ends with a webrtcvad neural gate
(`_StreamingVAD` + `_trailing_silence_neural`), falling back to the original RMS
energy gate when the VAD is disabled, the library is missing, the trailing-
silence window isn't full yet, or the probe errors. These tests pin:

  - `_StreamingVAD` voiced-fraction hysteresis (the `end_ratio` decision),
  - the neural verdict actually DRIVES `_maybe_transcribe` (overrides RMS),
  - every fallback path (disabled / missing lib / short window / exception)
    degrades cleanly to the RMS gate without raising.
"""

from __future__ import annotations

import numpy as np
import pytest

from sensors.whisper_stream import WhisperStream, _StreamingVAD
import sensors.whisper_stream as ws_mod


def _split(audio: np.ndarray, n: int) -> list:
    return [c for c in np.array_split(audio, n) if len(c)]


@pytest.fixture
def quiet_approval_dir(tmp_path, monkeypatch):
    """Isolate the approval-gate stat so a real ~/.claude/approval/pending can't
    flush the buffer mid-test (mirrors test_whisper_buffer.py)."""
    d = tmp_path / "approval"
    d.mkdir()
    monkeypatch.setattr(ws_mod, "_APPROVAL_DIR", d)
    return d


class _FakeVad:
    """Deterministic stand-in for webrtcvad.Vad — `decide(frame_bytes)->bool`."""

    def __init__(self, decide):
        self._decide = decide

    def is_speech(self, frame_bytes, sr):
        return self._decide(frame_bytes)


class _FakeNeural:
    """Stand-in for _StreamingVAD whose verdict (or exception) is scripted."""

    def __init__(self, verdict=None, raises=False):
        self._verdict = verdict
        self._raises = raises
        self.calls = 0

    def trailing_silence(self, tail_int16, silence_s, end_ratio):
        self.calls += 1
        if self._raises:
            raise RuntimeError("boom")
        return self._verdict


# ---------------------------------------------------------------------------
# _StreamingVAD — voiced-fraction hysteresis
# ---------------------------------------------------------------------------

def _frames_window(n_frames: int, voiced_idx: set, frame_samples: int) -> np.ndarray:
    """Build an int16 window of n_frames where voiced frames are nonzero."""
    win = np.zeros(n_frames * frame_samples, dtype=np.int16)
    for i in voiced_idx:
        win[i * frame_samples:(i + 1) * frame_samples] = 5000
    return win


def test_streaming_vad_low_voiced_fraction_is_silent():
    pytest.importorskip("webrtcvad")
    v = _StreamingVAD(2, 16000)
    v._vad = _FakeVad(lambda b: any(b))     # nonzero frame == "speech"
    fs = v._frame_samples                    # 480 @ 16k/30ms
    # 1 voiced of 20 frames = 0.05 ≤ end_ratio 0.10 → silent
    win = _frames_window(20, {0}, fs)
    assert v.trailing_silence(win, silence_s=20 * 30 / 1000, end_ratio=0.10) is True


def test_streaming_vad_high_voiced_fraction_is_not_silent():
    pytest.importorskip("webrtcvad")
    v = _StreamingVAD(2, 16000)
    v._vad = _FakeVad(lambda b: any(b))
    fs = v._frame_samples
    # 10 voiced of 20 frames = 0.50 > 0.10 → not silent
    win = _frames_window(20, set(range(10)), fs)
    assert v.trailing_silence(win, silence_s=20 * 30 / 1000, end_ratio=0.10) is False


def test_streaming_vad_short_window_not_silent():
    pytest.importorskip("webrtcvad")
    v = _StreamingVAD(2, 16000)
    # window shorter than silence_s → cannot conclude silence
    assert v.trailing_silence(np.zeros(100, dtype=np.int16), 0.6, 0.10) is False


def test_streaming_vad_pure_silence_is_silent():
    pytest.importorskip("webrtcvad")
    v = _StreamingVAD(2, 16000)
    sil = np.zeros(int(16000 * 0.6), dtype=np.int16)
    assert v.trailing_silence(sil, 0.6, 0.10) is True


# ---------------------------------------------------------------------------
# Gate selection / construction
# ---------------------------------------------------------------------------

def test_neural_gate_active_by_default_when_lib_present():
    pytest.importorskip("webrtcvad")
    ws = WhisperStream()
    assert ws._use_neural_vad is True
    assert ws._neural_vad is not None
    assert ws._vad_gate_desc().startswith("neural")
    assert ws.get_status()["vad_gate"].startswith("neural")


def test_use_neural_vad_false_falls_back_to_rms():
    ws = WhisperStream(use_neural_vad=False)
    assert ws._use_neural_vad is False
    assert ws._neural_vad is None
    assert ws._vad_gate_desc() == "rms"
    # the neural probe is a no-op when disabled
    assert ws._trailing_silence_neural() is None


def test_da_neural_vad_env_kill_switch(monkeypatch):
    pytest.importorskip("webrtcvad")
    monkeypatch.setenv("DA_NEURAL_VAD", "0")
    ws = WhisperStream()
    assert ws._use_neural_vad is False
    assert ws._vad_gate_desc() == "rms"


def test_missing_lib_falls_back_to_rms(monkeypatch):
    monkeypatch.setattr(ws_mod, "_WEBRTCVAD_AVAILABLE", False)
    ws = WhisperStream()
    assert ws._use_neural_vad is False
    assert ws._neural_vad is None
    assert ws._vad_gate_desc() == "rms"


# ---------------------------------------------------------------------------
# _trailing_silence_neural — fallback contract (returns None on degrade)
# ---------------------------------------------------------------------------

def test_neural_probe_none_when_window_not_full():
    pytest.importorskip("webrtcvad")
    ws = WhisperStream(silence_duration_s=0.6)
    sr = ws.SAMPLE_RATE
    ws._buffer_chunks = [np.zeros(int(0.2 * sr), dtype=np.float32)]  # < 0.6 s
    assert ws._trailing_silence_neural() is None


def test_neural_probe_none_on_exception():
    pytest.importorskip("webrtcvad")
    ws = WhisperStream(silence_duration_s=0.3)
    sr = ws.SAMPLE_RATE
    ws._buffer_chunks = _split(np.zeros(int(0.6 * sr), dtype=np.float32), 6)
    ws._neural_vad = _FakeNeural(raises=True)
    # exception inside the probe is swallowed → None (caller uses RMS)
    assert ws._trailing_silence_neural() is None


def test_neural_probe_silence_returns_true():
    pytest.importorskip("webrtcvad")
    ws = WhisperStream(silence_duration_s=0.3)
    sr = ws.SAMPLE_RATE
    ws._buffer_chunks = _split(np.zeros(int(0.6 * sr), dtype=np.float32), 6)
    assert ws._trailing_silence_neural() is True


# ---------------------------------------------------------------------------
# _maybe_transcribe — the neural verdict drives the decision
# ---------------------------------------------------------------------------

async def test_neural_verdict_overrides_rms_to_fire(quiet_approval_dir, monkeypatch):
    """Trailing audio is LOUD (RMS would say 'not silent'), but the neural gate
    says silent → the utterance must transcribe. Proves neural drives the gate."""
    ws = WhisperStream(min_speech_s=0.3, silence_duration_s=0.3, silence_threshold=0.01)
    sr = ws.SAMPLE_RATE
    ws._neural_vad = _FakeNeural(verdict=True)   # neural: silent
    called = []
    monkeypatch.setattr(ws, "_transcribe", lambda audio: called.append(len(audio)))
    full = np.full(int(0.8 * sr), 0.2, dtype=np.float32)  # loud throughout
    ws._buffer_chunks = _split(full, 8)
    # sanity: RMS alone would NOT fire on this loud buffer
    assert ws._trailing_silence_from_chunks() is False
    await ws._maybe_transcribe()
    assert len(called) == 1
    assert ws._neural_vad.calls == 1
    assert ws._buffer_chunks == []


async def test_neural_verdict_overrides_rms_to_hold(quiet_approval_dir, monkeypatch):
    """Trailing audio is SILENT (RMS would say 'silent'), but the neural gate
    says not-silent → the utterance must NOT transcribe yet."""
    ws = WhisperStream(min_speech_s=0.3, silence_duration_s=0.3, silence_threshold=0.01)
    sr = ws.SAMPLE_RATE
    ws._neural_vad = _FakeNeural(verdict=False)  # neural: still speaking
    called = []
    monkeypatch.setattr(ws, "_transcribe", lambda audio: called.append(len(audio)))
    full = np.concatenate([
        np.full(int(0.5 * sr), 0.2, dtype=np.float32),
        np.zeros(int(0.4 * sr), dtype=np.float32),    # trailing silence
    ])
    ws._buffer_chunks = _split(full, 9)
    # sanity: RMS alone WOULD fire here
    assert ws._trailing_silence_from_chunks() is True
    await ws._maybe_transcribe()
    assert called == []            # neural held the gate closed
    assert ws._buffer_chunks       # buffer retained


async def test_neural_exception_falls_back_to_rms_and_fires(quiet_approval_dir, monkeypatch):
    """A throwing neural probe must not break segmentation — RMS takes over."""
    ws = WhisperStream(min_speech_s=0.3, silence_duration_s=0.3, silence_threshold=0.01)
    sr = ws.SAMPLE_RATE
    ws._neural_vad = _FakeNeural(raises=True)
    called = []
    monkeypatch.setattr(ws, "_transcribe", lambda audio: called.append(len(audio)))
    full = np.concatenate([
        np.full(int(0.5 * sr), 0.2, dtype=np.float32),
        np.zeros(int(0.4 * sr), dtype=np.float32),
    ])
    ws._buffer_chunks = _split(full, 9)
    await ws._maybe_transcribe()   # neural raises → None → RMS says silent → fire
    assert len(called) == 1
    assert ws._buffer_chunks == []


async def test_force_at_max_buffer_skips_vad(quiet_approval_dir, monkeypatch):
    """At max_buffer_s the VAD is bypassed entirely (force path)."""
    ws = WhisperStream(min_speech_s=0.3, max_buffer_s=1.0, silence_threshold=0.01)
    sr = ws.SAMPLE_RATE
    ws._neural_vad = _FakeNeural(verdict=False)  # would hold, but force overrides
    called = []
    monkeypatch.setattr(ws, "_transcribe", lambda audio: called.append(len(audio)))
    full = np.full(int(1.2 * sr), 0.2, dtype=np.float32)
    ws._buffer_chunks = _split(full, 12)
    await ws._maybe_transcribe()
    assert len(called) == 1
    assert ws._neural_vad.calls == 0   # VAD never consulted on the force path
    assert ws._buffer_chunks == []
