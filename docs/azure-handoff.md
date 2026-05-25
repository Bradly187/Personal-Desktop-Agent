# Azure AI Foundry — Handoff & Integration Guide

**Project:** Personal Desktop Agent (Brad Tarver)  
**GitHub:** https://github.com/Bradly187/Personal-Desktop-Agent  
**Foundry project:** `jbtarver197-8957`  
**Foundry agent:** `b-rad-Desktop-Agent` (gpt-4.1-mini, voice mode enabled)  
**Date:** 2026-05-25

---

## What This Project Is

A multimodal accessibility desktop-control system for a user with rheumatoid arthritis. An iPad Pro streams sensor data (voice, tilt, gaze, gestures, LiDAR) over WebSocket to a Windows PC (RTX 5090). The PC runs local LLM inference and executes desktop actions via pyautogui/Win32.

**16-verb action vocabulary:**

| Category | Verbs |
|----------|-------|
| Accessibility (11) | `CLICK` `MOUSEDOWN` `MOUSEUP` `SCROLL` `TYPE` `OPEN` `CLOSE` `HOTKEY` `DICTATE` `CLARIFY` `SCREENSHOT` |
| Dev-agent (5) | `WRITE_FILE` `RUN_TERMINAL` `EXPLAIN` `SEARCH_WEB` `READ_SCREEN` |

Every command flows through a `Command` dataclass. The LLM's job is to convert natural-language speech into exactly one verb + argument (e.g. `CLICK Save button`, `OPEN Chrome`).

---

## Azure Credentials

> **ACTION REQUIRED:** The key below was stored in plaintext and must be rotated at  
> https://ai.azure.com → Project settings → API keys → Regenerate

```
# .env  (never commit this file — it's in .gitignore)
AZURE_FOUNDRY_API_KEY=<regenerated-key-here>
AZURE_FOUNDRY_ENDPOINT=https://jbtarver197-8957.services.ai.azure.com/
AZURE_FOUNDRY_MODEL=gpt-4.1-mini
```

Install the SDK:
```bash
pip install openai azure-ai-projects azure-identity
```

---

## Inference Architecture

The PC-side inference stack uses an abstract base class so backends are swappable without touching routing logic:

```
HybridCoordinator
  └── LocalInference (ABC)          ← local_inference.py
        ├── OllamaInference          ← active default (llama3.1:8b, 373ms p50)
        ├── VLLMInference            ← code complete; blocked on CUDA 13.x wheels
        └── AzureInference           ← ADD THIS (see below)
```

Cloud fallback is already wired: when local inference fails, `hybrid_coordinator.py` calls raw AWS Bedrock (`claude-haiku-4-5`). **Azure can replace or supplement this path.**

### Gate routing (hybrid_coordinator.py)

| Gate | Trigger | Backend |
|------|---------|---------|
| Gate 0 | Privacy filter | Block — never leaves device |
| Gate 1 | Simple command | Local Ollama / vLLM |
| Gate 2 | Needs context | Local Ollama with few-shot |
| Gate 3 | Complex / ambiguous | AWS Bedrock (claude-haiku-4-5) |
| Gate 4 | Dev-agent domain | DevAgent → ModelRouter → specialist |

---

## Adding AzureInference to local_inference.py

Add this class at the bottom of `local_inference.py`, before the Nemotron tombstone comment:

```python
class AzureInference(LocalInference):
    """Azure AI Foundry / Azure OpenAI inference backend.

    Uses the OpenAI-compatible endpoint exposed by Azure AI Foundry.
    Set credentials in environment variables (never hardcode):
        AZURE_FOUNDRY_ENDPOINT  — e.g. https://<project>.services.ai.azure.com/
        AZURE_FOUNDRY_API_KEY   — rotate at ai.azure.com → Project settings
        AZURE_FOUNDRY_MODEL     — e.g. gpt-4.1-mini, gpt-4o, gpt-4o-mini

    Benchmarked accuracy target: >= 100% on 12-prompt command eval suite
    (same suite used for OllamaInference baseline).
    """

    def __init__(
        self,
        model: str | None = None,
        endpoint: str | None = None,
        api_key: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        import os
        self.model    = model    or os.environ["AZURE_FOUNDRY_MODEL"]
        self.endpoint = endpoint or os.environ["AZURE_FOUNDRY_ENDPOINT"]
        self.api_key  = api_key  or os.environ["AZURE_FOUNDRY_API_KEY"]
        self.timeout  = timeout
        self._available: bool | None = None

    async def infer(
        self,
        cmd: Command,
        few_shot_examples: list[dict] | None = None,
    ) -> str:
        try:
            from openai import AsyncAzureOpenAI
        except ImportError:
            return "CLARIFY openai package not installed — run: pip install openai"

        prompt = _build_prompt(cmd, few_shot_examples)
        client = AsyncAzureOpenAI(
            azure_endpoint=self.endpoint,
            api_key=self.api_key,
            api_version="2024-12-01-preview",
        )

        t0 = time.monotonic()
        try:
            resp = await client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=64,
            )
            action = resp.choices[0].message.content.strip().splitlines()[0].strip()
            latency_ms = (time.monotonic() - t0) * 1000
            log.info("AzureInference: %r → %r (%.0f ms)", cmd.text, action, latency_ms)
            self._available = True
            return action
        except Exception as exc:
            self._available = False
            log.error("AzureInference failed: %s", exc)
            return f"CLARIFY azure inference error: {exc}"

    def get_status(self) -> dict:
        return {
            "backend": "azure",
            "model": self.model,
            "endpoint": self.endpoint,
            "available": self._available,
        }
```

---

## Wiring Azure as the Cloud Fallback (hybrid_coordinator.py)

Find the Bedrock fallback block in `_route_gate3()` and add Azure as primary cloud path:

```python
# In hybrid_coordinator.py — _route_gate3 or _cloud_fallback
# Replace or supplement the Bedrock call:

async def _cloud_fallback(self, cmd: Command) -> str:
    # 1. Try Azure (lower latency than Bedrock for short commands)
    if self._azure:
        try:
            return await self._azure.infer(cmd)
        except Exception as exc:
            log.warning("Azure fallback failed: %s — trying Bedrock", exc)

    # 2. Fall back to Bedrock (existing path)
    return await self._bedrock_infer(cmd)
```

Then in `HybridCoordinator.__init__`:
```python
from local_inference import AzureInference
import os

self._azure = None
if os.environ.get("AZURE_FOUNDRY_API_KEY"):
    self._azure = AzureInference()
    log.info("HybridCoordinator: Azure fallback enabled (%s)", self._azure.model)
```

---

## Switching the Primary Backend to Azure

To use Azure instead of Ollama as the Gate 1 primary (e.g. if Ollama is down or during evaluation):

In `main.py`, change the inference instantiation block:

```python
# Current (around line 486):
inference = OllamaInference()

# To use Azure as primary:
from local_inference import AzureInference
inference = AzureInference()   # reads credentials from environment
```

Pass `inference` to `HybridCoordinator(inference=inference, ...)` as usual.

---

## Model Recommendations for Each Domain

| Domain | Current model | Recommended Azure model | Why |
|--------|-------------|------------------------|-----|
| Command (verb-first) | llama3.1:8b | gpt-4o-mini or gpt-4.1-mini | Fast, follows format reliably |
| Code / DevAgent | qwen3-coder:30b | gpt-4o | Best code reasoning |
| Math | mathstral via Ollama | gpt-4o | Strong math; no local alternative needed |
| Vision grounding | claude-sonnet-4-6 (Anthropic) | gpt-4o (vision) | Already via Anthropic — keep as-is |
| Cloud fallback | claude-haiku-4-5 (Bedrock) | gpt-4.1-mini | Azure is already configured; consolidate |

---

## Running the Agent with Azure

```powershell
# Set credentials in the shell before starting
$env:AZURE_FOUNDRY_ENDPOINT = "https://jbtarver197-8957.services.ai.azure.com/"
$env:AZURE_FOUNDRY_API_KEY  = "<your-rotated-key>"
$env:AZURE_FOUNDRY_MODEL    = "gpt-4.1-mini"

# Start the full pipeline
python main.py --debug
```

Or add these to a `.env` file (already gitignored) and load them with `python-dotenv`:
```python
# At the top of main.py
from dotenv import load_dotenv
load_dotenv()
```

---

## Key Files Quick Reference

| File | What it does | Azure touchpoint |
|------|-------------|-----------------|
| `local_inference.py` | LLM inference ABC + Ollama/vLLM backends | Add `AzureInference` class here |
| `hybrid_coordinator.py` | 5-gate routing; Bedrock fallback | Add Azure as primary cloud fallback |
| `model_router.py` | Domain-specific specialist model selection | Add Azure model variants per domain |
| `dev_agent.py` | Plan→execute→reflect dev loop | Can use Azure for CODE/PLAN domains |
| `vision_grounder.py` | Named UI target → pixel coords via vision API | Uses Anthropic; Azure GPT-4o vision is a drop-in |
| `whisper_stream.py` | GPU speech transcription | Not a cloud call — stays local |
| `acoustic_profiler.py` | VAD threshold calibration | Not a cloud call — stays local |
| `approval_config.json` | Per-tool voice approval policy | No Azure config here |
| `docs/desktop-agent-overview.md` | Full architecture rundown | Read this for deeper context |

---

## Current Open Items Relevant to Azure

1. **Rotate API key** — `docs/API Key Azure Foundry.txt` was in plaintext; regenerate at ai.azure.com
2. **Benchmark AzureInference** — run `python benchmark_models.py` once `AzureInference` is added to compare latency vs Ollama baseline (373ms p50)
3. **vLLM still blocked** — CUDA 13.x torch wheels not yet available for RTX 5090; Azure is a viable production path in the interim
4. **Gate 3 cloud consolidation** — currently splits across Bedrock (claude-haiku) and Anthropic (vision); Azure can unify both under one credential/billing account if preferred
