@echo off
cd /d "E:\Personal_Desktop_Agent"

:: RealSense L515 capture sidecar — runs in the Python 3.10 venv (.venv-realsense)
:: because the L515 needs pyrealsense2 <= 2.54 (CPython 3.7-3.11 only).
:: Start the main app first (python main.py) so the bridge is listening on :8765,
:: then run this. The sidecar auto-reconnects if the bridge restarts.

set LOG="E:\Personal_Desktop_Agent\logs\realsense.log"
set PREV="E:\Personal_Desktop_Agent\logs\realsense.prev.log"
if not exist "E:\Personal_Desktop_Agent\logs" mkdir "E:\Personal_Desktop_Agent\logs"

if exist %LOG% copy /Y %LOG% %PREV% > nul

if not exist ".venv-realsense\Scripts\python.exe" (
    echo [ERROR] .venv-realsense not found. Create it with Python 3.10:
    echo     "C:\Users\bradt\AppData\Local\Programs\Python\Python310\python.exe" -m venv .venv-realsense
    echo     .venv-realsense\Scripts\python.exe -m pip install -r requirements-realsense.txt
    exit /b 1
)

echo [%DATE% %TIME%] Starting RealSense L515 sidecar > %LOG%

".venv-realsense\Scripts\python.exe" sensors\realsense_publisher.py %* >> %LOG% 2>&1
