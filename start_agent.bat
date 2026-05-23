@echo off
cd /d "E:\Personal_Desktop_Agent"

:: Redirect stdout+stderr to a rolling log file (overwritten each boot).
:: Previous boot's log is preserved as agent_startup.prev.log so a post-crash
:: trail survives the next restart.
set LOG="E:\Personal_Desktop_Agent\logs\agent_startup.log"
set PREV="E:\Personal_Desktop_Agent\logs\agent_startup.prev.log"
if not exist "E:\Personal_Desktop_Agent\logs" mkdir "E:\Personal_Desktop_Agent\logs"

if exist %LOG% copy /Y %LOG% %PREV% > nul

echo [%DATE% %TIME%] Starting Personal Desktop Agent > %LOG%

".venv\Scripts\python.exe" main.py >> %LOG% 2>&1
