"""Smoke test: RemoteWhisperClient -> RemoteWhisperService (must be running on :8888)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
from sensors.remote_whisper_client import RemoteWhisperClient

c = RemoteWhisperClient("http://127.0.0.1:8888", timeout=30)
audio = (0.05 * np.random.RandomState(1).randn(16000 * 2)).astype(np.float32)
segs, info = c.transcribe(audio, initial_prompt="Kiro, Slack")
print("segments:", len(segs))
for s in segs[:3]:
    print("  ", type(s).__name__, "text=%r logprob=%.2f no_speech=%.2f" % (s.text[:40], s.avg_logprob, s.no_speech_prob))
print("info: lang=%s dur=%.2f" % (info.language, info.duration))
# Shape assertions: segments must quack like faster-whisper segments
for s in segs:
    assert hasattr(s, "text") and hasattr(s, "avg_logprob") and hasattr(s, "no_speech_prob")
assert hasattr(info, "language") and hasattr(info, "duration")
print("REMOTE_WHISPER_SMOKE_OK")
