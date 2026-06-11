#!/usr/bin/env bash
# Run this inside WSL2:  wsl bash scripts/start_vllm_server.sh
#
# Launches an OpenAI-compatible vLLM server inside WSL2. The Windows side talks
# to it over localhost (WSL2 forwards 0.0.0.0:8000 → Windows localhost:8000),
# so VLLMServerInference avoids all the vllm._C Windows-compilation issues.
#
# Activate on the Windows side with:
#   python main.py --backend vllm-server [--vllm-server-url http://localhost:8000]
#
# The --vllm-server-model passed to main.py MUST match the model served below.

set -euo pipefail

MODEL="${VLLM_MODEL:-meta-llama/Meta-Llama-3.1-8B-Instruct}"
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
#    --gpu-memory-utilization 0.75 leaves ~8 GB headroom for Whisper.
#    Env vars must be set in the shell environment, not as vllm arguments.
echo "[start_vllm_server] serving ${MODEL} on ${HOST}:${PORT}"
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
export VLLM_WORKER_MULTIPROC_METHOD=spawn
exec vllm serve "${MODEL}" \
    --host "${HOST}" \
    --port "${PORT}" \
    --gpu-memory-utilization 0.75 \
    --max-model-len 8192 \
    --dtype float16

