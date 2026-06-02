"""RemoteWhisperClient — desktop-side client for the laptop Whisper service.

Used by WhisperStream._run_whisper() when a remote URL is configured and the
laptop Whisper service is healthy. Returns (segments, info) shaped exactly like
faster-whisper's local output (list of segment objects + an info object) so the
existing WhisperStream filtering code is unchanged.

Synchronous (urllib): WhisperStream._transcribe runs in a worker thread, so a
blocking HTTP call here does not touch the event loop.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np


@dataclass
class _Seg:
    text: str
    avg_logprob: float
    no_speech_prob: float
    start: float
    end: float


@dataclass
class _Info:
    language: str
    language_probability: float
    duration: float


class RemoteWhisperClient:
    def __init__(self, url: str, timeout: float = 30.0) -> None:
        # url e.g. "http://192.168.18.12:8888"
        self._endpoint = url.rstrip("/") + "/transcribe"
        self._timeout = timeout

    def transcribe(
        self,
        audio: "np.ndarray",
        initial_prompt: Optional[str] = None,
        min_silence_ms: int = 600,
        speech_pad_ms: int = 100,
    ) -> Tuple[List[_Seg], _Info]:
        """POST audio to the laptop, return (segments, info).

        Raises urllib.error.URLError / OSError on transport failure so the caller
        can fall back to local inference.
        """
        samples = np.ascontiguousarray(audio, dtype=np.float32)
        payload = {
            "samples_b64": base64.b64encode(samples.tobytes()).decode("ascii"),
            "sample_rate": 16000,
            "initial_prompt": initial_prompt,
            "min_silence_ms": min_silence_ms,
            "speech_pad_ms": speech_pad_ms,
        }
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            self._endpoint, data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            data = json.loads(resp.read())

        if "error" in data:
            raise RuntimeError(f"remote whisper error: {data['error']}")

        segs = [
            _Seg(
                text=s.get("text", ""),
                avg_logprob=float(s.get("avg_logprob", 0.0)),
                no_speech_prob=float(s.get("no_speech_prob", 0.0)),
                start=float(s.get("start", 0.0)),
                end=float(s.get("end", 0.0)),
            )
            for s in data.get("segments", [])
        ]
        info_d = data.get("info", {})
        info = _Info(
            language=info_d.get("language", "en"),
            language_probability=float(info_d.get("language_probability", 0.0)),
            duration=float(info_d.get("duration", len(samples) / 16000.0)),
        )
        return segs, info
