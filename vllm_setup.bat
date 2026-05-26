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
pip install vllm
if errorlevel 1 (
    echo [FAIL] vllm install failed. Check CUDA version matches torch.
    pause
    exit /b 1
)

echo.
echo [3/3] Verifying installation...
python -c "import vllm; print('vLLM version:', vllm.__version__)"
python -c "import torch; print('CUDA available:', torch.cuda.is_available()); print('CUDA version:', torch.version.cuda)"

echo.
echo === Setup complete ===
echo.
echo To activate vLLM backend:
echo   python main.py --backend vllm [--vllm-model meta-llama/Meta-Llama-3.1-8B-Instruct]
echo.
echo To benchmark (compare Ollama vs vLLM):
echo   python benchmark_models.py --vllm meta-llama/Meta-Llama-3.1-8B-Instruct
echo.
echo For speculative decoding (roadmap item #9), start vLLM with:
echo   python -m vllm.entrypoints.openai.api_server \
echo       --model Qwen/Qwen2.5-Coder-32B-Instruct-AWQ \
echo       --speculative-model meta-llama/Meta-Llama-3.1-8B-Instruct \
echo       --gpu-memory-utilization 0.85 \
echo       --max-model-len 32768 \
echo       --swap-space 64
echo.
pause
