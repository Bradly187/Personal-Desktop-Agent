@echo off
REM setup_ramdisk.bat — Create 32 GB RAM disk for agent.db + chroma_db
REM Run as Administrator. Requires ImDisk Toolkit installed.
REM See docs/ramdisk_setup.md for full documentation.

echo.
echo === Personal Desktop Agent — RAM Disk Setup ===
echo.
echo This will create a 32 GB RAM disk at R:\ and symlink agent.db
echo and chroma_db/ to it. Run as Administrator.
echo.
pause

REM Check for admin
net session >nul 2>&1
if errorlevel 1 (
    echo [FAIL] This script must be run as Administrator.
    pause
    exit /b 1
)

REM Check ImDisk is installed
where imdisk >nul 2>&1
if errorlevel 1 (
    echo [FAIL] ImDisk not found. Download ImDisk Toolkit from:
    echo   https://sourceforge.net/projects/imdisk-toolkit/
    pause
    exit /b 1
)

REM Create 32 GB RAM disk formatted NTFS
echo [1/5] Creating 32 GB RAM disk at R:\...
imdisk -a -s 32G -m R: -p "/fs:NTFS /q /y"
if errorlevel 1 (
    echo [FAIL] Failed to create RAM disk. May already exist or insufficient RAM.
    pause
    exit /b 1
)
echo       Done.

REM Copy existing data to RAM disk
echo [2/5] Copying existing data to RAM disk...
set PROJ=E:\Personal_Desktop_Agent
if exist "%PROJ%\agent.db" (
    copy "%PROJ%\agent.db" R:\agent.db
    echo       Copied agent.db
)
if exist "%PROJ%\chroma_db" (
    xcopy /e /i /q "%PROJ%\chroma_db" R:\chroma_db\
    echo       Copied chroma_db
)
if exist "%PROJ%\gaze_calibration.json" (
    copy "%PROJ%\gaze_calibration.json" R:\gaze_calibration.json
    echo       Copied gaze_calibration.json
)

REM Create symlinks
echo [3/5] Creating symlinks...
if exist "%PROJ%\agent.db" del "%PROJ%\agent.db"
mklink "%PROJ%\agent.db" R:\agent.db
if errorlevel 1 (echo [WARN] Could not symlink agent.db — check permissions)

if exist "%PROJ%\chroma_db" rd /s /q "%PROJ%\chroma_db"
mklink /d "%PROJ%\chroma_db" R:\chroma_db
if errorlevel 1 (echo [WARN] Could not symlink chroma_db — check permissions)

REM Register shutdown task to persist RAM disk back to SSD
echo [4/5] Registering shutdown sync task...
set SYNC_CMD=xcopy /e /i /y R:\chroma_db "%PROJ%\chroma_db_ssd\" & copy /y R:\agent.db "%PROJ%\agent_ssd.db"
schtasks /create /tn "DesktopAgent_RAMDisk_Sync" /tr "cmd /c %SYNC_CMD%" /sc onevent /ec system /mo "*[System[Provider[@Name='Microsoft-Windows-Kernel-Power'] and (EventID=42 or EventID=109)]]" /ru SYSTEM /f >nul 2>&1
echo       Shutdown sync task registered (or already exists).

REM Update .gitignore to exclude RAM disk symlinks from accidental commits
echo [5/5] Updating .gitignore...
findstr /c:"chroma_db" "%PROJ%\.gitignore" >nul 2>&1
if errorlevel 1 (
    echo chroma_db/ >> "%PROJ%\.gitignore"
    echo agent.db   >> "%PROJ%\.gitignore"
)

echo.
echo === RAM Disk Setup Complete ===
echo.
echo R:\ is now a 32 GB RAM disk. Symlinks created:
echo   agent.db  → R:\agent.db
echo   chroma_db → R:\chroma_db
echo.
echo IMPORTANT: RAM disk is volatile — data is synced to SSD on shutdown via Task Scheduler.
echo For manual sync: xcopy /e /i /y R:\chroma_db "%PROJ%\chroma_db_ssd\"
echo.
pause
