"""WhisperStream buffering — keep the event loop off the O(n²) concat path.

`_maybe_transcribe` runs every poll tick (~150 ms). It used to `np.concatenate`
the entire growing audio buffer just to measure duration and check trailing
silence, discarding it whenever the utterance wasn't finished — O(n) per tick,
O(n²) over a long (e.g. lecture-mode, 30 s) utterance, all on the event loop.

The buffer is now measured by summing chunk lengths (O(#chunks)) and the
trailing-silence test concatenates only the tail window — the full concat is
deferred to the single moment a segment is actually transcribed. These tests pin
that the cheap path produces the SAME decisions as the old full-buffer logic.
"""

from __future__ import annotations

import numpy as np
import pytest

from sensors.whisper_stream import WhisperStream, _has_trailing_silence
import sensors.whisper_stream as ws_mod


def _split(audio: np.ndarray, n: int) -> list:
    """Chunk a signal into ~n pieces, mimicking ~100 ms audio_stream chunks."""
    return [c for c in np.array_split(audio, n) if len(c)]


@pytest.fixture
def quiet_approval_dir(tmp_path, monkeypatch):
    """Isolate the approval-gate stat so a real ~/.claude/approval/pending on the
    dev box can't flush the buffer mid-test."""
    d = tmp_path / "approval"
    d.mkdir()
    monkeypatch.setattr(ws_mod, "_APPROVAL_DIR", d)
    return d


# ---------------------------------------------------------------------------
# _trailing_silence_from_chunks — parity with the old full-buffer helper
# ---------------------------------------------------------------------------

def test_trailing_silence_true_matches_full_buffer():
    ws = WhisperStream(silence_duration_s=0.3, silence_threshold=0.01)
    sr = ws.SAMPLE_RATE
    full = np.concatenate([
        np.full(int(0.5 * sr), 0.2, dtype=np.float32),   # speech
        np.zeros(int(0.4 * sr), dtype=np.float32),       # trailing silence
    ])
    ws._buffer_chunks = _split(full, 9)
    expected = _has_trailing_silence(full, sr, ws._silence_s, ws._silence_thresh)
    assert expected is True
    assert ws._trailing_silence_from_chunks() == expected


def test_trailing_silence_false_while_speaking_matches_full_buffer():
    ws = WhisperStream(silence_duration_s=0.3, silence_threshold=0.01)
    sr = ws.SAMPLE_RATE
    full = np.full(int(0.8 * sr), 0.2, dtype=np.float32)  # loud throughout
    ws._buffer_chunks = _split(full, 8)
    expected = _has_trailing_silence(full, sr, ws._silence_s, ws._silence_thresh)
    assert expected is False
    assert ws._trailing_silence_from_chunks() == expected


def test_trailing_silence_false_when_buffer_shorter_than_window():
    ws = WhisperStream(silence_duration_s=0.6)
    sr = ws.SAMPLE_RATE
    ws._buffer_chunks = [np.zeros(int(0.2 * sr), dtype=np.float32)]  # < 0.6 s
    assert ws._trailing_silence_from_chunks() is False


def test_trailing_silence_only_concats_tail_not_whole_buffer(monkeypatch):
    """The cheap path must not concatenate the entire buffer — guard against a
    regression that reintroduces the O(n²) full concat in the poll path."""
    ws = WhisperStream(silence_duration_s=0.3, silence_threshold=0.01)
    sr = ws.SAMPLE_RATE
    full = np.concatenate([
        np.full(int(2.0 * sr), 0.2, dtype=np.float32),
        np.zeros(int(0.4 * sr), dtype=np.float32),
    ])
    ws._buffer_chunks = _split(full, 24)

    sizes = []
    real_concat = np.concatenate
    monkeypatch.setattr(ws_mod.np, "concatenate",
                        lambda seq, *a, **k: sizes.append(sum(len(x) for x in seq)) or real_concat(seq, *a, **k))
    ws._trailing_silence_from_chunks()
    # Only the ~0.3 s tail window was concatenated, not the full ~2.4 s buffer.
    assert sizes and max(sizes) < int(1.0 * sr)


# ---------------------------------------------------------------------------
# _maybe_transcribe — trigger decisions unchanged
# ---------------------------------------------------------------------------

async def test_maybe_transcribe_skips_short_audio(quiet_approval_dir, monkeypatch):
    ws = WhisperStream(min_speech_s=0.5)
    sr = ws.SAMPLE_RATE
    called = []
    monkeypatch.setattr(ws, "_transcribe", lambda audio: called.append(len(audio)))
    ws._buffer_chunks = _split(np.full(int(0.2 * sr), 0.2, dtype=np.float32), 2)
    await ws._maybe_transcribe()
    assert called == []           # too short — not transcribed
    assert ws._buffer_chunks      # buffer retained for more audio


async def test_maybe_transcribe_fires_on_trailing_silence(quiet_approval_dir, monkeypatch):
    ws = WhisperStream(min_speech_s=0.3, silence_duration_s=0.3, silence_threshold=0.01)
    sr = ws.SAMPLE_RATE
    called = []
    monkeypatch.setattr(ws, "_transcribe", lambda audio: called.append(len(audio)))
    full = np.concatenate([
        np.full(int(0.5 * sr), 0.2, dtype=np.float32),
        np.zeros(int(0.4 * sr), dtype=np.float32),
    ])
    ws._buffer_chunks = _split(full, 9)
    await ws._maybe_transcribe()
    assert len(called) == 1            # transcribed exactly once
    assert called[0] == len(full)      # the full buffer was handed off
    assert ws._buffer_chunks == []     # buffer claimed


async def test_maybe_transcribe_force_at_max_buffer(quiet_approval_dir, monkeypatch):
    """A buffer at max length transcribes even with no trailing silence."""
    ws = WhisperStream(min_speech_s=0.3, max_buffer_s=1.0, silence_threshold=0.01)
    sr = ws.SAMPLE_RATE
    called = []
    monkeypatch.setattr(ws, "_transcribe", lambda audio: called.append(len(audio)))
    full = np.full(int(1.2 * sr), 0.2, dtype=np.float32)  # loud, over max
    ws._buffer_chunks = _split(full, 12)
    await ws._maybe_transcribe()
    assert len(called) == 1
    assert ws._buffer_chunks == []
