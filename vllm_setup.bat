@echo off
REM vllm_setup.bat — Install vLLM with CUDA 12.8 torch wheels for RTX 5090
REM Run once from the project venv. Requires ~6 GB disk space.
REM See ROADMAP.md item #1 and docs/vllm_setup.md for full details.

echo.
echo === vLLM Setup for RTX 5090 (CUDA 12.8) ===
echo.

REM Check we're in a venv
python -c "import sys; assert hasattr(sys, 'real_prefix') or sys.prefix != sys.base_prefix, 'Not in a venv'" 2>nul
if errorlevel 1 (
    echo [WARN] Not running in a virtual environment. Activate your venv first:
    echo   .venv\Scripts\activate
    echo.
    pause
    exit /b 1
)

echo [1/3] Installing PyTorch with CUDA 12.8 support...
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128
if errorlevel 1 (
    echo [FAIL] torch install failed.
    pause
    exit /b 1
)

echo.
echo [2/3] Installing vLLM...
pip install vllm --extra-index-url https://download.pytorch.org/whl/cu128
if errorlevel 1 (
    echo [FAIL] vllm install failed. Check CUDA version matches torch.
    pause
    exit /b 1
)

REM Blackwell (sm_120) requires FA2 — FA3 not yet supported on RTX 5090
set VLLM_FLASH_ATTN_VERSION=2
set TORCH_CUDA_ARCH_LIST=12.0

echo.
echo [3/3] Verifying installation...
python -c "import vllm; print('vLLM version:', vllm.__version__)"
python -c "import torch; print('CUDA available:', torch.cuda.is_available()); print('CUDA version:', torch.version.cuda); cap = torch.cuda.get_device_capability(0); print('Compute capability:', cap); assert cap == (12, 0), f'Expected sm_120 for RTX 5090, got {cap}'"
if errorlevel 1 (
    echo [WARN] Compute capability check failed — torch may not see sm_120.
    echo        Re-run step 1 and confirm cu128 wheels were installed.
)

echo.
echo === Setup complete ===
echo.
echo To activate vLLM backend:
echo   python main.py --backend vllm [--vllm-model meta-llama/Meta-Llama-3.1-8B-Instruct]
echo.
echo To benchmark (compare Ollama vs vLLM):
echo   python benchmark_models.py --vllm meta-llama/Meta-Llama-3.1-8B-Instruct
echo.
echo For speculative decoding (roadmap item #9), use --speculative flag:
echo   python main.py --backend vllm --speculative
echo.
echo This passes --speculative-model llama3.1:8b and --num-speculative-tokens 5
echo to the AsyncEngineArgs. Expected acceptance rate: 60-80%% on code tasks.
echo Expected throughput: ~2,400-3,500 tok/s (vs ~1,186 tok/s without speculative).
echo.
echo For manual vLLM server with speculative decoding:
echo   python -m vllm.entrypoints.openai.api_server \
echo       --model Qwen/Qwen2.5-Coder-32B-Instruct-AWQ \
echo       --speculative-model meta-llama/Meta-Llama-3.1-8B-Instruct \
echo       --gpu-memory-utilization 0.85 \
echo       --max-model-len 32768 \
echo       --swap-space 64
echo.
pause