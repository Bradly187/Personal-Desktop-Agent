# llama-server Setup — Qwen3.6-27B on RTX 5090

Roadmap item #6: `LlamaCppInference` backend using llama.cpp's OpenAI-compatible server.

## Why llama.cpp

- **Qwen3.6-27B** (dense 27B): 68.9% SWE-Bench Verified, ~158 tok/s at Q4_K_M on RTX 5090
- Model fits fully in VRAM at Q4_K_M (17 GB) alongside Whisper (4.2 GB) — 10.8 GB headroom
- For larger models (72B), llama.cpp splits across VRAM + RAM via `--n-gpu-layers`
- vLLM only supports CUDA weights; llama.cpp supports GGUF which is more widely available

## Installation

```bat
# Option A: Pre-built Windows binaries (recommended)
# Download from: https://github.com/ggerganov/llama.cpp/releases
# Get: llama-server.exe + required DLLs (cublas, cudart, etc.)

# Option B: Build from source
git clone https://github.com/ggerganov/llama.cpp
cd llama.cpp
cmake -B build -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=120
cmake --build build --config Release -j8
# Binary at: build\bin\Release\llama-server.exe
```

## Model Download

```bat
# Qwen3.6-27B Q4_K_M (recommended — fits fully in VRAM)
# ~17 GB download
hf download Qwen/Qwen3.6-27B-GGUF \
    Qwen3.6-27B-Q4_K_M.gguf \
    --local-dir models/

# Alternatively, use lm-studio or ollama (gguf command) to download
```

## Server Launch

```bat
REM Full VRAM — Qwen3.6-27B Q4_K_M (17 GB VRAM, fits alongside Whisper)
llama-server.exe ^
    --model models\Qwen3.6-27B-Q4_K_M.gguf ^
    --n-gpu-layers 999 ^
    --ctx-size 16384 ^
    --port 8080 ^
    --host 127.0.0.1 ^
    --threads 6

REM For larger models (70B+): split across VRAM + RAM
REM --n-gpu-layers 40  (tune until VRAM is ~95% used)
REM Add to PATH or create llama_server.bat for convenience
```

## Activate in Agent

```bat
python main.py --backend llamacpp --llamacpp-host http://localhost:8080
```

## VRAM Budget (with Qwen3.6-27B)

| Component | VRAM |
|-----------|------|
| Whisper large-v3 | 4.2 GB |
| Qwen3.6-27B Q4_K_M | 17.0 GB |
| KV cache (16K ctx) | ~3.0 GB |
| System / driver | 1.5 GB |
| **Total** | **~25.7 GB** |
| **Free (32 GB)** | **~6.3 GB** |

6.3 GB headroom — comfortable for typical usage. For very long contexts, reduce
`--ctx-size` to 8192 to free ~1.5 GB of KV cache.

## Benchmark

```bat
python benchmark_models.py  # tests Ollama backend
# For llama.cpp backend:
python -c "
import asyncio, time
from local_inference import LlamaCppInference
from command_executor import Command

async def bench():
    inf = LlamaCppInference()
    cmd = Command(text='click the save button', action='', source='test')
    for i in range(5):
        t0 = time.monotonic()
        r = await inf.infer(cmd)
        print(f'{i}: {r!r}  ({(time.monotonic()-t0)*1000:.0f}ms)')

asyncio.run(bench())
"
```
