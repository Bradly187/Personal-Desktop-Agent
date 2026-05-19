@echo off
cd /d "E:\Personal_Desktop_Agent"

:: Redirect stdout+stderr to a rolling log file (overwritten each boot)
set LOG="E:\Personal_Desktop_Agent\logs\agent_startup.log"
if not exist "E:\Personal_Desktop_Agent\logs" mkdir "E:\Personal_Desktop_Agent\logs"

echo [%DATE% %TIME%] Starting Personal Desktop Agent >> %LOG%

".venv\Scripts\python.exe" main.py >> %LOG% 2>&1
