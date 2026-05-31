@echo off
REM Double-click to start the vLLM OpenAI-compatible server inside WSL2.
REM Forwards to scripts/start_vllm_server.sh, which activates .venv-wsl,
REM installs vllm if needed, and runs `vllm serve` on 0.0.0.0:8000.
REM
REM The Windows side connects with:  python main.py --backend vllm-server
wsl bash "%~dp0start_vllm_server.sh"
pause
