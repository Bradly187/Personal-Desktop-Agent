"""RemoteWhisperService — faster-whisper transcription offloaded to the laptop node.

Runs on the laptop (RTX 4070). The desktop's WhisperStream owns audio capture and
the VAD *decision* (whether to transcribe at all); this service only runs the model
inference and returns raw segments. The desktop then applies its own (user-calibrated)
hallucination filter, so we deliberately do NOT filter here.

Protocol:
    POST /transcribe
      body: {
        "samples_b64": "<base64 float32 PCM>",
        "sample_rate": 16000,
        "initial_prompt": "Kiro, Slack, ...",   # optional
        "min_silence_ms": 600,                   # optional VAD param
        "speech_pad_ms": 100                     # optional VAD param
      }
      -> {
        "segments": [{"text","avg_logprob","no_speech_prob","start","end"}, ...],
        "info": {"language","language_probability","duration"}
      }

    GET /health -> {"status":"ok","model":...,"device":...,"warm":true}

Run on the laptop:
    .venv-laptop\\Scripts\\python.exe sensors\\remote_whisper_service.py --port 8888
"""

from __future__ import annotations

import argparse
import base64
import hmac
import logging
import os
import site
import sys
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("remote_whisper")


def _register_cuda_dll_dirs() -> None:
    """Add pip-installed CUDA math libs (cuBLAS/cuDNN/nvrtc) to the DLL search path.

    CTranslate2 on Windows loads these lazily at first GPU inference; without the
    bin dirs on the loader path the CUDA load fails with a missing-DLL error.
    """
    bases = []
    try:
        bases.extend(site.getsitepackages())
    except Exception:
        pass
    bases.append(os.path.join(sys.prefix, "Lib", "site-packages"))
    seen = set()
    for sp in bases:
        nv = os.path.join(sp, "nvidia")
        for sub in ("cublas", "cudnn", "cuda_nvrtc"):
            p = os.path.join(nv, sub, "bin")
            if os.path.isdir(p) and p not in seen:
                seen.add(p)
                try:
                    os.add_dll_directory(p)
                except Exception:
                    pass
                os.environ["PATH"] = p + os.pathsep + os.environ.get("PATH", "")
    if seen:
        log.info("CUDA DLL dirs registered: %d", len(seen))


_register_cuda_dll_dirs()

import numpy as np  # noqa: E402
from aiohttp import web  # noqa: E402
from faster_whisper import WhisperModel  # noqa: E402


class RemoteWhisperService:
    def __init__(self, model_size: str = "large-v3", device: str = "cuda",
                 compute_type: str = "float16") -> None:
        self._model_size = model_size
        self._device = device
        self._compute_type = compute_type
        self._model: WhisperModel | None = None
        self._warm = False

    def load(self) -> None:
        log.info("Loading %s on %s (%s) ...", self._model_size, self._device, self._compute_type)
        t0 = time.time()
        try:
            self._model = WhisperModel(self._model_size, device=self._device,
                                       compute_type=self._compute_type)
        except Exception as exc:
            log.warning("GPU load failed (%s) — falling back to CPU int8", exc)
            self._model = WhisperModel(self._model_size, device="cpu", compute_type="int8")
            self._device, self._compute_type = "cpu", "int8"
        log.info("Model loaded in %.1fs", time.time() - t0)
        # Warm up: first inference triggers ~50s cuDNN kernel autotuning. Do it now
        # so the first real request from the desktop isn't penalised.
        try:
            t1 = time.time()
            warm = (0.01 * np.random.RandomState(0).randn(16000)).astype(np.float32)
            segs, _ = self._model.transcribe(warm, language="en", vad_filter=False)
            list(segs)
            self._warm = True
            log.info("Warmup transcribe done in %.1fs", time.time() - t1)
        except Exception as exc:
            log.warning("Warmup failed (non-fatal): %s", exc)

    async def handle_transcribe(self, request: web.Request) -> web.Response:
        if self._model is None:
            return web.json_response({"error": "model not loaded"}, status=503)
        try:
            body = await request.json()
            audio = np.frombuffer(base64.b64decode(body["samples_b64"]), dtype=np.float32)
            initial_prompt = body.get("initial_prompt") or None
            min_silence_ms = int(body.get("min_silence_ms", 600))
            speech_pad_ms = int(body.get("speech_pad_ms", 100))
        except Exception as exc:
            return web.json_response({"error": f"bad request: {exc}"}, status=400)

        try:
            t0 = time.time()
            seg_iter, info = self._model.transcribe(
                audio,
                language="en",
                beam_size=5,
                vad_filter=True,
                vad_parameters={
                    "min_silence_duration_ms": min_silence_ms,
                    "speech_pad_ms": speech_pad_ms,
                },
                initial_prompt=initial_prompt,
            )
            segments = [
                {
                    "text": s.text,
                    "avg_logprob": float(s.avg_logprob),
                    "no_speech_prob": float(s.no_speech_prob),
                    "start": float(s.start),
                    "end": float(s.end),
                }
                for s in seg_iter
            ]
            dt = (time.time() - t0) * 1000
            log.info("transcribe: %d segs, %.0f ms, %d samples", len(segments), dt, audio.size)
            return web.json_response({
                "segments": segments,
                "info": {
                    "language": info.language,
                    "language_probability": float(getattr(info, "language_probability", 0.0) or 0.0),
                    "duration": float(getattr(info, "duration", audio.size / 16000.0)),
                },
            })
        except Exception as exc:
            log.error("transcription error: %s", exc)
            return web.json_response({"error": str(exc)}, status=500)

    async def handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({
            "status": "ok" if self._model is not None else "loading",
            "model": self._model_size,
            "device": self._device,
            "warm": self._warm,
        })

    async def handle_devices(self, request: web.Request) -> web.Response:
        """Return all sounddevice audio input devices on this machine."""
        try:
            import sounddevice as sd
            devices = sd.query_devices()
            default_input = sd.default.device[0] if hasattr(sd.default, "device") else None
            inputs = [
                {
                    "index": i,
                    "name": d["name"],
                    "channels": d["max_input_channels"],
                    "default_samplerate": d["default_samplerate"],
                    "is_default": (i == default_input),
                }
                for i, d in enumerate(devices)
                if d["max_input_channels"] > 0
            ]
            return web.json_response({"inputs": inputs, "default_index": default_input})
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)


def _make_auth_middleware(token: str):
    """Require a shared bearer token on the data-returning /transcribe and /devices routes (C2).

    /health stays open so ClusterHealthMonitor can probe liveness without the token.
    Constant-time comparison; any mismatch → 401.
    """
    @web.middleware
    async def _auth(request: web.Request, handler):
        if request.path in ("/transcribe", "/devices"):
            supplied = request.headers.get("Authorization", "")
            if not hmac.compare_digest(supplied, f"Bearer {token}"):
                return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)
    return _auth


def main() -> None:
    ap = argparse.ArgumentParser(description="Remote faster-whisper service (laptop node)")
    ap.add_argument("--host", default="127.0.0.1",
                    help="Bind address (default 127.0.0.1 for loopback; set to 0.0.0.0 only with --token)")
    ap.add_argument("--port", type=int, default=8888)
    ap.add_argument("--model", default="large-v3")
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--compute-type", default="float16")
    ap.add_argument("--token", default=None,
                    help="Shared bearer token; falls back to the WHISPER_TOKEN env var")
    args = ap.parse_args()

    # C2: refuse to run unauthenticated on non-loopback interfaces.
    token = os.environ.get("WHISPER_TOKEN") or args.token
    if not token:
        if args.host != "127.0.0.1":
            log.error("No whisper token set (WHISPER_TOKEN env or --token) and binding to "
                      "non-loopback %s. Refusing to start an unauthenticated public service.", args.host)
            sys.exit(2)
        log.warning("Running without token on loopback (127.0.0.1) — set WHISPER_TOKEN for "
                    "multi-machine deployments")
        token = ""  # empty token => middleware always denies (fail-safe)

    svc = RemoteWhisperService(args.model, args.device, args.compute_type)
    svc.load()

    app = web.Application(client_max_size=64 * 1024 * 1024, middlewares=[_make_auth_middleware(token)])
    app.router.add_post("/transcribe", svc.handle_transcribe)
    app.router.add_get("/health", svc.handle_health)
    app.router.add_get("/devices", svc.handle_devices)
    log.info("RemoteWhisperService listening on %s:%d (auth: %s)",
             args.host, args.port, "token-required" if token else "loopback-only")
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
