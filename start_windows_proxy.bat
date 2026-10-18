@echo off
setlocal
:: start_windows_proxy.bat — start the authenticated Windows proxy for WSL.

set "PORT=8768"
set "LOG=logs\windows_proxy.log"
set "SECRET_DIR=%~dp0..\Personal_Desktop_Agent-secrets"
set "TOKEN_FILE=%SECRET_DIR%\windows_proxy.token"

cd /d "%~dp0"
if not exist logs mkdir logs
if not exist "%SECRET_DIR%" mkdir "%SECRET_DIR%"
if errorlevel 1 (
    echo [proxy] Failed to create the external proxy secret directory.
    exit /b 1
)

:: Keep the bootstrap credential outside PROJ_ROOT and replace each DACL with
:: one protected current-SID grant. The helper verifies the effective ACL.
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0scripts\Set-ProxySecretAcl.ps1" -Path "%SECRET_DIR%" -Directory >nul 2>&1
if errorlevel 1 (
    echo [proxy] Failed to establish exclusive proxy secret directory permissions.
    exit /b 1
)
if not exist "%TOKEN_FILE%" (
    ".venv\Scripts\python.exe" -c "from pathlib import Path; import secrets; Path(r'%TOKEN_FILE%').write_text(secrets.token_hex(32), encoding='utf-8')"
    if errorlevel 1 (
        echo [proxy] Failed to generate authentication token.
        exit /b 1
    )
)
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0scripts\Set-ProxySecretAcl.ps1" -Path "%TOKEN_FILE%" >nul 2>&1
if errorlevel 1 (
    echo [proxy] Failed to establish exclusive authentication token permissions.
    exit /b 1
)
set /p WSL_ACTION_PROXY_TOKEN=<"%TOKEN_FILE%"
if not defined WSL_ACTION_PROXY_TOKEN (
    echo [proxy] Authentication token is empty.
    exit /b 1
)
for /f "usebackq delims=" %%I in (`".venv\Scripts\python.exe" -c "import secrets; print(secrets.token_hex(16))"`) do set "WSL_PROXY_CHALLENGE=%%I"
if not defined WSL_PROXY_CHALLENGE (
    echo [proxy] Failed to create readiness challenge.
    exit /b 1
)

:: /ready returns instance_id plus HMAC(token, challenge + instance_id). The
:: helper parses JSON and prints only a verified instance identifier.
call :check_ready
if defined READY_INSTANCE (
    echo [proxy] Already running on :%PORT%
    exit /b 0
)

:: A foreign, stale-token, unready, or legacy process may still answer health.
curl -s "http://127.0.0.1:%PORT%/health" >nul 2>&1
if not errorlevel 1 (
    echo [proxy] A different or unready proxy is running on :%PORT%; stop it and retry.
    exit /b 1
)

echo [proxy] Starting Windows Action Proxy on :%PORT% ...
echo [proxy] Log: %LOG%

start /B "" ".venv\Scripts\python.exe" windows_action_proxy.py --port %PORT% >> %LOG% 2>&1

:: Wait up to 5 seconds for it to come up.
set /a tries=0
:wait_loop
timeout /t 1 /nobreak >nul
call :check_ready
if defined READY_INSTANCE (
    echo [proxy] Ready on http://127.0.0.1:%PORT%
    exit /b 0
)
set /a tries+=1
if %tries% lss 5 goto wait_loop

echo [warn] Proxy did not start within 5s — check %LOG%
exit /b 1

:check_ready
set "READY_INSTANCE="
set "READY_FILE=%TEMP%\personal_desktop_agent_proxy_ready_%RANDOM%.json"
curl -fsS "http://127.0.0.1:%PORT%/ready?challenge=%WSL_PROXY_CHALLENGE%" > "%READY_FILE%" 2>nul
if errorlevel 1 (
    del "%READY_FILE%" >nul 2>&1
    exit /b 0
)
for /f "usebackq delims=" %%I in (`".venv\Scripts\python.exe" scripts\verify_proxy_ready.py "%READY_FILE%"`) do set "READY_INSTANCE=%%I"
del "%READY_FILE%" >nul 2>&1
exit /b 0
