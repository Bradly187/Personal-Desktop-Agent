# TTS Reference

## Default runtime backend: Kokoro (local ONNX)

Since 2026-06-23 the agent runtime defaults to **Kokoro** (`tts_backend: "kokoro"` in `approval_config.json`, voice `af_bella`, speed 1.0). It's a fully-offline ONNX TTS — no Node sidecar, no AWS, no network. It runs on **CPU** in the current env (the installed `onnxruntime` is the CPU build); `tts/kokoro_tts.py` `_select_onnx_provider()` auto-selects `CUDAExecutionProvider` once `onnxruntime-gpu` + the CUDA stack are installed. Requires the `espeak-ng` binary on PATH and the model weights (`kokoro-v1.0.onnx`, `voices.bin`) in `tts/` (download via `tts/download_kokoro.py`; gitignored).

Switch backends with `tts_backend` (`kokoro` | `polly` | `sapi`) — takes effect immediately, no restart. An unknown backend value falls back to Polly with a logged warning.

> **Note:** `approval_hook.py`'s Claude Code voice-approval consent prompts speak via Amazon Polly directly (hardcoded `_polly_speak`), independent of `tts_backend`. Only the agent runtime honors the backend switch.

## Polly voice: Danielle (en-US, Generative engine, 24 kHz)

Used by the `polly` backend and the approval-hook/sidecar-fallback paths. Danielle is the only en-US female voice that supports both the Generative engine (bidirectional streaming sidecar — lowest latency, most natural prosody) and the Long-form engine (batch path — best for multi-paragraph responses).

### Changing the voice

One line in `approval_config.json`:
```json
"voice_id": "Danielle"
```
Takes effect immediately — no restart required. The sidecar reads the voice from each POST request.

### Available voices (en-US, verified 2026-05-15)

| Voice | Gender | Generative | Long-form | Notes |
|-------|--------|-----------|-----------|-------|
| **Danielle** | Female | ✅ | ✅ | Current — most capable, both engines |
| Ruth | Female | ✅ | ✅ | Previous default |
| Joanna | Female | ✅ | — | Professional; Alexa-adjacent |
| Salli | Female | ✅ | — | Upbeat, clear |
| Matthew | Male | ✅ | — | |
| Stephen | Male | ✅ | — | |
| Gregory | Male | — | ✅ | Long-form only (was original default) |

### TTS paths and engines

| Path | Engine | Voice source | When |
|------|--------|-------------|------|
| `tts/kokoro_tts.py` (via `polly_stream.get_client()`) | Local ONNX (CPU; GPU when available) | `kokoro_voice`/`kokoro_speed` in `approval_config.json` | **Default** — when `tts_backend == "kokoro"` |
| `tts/polly_stream.py` → `tts_service/server.js` | Generative 24kHz | `approval_config.json` → POST body | When `tts_backend == "polly"`; CLARIFY questions, DevAgent EXPLAIN |
| `tts/sapi_tts.py` (via `polly_stream.get_client()`) | Windows SAPI (local) | `sapi_rate`/`sapi_voice` in `approval_config.json` | When `tts_backend == "sapi"`/`"windows"` |
| `approval_hook.py` `_polly_speak()` | Neural 16kHz | `approval_config.json` `voice_id` | "Approve write to…?" gate |
| `core/command_executor.py` `_polly_speak()` | Neural 16kHz | `_POLLY_VOICE` constant | Sidecar-down fallback |

### iPad mic approval flow

When the bridge is running, Danielle's question plays through PC speakers, then
the next utterance into the **iPad mic** is captured by WhisperStream and routed
to the approval gate via `~/.claude/approval/pending` + `response` signal files.
If the bridge is not running, the PC's **Microphone (Realtek USB Audio)** mic is
used instead (4-second recording window, auto-approve on silence).
