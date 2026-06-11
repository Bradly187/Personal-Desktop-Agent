#!/usr/bin/env bash
# Run this inside WSL2:  wsl bash scripts/start_vllm_server.sh
#
# Launches an OpenAI-compatible vLLM server inside WSL2. The Windows side talks
# to it over loopback (WSL2 NAT localhostForwarding: 0.0.0.0:8000 → Windows
# 127.0.0.1:8000), so VLLMServerInference avoids all the vllm._C
# Windows-compilation issues.
#
# Requires networkingMode=nat in ~/.wslconfig — mirrored-mode loopback was
# broken in both directions on this machine (verified 2026-06-11). Always use
# 127.0.0.1 from Windows, never "localhost": the ::1 attempt adds ~2 s per
# request because NAT forwarding is IPv4-only.
#
# Activate on the Windows side with:
#   python main.py --backend vllm-server [--vllm-server-url http://127.0.0.1:8000]
#
# The --vllm-server-model passed to main.py MUST match the model served below.

set -euo pipefail

# AWQ INT4 default (tuning session 2026-06-11): 2.45x faster than fp16 on the
# command shape (140 ms vs 344 ms p50, ~235 vs ~95 tok/s decode on the RTX
# 5090) at 11/12 on the verb-accuracy suite, and weights shrink 16 GB -> 5.7 GB
# so --gpu-memory-utilization can drop to 0.5. Set
# VLLM_MODEL=meta-llama/Meta-Llama-3.1-8B-Instruct to serve fp16 instead.
MODEL="${VLLM_MODEL:-hugging-quants/Meta-Llama-3.1-8B-Instruct-AWQ-INT4}"
HOST="${VLLM_HOST:-0.0.0.0}"
PORT="${VLLM_PORT:-8000}"
# Default venv is $HOME/.venv-wsl (absolute). Override with VLLM_VENV=<path>.
VENV="${VLLM_VENV:-$HOME/.venv-wsl}"

cd "$(dirname "$0")/.."

# 1. Activate the WSL virtualenv, creating it if missing.
if [[ ! -d "${VENV}" ]]; then
    echo "[start_vllm_server] creating venv ${VENV}"
    python3 -m venv "${VENV}"
fi
# The venv activate script appends to LD_LIBRARY_PATH (libcudart fix); under
# `set -u` that explodes if the variable was never exported. Default it first.
export LD_LIBRARY_PATH="${LD_LIBRARY_PATH:-}"
# shellcheck disable=SC1090
source "${VENV}/bin/activate"

# 2. Install vllm + torch (CUDA 12.8 wheels) if vllm isn't importable yet.
#    Use --extra-index-url (not --index-url) so PyPI stays available for
#    vllm's non-torch dependencies (regex, etc.).
if ! python -c "import vllm" 2>/dev/null; then
    echo "[start_vllm_server] installing vllm + torch (cu128 wheels)..."
    pip install vllm torch \
        --extra-index-url https://download.pytorch.org/whl/cu128
fi

# 3. Serve the model with CUDA graph optimizations.
#    --gpu-memory-utilization 0.5 (16 GB) is plenty for AWQ INT4 weights + KV
#    cache at 8k context and leaves ~15 GB for Whisper/Ollama; measured
#    identical latency to 0.75. Raise it if serving an fp16 model.
#    Attention backend stays on auto (FLASH_ATTN): FLASHINFER measured 2-4x
#    SLOWER on Blackwell/sm_120 with flashinfer 0.6.8 (570 ms vs 140 ms p50,
#    12 s JIT p95 outliers) — do not pass --attention-backend FLASHINFER.
#    (The VLLM_ATTENTION_BACKEND env var is ignored by vLLM 0.21; only the
#    CLI flag selects a backend.)
#    Env vars must be set in the shell environment, not as vllm arguments.
echo "[start_vllm_server] serving ${MODEL} on ${HOST}:${PORT}"
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
export VLLM_WORKER_MULTIPROC_METHOD=spawn
exec vllm serve "${MODEL}" \
    --host "${HOST}" \
    --port "${PORT}" \
    --gpu-memory-utilization "${VLLM_GPU_UTIL:-0.5}" \
    --max-model-len 8192

