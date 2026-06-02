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

REM (1)(2) Activate the venv so its Scripts + DLL directories are on PATH (chromadb
REM and other native deps resolve under the minimal login environment) and the
REM correct interpreter is guaranteed regardless of the system PATH at startup.
call "%ROOT%.venv-laptop\Scripts\activate.bat"

REM (3) Wait for the LAN to come up before launching. We look for the home subnet
REM (192.168.x) specifically: the 172.16.x Ethernet is a virtual adapter that is
REM "up" instantly and must NOT satisfy the check. Pure cmd (no powershell, which
REM fails under for/f with "input redirection not supported"). Capped at ~60s.
REM If your LAN isn't 192.168.x, change the subnet below.
echo Waiting for network (LAN 192.168.x) ...
set _tries=0
:waitnet
ipconfig | findstr /C:"192.168." >nul 2>&1
if %errorlevel%==0 goto netok
set /a _tries+=1
if %_tries% geq 20 (
  echo   network not ready after ~60s - continuing anyway
  goto netok
)
timeout /t 3 /nobreak >nul
goto waitnet
:netok
echo   network ready.

REM Launch python directly in each window (title + working dir). This avoids the
REM `cmd /k` quote-mangling that breaks when both the exe and the script path are
REM quoted. The window stays open while the service runs and shows its logs; it
REM closes when the service stops (re-run this script to restart).
REM Idempotent: skip a service if its port is already listening (safe to run at
REM every login / re-run without producing "port in use" error windows).
echo Starting Whisper service on :8888 ...
netstat -ano | findstr "LISTENING" | findstr ":8888" >nul 2>&1
if %errorlevel%==0 (
  echo   already listening on :8888 - skipping
) else (
  start "Laptop Whisper :8888" /D "%ROOT%" "%PY%" "%ROOT%sensors\remote_whisper_service.py" --port 8888
)

echo Starting Indexer service on :9000 ...
netstat -ano | findstr "LISTENING" | findstr ":9000" >nul 2>&1
if %errorlevel%==0 (
  echo   already listening on :9000 - skipping
) else (
  start "Laptop Indexer :9000" /D "%ROOT%" "%PY%" "%ROOT%inference\remote_indexer_service.py" --port 9000 --watch
)

echo.
echo Laptop services launching in separate windows.
echo   Whisper : http://0.0.0.0:8888/health
echo   Indexer : http://0.0.0.0:9000/health
echo   Ollama  : http://0.0.0.0:11434/api/tags  (via tray app)
echo.
endlocal
