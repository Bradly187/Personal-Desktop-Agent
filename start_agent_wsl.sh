#!/usr/bin/env bash
# start_agent_wsl.sh — Launch Personal Desktop Agent from WSL2 Ubuntu
#
# Prerequisites:
#   1. Run start_windows_proxy.bat in Windows PowerShell/CMD first
#      (handles pyautogui / Win32 desktop control from Windows Python)
#   2. E drive must be mountable (automated below)
#   3. ~/.venv-wsl must have vLLM + project deps installed
#      (run setup_wsl_deps.sh once if not done)
#
# Usage (from within WSL):
#   bash /mnt/e/Personal_Desktop_Agent/start_agent_wsl.sh [main.py args...]
#
# Extra args are passed to main.py, e.g.:
#   bash start_agent_wsl.sh --debug --vllm-embed
#
# What runs where:
#   WSL  — vLLM (E4B-IT command + Gemma 4 31B specialists), FusionEngine,
#           HybridCoordinator, IPadBridge, WhisperStream, all pipeline code
#   Win  — pyautogui / win32gui (via windows_action_proxy.py on port 8768)

set -e

# ── Configuration ────────────────────────────────────────────────────────────
PROJ_WIN_DRIVE="E:"
MOUNT_POINT="/mnt/e"
PROJ_ROOT="$MOUNT_POINT/Personal_Desktop_Agent"
VENV="$HOME/.venv-wsl"
PROXY_PORT=8768
PROXY_URL="http://127.0.0.1:${PROXY_PORT}"

# ── Colours ──────────────────────────────────────────────────────────────────
GRN="\033[0;32m"; YLW="\033[0;33m"; RED="\033[0;31m"; RST="\033[0m"
info()  { echo -e "${GRN}[agent]${RST} $*"; }
warn()  { echo -e "${YLW}[warn] ${RST} $*"; }
error() { echo -e "${RED}[error]${RST} $*"; exit 1; }

# ── 1. Mount E drive ─────────────────────────────────────────────────────────
if mountpoint -q "$MOUNT_POINT" 2>/dev/null; then
    info "$MOUNT_POINT already mounted."
else
    info "Mounting $PROJ_WIN_DRIVE → $MOUNT_POINT ..."
    sudo mkdir -p "$MOUNT_POINT"
    # metadata option: preserves Unix permissions on DrvFs
    sudo mount -t drvfs "$PROJ_WIN_DRIVE" "$MOUNT_POINT" \
        -o "metadata,uid=$(id -u),gid=$(id -g),umask=22" \
        || error "Failed to mount $PROJ_WIN_DRIVE. Run: sudo mount -t drvfs E: /mnt/e"
    info "Mounted."
fi

[[ -d "$PROJ_ROOT" ]] || error "Project not found at $PROJ_ROOT"

# ── 2. Activate venv ─────────────────────────────────────────────────────────
[[ -f "$VENV/bin/activate" ]] \
    || error "venv not found at $VENV — run setup_wsl_deps.sh first"
# shellcheck disable=SC1090
source "$VENV/bin/activate"
info "venv: $VENV  (Python $(python --version 2>&1 | cut -d' ' -f2))"

# ── 3. CUDA / Blackwell environment ──────────────────────────────────────────
# sm_120 (Blackwell GB202/GB10) — required for RTX 5090 builds from source.
export TORCH_CUDA_ARCH_LIST="12.0"
# Flash Attention 3 is not yet supported on Blackwell; use FA2.
export VLLM_FLASH_ATTN_VERSION=2
# FlashInfer's top-k/top-p sampler can't read SM 12.x capability under CUDA 12.8
# ("SM 12.x requires CUDA >= 12.9" → "FlashInfer requires GPUs with sm75 or higher").
# Force vLLM's native Torch sampler, which works on Blackwell via torch cu128.
export VLLM_USE_FLASHINFER_SAMPLER=0
# Expose the RTX 5090 only; prevents accidental multi-GPU initialisation.
export CUDA_VISIBLE_DEVICES=0
# Ninja build tool (needed for vLLM JIT compilation).
export PATH="$HOME/.local/bin:$PATH"

# libcudart.so.13 version fix: the system has both CUDA 12.8 and CUDA 13.0;
# the interactive shell ldconfig may find the 12.8 version which has the wrong
# internal version symbol. Prefer CUDA 13.0 which satisfies vllm._C.abi3.so.
export LD_LIBRARY_PATH="/usr/local/cuda-13.0/targets/x86_64-linux/lib:/usr/lib/wsl/lib:${LD_LIBRARY_PATH}"

# ── 4. Windows action proxy ───────────────────────────────────────────────────
# Desktop control (pyautogui, win32gui, clipboard) is proxied to Windows Python.
# Run start_windows_proxy.bat on the Windows side before this script.
info "Checking Windows action proxy on :${PROXY_PORT} ..."
if curl -sf "${PROXY_URL}/health" >/dev/null 2>&1; then
    info "Proxy ready."
else
    warn "Proxy not responding on :${PROXY_PORT}."
    warn "Run start_windows_proxy.bat in Windows PowerShell, then re-run this script."
    warn "Desktop control (CLICK, TYPE, SCROLL, etc.) will fail without the proxy."
    warn "Continuing anyway — voice/gaze/tilt commands that don't require desktop"
    warn "actions (CLARIFY, EXPLAIN, READ_SCREEN, etc.) will still work."
fi
export WSL_ACTION_PROXY="${PROXY_URL}"

# ── 5. Python path ────────────────────────────────────────────────────────────
export PYTHONPATH="${PROJ_ROOT}:${PROJ_ROOT}/mcp_server"

# ── 6. HuggingFace cache (keep in WSL home, not on E drive) ──────────────────
# Models are cached in ~/.cache/huggingface/ inside WSL — faster than DrvFs.
# Override here only if you want models on E drive (slower but persists across
# WSL resets):
# export HF_HOME="/mnt/e/hf_cache"

# ── 7. Log setup ─────────────────────────────────────────────────────────────
mkdir -p "${PROJ_ROOT}/logs"
LOG="${PROJ_ROOT}/logs/agent_wsl_startup.log"
# Roll previous log
[[ -f "$LOG" ]] && mv "$LOG" "${LOG%.log}.prev.log"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] WSL agent starting" > "$LOG"

# ── 8. Launch ────────────────────────────────────────────────────────────────
info "Starting Personal Desktop Agent (vLLM pool + Gemma 4) ..."
info "Log: $LOG"
cd "$PROJ_ROOT"
exec python main.py \
    --backend vllm \
    --vllm-pool \
    "$@" \
    2>&1 | tee -a "$LOG"
