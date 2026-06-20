# Cloud Cost Savings Plan — Personal Desktop Agent

**Baseline:** ~$80/month cloud spend  
**Target:** $25–35/month  
**Reduction:** ~55–69%  
**Date estimated:** 2026-05-25

---

## Current Cost Breakdown (estimated)

| Service | Cost/month | Notes |
|---------|-----------|-------|
| **Claude Code API (Anthropic)** | ~$52 | Dominant driver. Every Claude Code session uses claude-sonnet-4-6. Heavy interactive coding sessions burn 2–5M tokens/day. Hardwired — Claude Code cannot use local models. |
| **VisionGrounder (claude-sonnet-4-6 vision)** | ~$11 | Each CLICK with named target = 1 API call with a screenshot (~1500 tokens image + prompt). ~80 calls/day × 30 days at $3/MTok input + $15/MTok output ≈ $11/month. |
| **Polly TTS (Danielle, Generative engine)** | ~$7 | ~300K chars/month (CLARIFY events, approval gate). Polly Neural: $16/million chars. |
| **AWS Bedrock claude-haiku (Gate 3/4 fallback)** | ~$5 | Cloud fallback for voice misrecognitions that survive Stage 1+2 correction. ~200 calls/month at $0.025/call average. |
| **Amazon Transcribe (Stage 2 re-transcription)** | ~$3 | Rare — fires on Gate 1 failures only. ~15–30 minutes/month at $0.024/min. |
| **Other (data transfer, misc AWS)** | ~$2 | S3, CloudWatch, etc. |
| **Total** | **~$80** | |

---

## Savings Plan — Three Levers

### Lever 1 — Continue.dev (already configured) ✅
**Estimated savings: $22–35/month**

Continue.dev offloads interactive coding tasks from Claude Code to local Ollama models.

| Task type | Before | After |
|-----------|--------|-------|
| Tab autocomplete | Nothing (config broken — dead model) | qwen3-coder:30b FIM — free |
| Refactor selected function | Claude Code session (~$0.08–0.40) | Continue.dev `/edit` — free |
| Explain this code | Claude Code session | Continue.dev `/explain` — free |
| Generate unit tests | Claude Code session | Continue.dev `/tests` — free |
| Write a commit message | Claude Code session | Continue.dev `/commit` — free |
| ML/QC theory questions | Claude Code session | gemma3:27b chat — free |
| Math derivation | Claude Code session | deepseek-r1:8b chain-of-thought — free |
| Quick one-liner lookup | Claude Code session | llama3.1:8b (373ms) — free |

**Tasks that still require Claude Code (cloud):**
- Cross-repo architectural refactors (10+ files)
- Complex debugging with >50K token context
- Tasks requiring MCP tool use (pyautogui, browser, desktop control)
- Anything requiring the approval hook pipeline

Realistic offload rate: **50–65% of Claude Code queries** → saves **$26–34/month** from the $52 Claude Code baseline.

**How to route:**
```
Code completion / small edit    → Continue.dev  (qwen3-coder:30b, free)
Research / theory / math        → Continue.dev  (gemma3:27b or deepseek-r1, free)
Complex multi-file / MCP tools  → Claude Code   (cloud, necessary)
```

---

### Lever 2 — Local VisionGrounder ✅ (implemented 2026-05-25)
**Estimated savings: $8–12/month**

`vision_grounder.py` now uses `qwen3-vl:30b` via Ollama as the primary backend (free, ~0.4s on RTX 5090). Anthropic claude-sonnet-4-6 is kept as an automatic fallback if Ollama is unavailable.

| Before | After |
|--------|-------|
| claude-sonnet-4-6 for every CLICK with named target (~$0.004/call) | qwen3-vl:30b via Ollama (free) |
| ~80 calls/day × 30 = 2400 calls × $0.004 ≈ $9.60/month | $0 |

The fallback chain is preserved: local vision → Anthropic vision → gaze coords → OCR → CLARIFY.

**Activation:** Already active. No config change needed — `backend="ollama"` is now the default.

---

### Lever 3 — Chatterbox TTS ✅ (activated 2026-05-25)
**Estimated savings: $5–7/month**

`approval_config.json` → `tts_backend` switched from `"polly"` to `"chatterbox"`. The Chatterbox local GPU TTS backend was already fully implemented in `chatterbox_tts.py` — this change activates it.

| Before | After |
|--------|-------|
| Amazon Polly Danielle (Generative engine, $16/million chars) | Chatterbox local GPU (RTX 5090, free) |
| ~300K chars/month ≈ $4.80 Polly fee + $2 sidecar AWS costs | $0 operating cost |

**Note on quality:** Chatterbox (MIT licence, Resemble AI) produces natural prosody with emotion exaggeration controls. Set `chatterbox_exaggeration` (0.25–2.0) in `approval_config.json` for desired expressiveness. Currently set to 0.5 (neutral, clear).

**Revert if needed:** Change `tts_backend` back to `"polly"` — takes effect immediately, no restart.

---

## Total Savings Summary

| Change | Status | Monthly savings |
|--------|--------|----------------|
| Continue.dev local autocomplete + chat | ✅ Configured | $22–35 |
| VisionGrounder → qwen3-vl:30b | ✅ Implemented | $8–12 |
| Chatterbox TTS | ✅ Activated | $5–7 |
| **Total reduction** | | **$35–54** |
| **New estimated monthly bill** | | **$26–45** |
| **Reduction from $80 baseline** | | **44–68%** |

---

## What Remains On Cloud

These cannot be moved to local models without significant architectural changes:

| Cost | Why it stays | Path to local |
|------|-------------|---------------|
| **Claude Code API** (~$18–30/month after offloading) | Claude Code is hardwired to Anthropic API — the tool itself uses cloud models | N/A — Continue.dev is the local complement, not a replacement |
| **Bedrock haiku fallback** (~$5/month) | Gate 3/4 fires when local Ollama fails or VRAM is exhausted | Reduce by improving Gate 1/2 accuracy; lower when vLLM activates |
| **Amazon Transcribe** (~$3/month) | Stage 2 re-transcription for voice misrecognitions | Already minimal; whisper_stream accuracy gains reduce this |

---

## One-Time Setup Checklist

- [x] Pull `nomic-embed-text` — `ollama pull nomic-embed-text` (done 2026-05-25)
- [x] Update `~/.continue/config.yaml` with full fleet + slash commands (done)
- [x] VisionGrounder `backend="ollama"` — default now, Anthropic fallback preserved
- [x] Chatterbox TTS activated in `approval_config.json`
- [ ] First-time codebase index — open Continue.dev in VS Code, type `@codebase` once to trigger index (~2 min for full repo)
- [ ] Test autocomplete latency — if keypress latency > 800ms with qwen3-coder:30b, switch autocomplete model to `llama3.2:3b` in config.yaml

---

## Monitoring

To track actual savings, check cloud bills in:
- **Anthropic Console** → `console.anthropic.com/billing` — Claude Code usage
- **AWS Cost Explorer** → filter by service: Polly, Transcribe, Bedrock
- **Ollama** — zero cost; monitor via `nvidia-smi` for VRAM usage (should stay under 27GB with all models loaded)

Target: Next bill ≤ $40/month. If still above $50, audit the Bedrock haiku Gate 3/4 firing rate in `agent.db` → `routing_log` table.
