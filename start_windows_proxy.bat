@echo off
:: start_windows_proxy.bat — start the Windows desktop action proxy for WSL agent
:: Run this on Windows before launching start_agent_wsl.sh in WSL.

set PORT=8768
set LOG=logs\windows_proxy.log

cd /d "%~dp0"
if not exist logs mkdir logs

:: Check if already running
curl -s "http://127.0.0.1:%PORT%/health" >nul 2>&1
if not errorlevel 1 (
    echo [proxy] Already running on :%PORT%
    exit /b 0
)

echo [proxy] Starting Windows Action Proxy on :%PORT% ...
echo [proxy] Log: %LOG%

start /B "" ".venv\Scripts\python.exe" windows_action_proxy.py --port %PORT% >> %LOG% 2>&1

:: Wait up to 5 seconds for it to come up
set /a tries=0
:wait_loop
timeout /t 1 /nobreak >nul
curl -s "http://127.0.0.1:%PORT%/health" >nul 2>&1
if not errorlevel 1 (
    echo [proxy] Ready on http://127.0.0.1:%PORT%
    exit /b 0
)
set /a tries+=1
if %tries% lss 5 goto wait_loop

echo [warn] Proxy did not start within 5s — check %LOG%
exit /b 1
