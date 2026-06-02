@echo off
REM ============================================================================
REM  start_laptop_services.bat - launch the laptop compute-node services
REM
REM  Starts the Whisper offload (:8888) and Indexer offload (:9000) services
REM  using the laptop venv. Ollama (:11434) is assumed already running via the
REM  Ollama tray app (OLLAMA_HOST=0.0.0.0 is persisted, so it binds the LAN).
REM
REM  Each service runs in its own window so logs are visible and either can be
REM  restarted independently. Close a window to stop that service.
REM ============================================================================

setlocal
set ROOT=%~dp0
set PY=%ROOT%.venv-laptop\Scripts\python.exe

if not exist "%PY%" (
  echo [ERROR] venv python not found at %PY%
  echo         Create it with:  python -m venv .venv-laptop  ^&^&  .venv-laptop\Scripts\pip install -r requirements.txt
  exit /b 1
)

echo Starting Whisper service on :8888 ...
start "Laptop Whisper :8888" cmd /k "%PY%" "%ROOT%sensors\remote_whisper_service.py" --port 8888

echo Starting Indexer service on :9000 ...
start "Laptop Indexer :9000" cmd /k "%PY%" "%ROOT%inference\remote_indexer_service.py" --port 9000 --watch

echo.
echo Laptop services launching in separate windows.
echo   Whisper : http://0.0.0.0:8888/health
echo   Indexer : http://0.0.0.0:9000/health
echo   Ollama  : http://0.0.0.0:11434/api/tags  (via tray app)
echo.
endlocal
