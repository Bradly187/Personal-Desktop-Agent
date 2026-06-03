@echo off
:: start_desktop.bat — Personal Desktop Agent launcher
::
:: Starts all three services that must run together:
::   1. TTS sidecar   (Node.js  :8766)
::   2. Main agent    (Python   :8765)
::   3. (Ollama is assumed already running via its system tray app)
::
:: Each service opens in its own titled window so output is visible and
:: services can be stopped independently by closing the window.
:: Idempotent: already-running services are skipped.
::
:: Double-click from desktop, or add to Windows Startup folder:
::   shell:startup  →  paste a shortcut here

setlocal EnableDelayedExpansion
set ROOT=%~dp0
set PY=%ROOT%.venv\Scripts\python.exe
set NODE=%ROOT%tts_service\node_modules\.bin\node
where node >nul 2>&1 && set NODE=node

title Personal Desktop Agent — Startup

:: ── guard: python venv ────────────────────────────────────────────────────
if not exist "%PY%" (
    echo [ERROR] Python venv not found: %PY%
    echo         Run:  python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)

:: ── guard: node_modules ───────────────────────────────────────────────────
if not exist "%ROOT%tts_service\node_modules" (
    echo [INFO] Installing TTS sidecar dependencies...
    pushd "%ROOT%tts_service"
    call npm install --silent
    popd
)

:: ── logs dir ──────────────────────────────────────────────────────────────
if not exist "%ROOT%logs" mkdir "%ROOT%logs"

:: ─────────────────────────────────────────────────────────────────────────
:: 1. TTS sidecar (:8766)
:: ─────────────────────────────────────────────────────────────────────────
echo Checking TTS sidecar (:8766)...
curl -sf "http://127.0.0.1:8766/health" >nul 2>&1
if %errorlevel%==0 (
    echo   [SKIP] TTS sidecar already running.
) else (
    echo   [START] TTS sidecar...
    start "TTS Sidecar :8766" /D "%ROOT%tts_service" cmd /k "title TTS Sidecar :8766 && node server.js"
    :: Wait up to 6s for it to come up
    set /a _t=0
    :tts_wait
    timeout /t 1 /nobreak >nul
    curl -sf "http://127.0.0.1:8766/health" >nul 2>&1
    if %errorlevel%==0 goto tts_ok
    set /a _t+=1
    if !_t! lss 6 goto tts_wait
    echo   [WARN] TTS sidecar did not respond within 6s — check logs\tts_sidecar.log
    goto tts_ok
    :tts_ok
    echo   [OK] TTS sidecar ready.
)

:: ─────────────────────────────────────────────────────────────────────────
:: 2. Ollama health check (advisory only — not managed here)
:: ─────────────────────────────────────────────────────────────────────────
echo Checking Ollama (:11434)...
curl -sf "http://127.0.0.1:11434/api/tags" >nul 2>&1
if %errorlevel%==0 (
    echo   [OK] Ollama running.
) else (
    echo   [WARN] Ollama not detected on :11434 — start the Ollama tray app first.
    echo          Voice commands will fall back to Anthropic cloud.
)

:: ─────────────────────────────────────────────────────────────────────────
:: 3. Main agent (:8765) — runs in its own visible window
:: ─────────────────────────────────────────────────────────────────────────
echo Checking main agent (:8765)...
netstat -ano | findstr "LISTENING" | findstr ":8765" >nul 2>&1
if %errorlevel%==0 (
    echo   [SKIP] Agent already listening on :8765.
) else (
    echo   [START] Main agent...
    start "Personal Desktop Agent :8765" /D "%ROOT%" cmd /k "title Personal Desktop Agent :8765 && "%PY%" main.py"
)

:: ─────────────────────────────────────────────────────────────────────────
echo.
echo ═══════════════════════════════════════════════════════
echo   Personal Desktop Agent started.
echo   iPad WebSocket : ws://[your-pc-ip]:8765
echo   TTS sidecar    : http://127.0.0.1:8766/health
echo   Logs           : %ROOT%logs\
echo ═══════════════════════════════════════════════════════
echo.
echo This window will close in 5 seconds.
timeout /t 5 /nobreak >nul
endlocal
