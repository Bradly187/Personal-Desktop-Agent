#!/usr/bin/env bash
# setup_wsl_deps.sh — Install project dependencies into ~/.venv-wsl
#
# Run ONCE after creating the venv with vLLM.  Idempotent (safe to re-run).
#
# Assumes:
#   - ~/.venv-wsl already exists with vLLM + torch (created during vLLM setup)
#   - E drive is mounted at /mnt/e (run: sudo mount -t drvfs E: /mnt/e)
#
# Usage:
#   bash /mnt/e/Personal_Desktop_Agent/setup_wsl_deps.sh

set -e

PROJ_ROOT="/mnt/e/Personal_Desktop_Agent"
VENV="$HOME/.venv-wsl"
REQ="$PROJ_ROOT/requirements-wsl.txt"

GRN="\033[0;32m"; YLW="\033[0;33m"; RED="\033[0;31m"; RST="\033[0m"
info()  { echo -e "${GRN}[setup]${RST} $*"; }
warn()  { echo -e "${YLW}[warn] ${RST} $*"; }
error() { echo -e "${RED}[error]${RST} $*"; exit 1; }

# ── Checks ────────────────────────────────────────────────────────────────────
[[ -f "$VENV/bin/activate" ]] \
    || error "~/.venv-wsl not found. Create it with: python3 -m venv ~/.venv-wsl"

[[ -f "$REQ" ]] \
    || error "requirements-wsl.txt not found at $REQ. Mount E drive first."

# ── Activate ──────────────────────────────────────────────────────────────────
# shellcheck disable=SC1090
source "$VENV/bin/activate"
info "Active venv: $VIRTUAL_ENV"
info "Python: $(python --version 2>&1)"
info "pip:    $(pip --version 2>&1 | head -1)"

# ── Verify vLLM is present ────────────────────────────────────────────────────
python -c "import vllm; print('vLLM', vllm.__version__)" 2>/dev/null \
    || { warn "vLLM not found in this venv — install it first:"; \
         warn "  pip install vllm  (see vllm_setup.bat for CUDA wheel instructions)"; }

# ── Pre-install build prerequisites ──────────────────────────────────────────
# Several packages (pkuseg via chatterbox, mediapipe, etc.) require numpy and
# scipy to be present in the venv *before* their own wheels are built.
# pip's isolated build env won't find these even if vLLM installed them —
# installing explicitly here guarantees the main requirements pass succeeds.
info "Pre-installing build prerequisites (numpy, scipy, torch) ..."
pip install -q "numpy>=1.24.0" "scipy>=1.10.0"
# torch is already in the venv from vLLM; this is a no-op if already current
pip install -q torch --index-url https://download.pytorch.org/whl/cu128 --upgrade-strategy only-if-needed

# ── Install project deps ──────────────────────────────────────────────────────
info "Installing requirements-wsl.txt ..."
pip install -r "$REQ" --upgrade-strategy only-if-needed

# ── bitsandbytes CUDA build check ─────────────────────────────────────────────
info "Checking bitsandbytes CUDA support ..."
python -c "
import bitsandbytes as bnb
cuda_ok = bnb.cuda_specs is not None
print(f'  bitsandbytes {bnb.__version__}  CUDA={cuda_ok}')
" 2>/dev/null || warn "bitsandbytes import failed — E4B-IT INT4 quantization may not work"

# ── Verify CUDA visible to torch ──────────────────────────────────────────────
python -c "
import torch
print(f'  torch {torch.__version__}  CUDA={torch.cuda.is_available()}', end='')
if torch.cuda.is_available():
    cap = torch.cuda.get_device_capability(0)
    name = torch.cuda.get_device_name(0)
    print(f'  device={name}  sm_{cap[0]}{cap[1]}')
else:
    print()
" 2>/dev/null || warn "PyTorch CUDA check failed"

# ── Add project root to venv's pth file for easy import ──────────────────────
SP=$(python -c "import sysconfig; print(sysconfig.get_path('purelib'))")
PTH="$SP/desktop_agent.pth"
if [[ ! -f "$PTH" ]] || ! grep -q "$PROJ_ROOT" "$PTH" 2>/dev/null; then
    echo "$PROJ_ROOT" > "$PTH"
    echo "$PROJ_ROOT/mcp_server" >> "$PTH"
    info "Added project root to venv path: $PTH"
else
    info "Project root already in venv path."
fi

info ""
info "Setup complete."
info "Start the agent with:"
info "  bash $PROJ_ROOT/start_agent_wsl.sh"
