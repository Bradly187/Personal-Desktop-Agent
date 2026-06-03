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
import logging
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np

log = logging.getLogger(__name__)


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
    def __init__(self, url: str, timeout: float = 8.0, retries: int = 1) -> None:
        # url e.g. "http://192.168.18.12:8888"
        # timeout 8s (was 30s): LAN inference is ~1-3s, so 30s only delayed the
        # fall back to local on a hung laptop. retries=1 absorbs a single
        # transient blip (jittered backoff) before giving up.
        self._endpoint = url.rstrip("/") + "/transcribe"
        self._timeout = timeout
        self._retries = max(0, retries)

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
        last_exc: Optional[BaseException] = None
        data = None
        for attempt in range(self._retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    data = json.loads(resp.read())
                break
            except (urllib.error.URLError, OSError) as exc:
                last_exc = exc
                if attempt < self._retries:
                    time.sleep(0.1 + random.random() * 0.2)  # 100–300ms jitter
                    log.debug("RemoteWhisperClient: retry %d after %s", attempt + 1, exc)
                    continue
                raise
        if data is None:  # defensive — loop either set data or raised
            raise last_exc or RuntimeError("remote whisper: no response")

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
