# RAM Disk Setup — Hot Data Paths on 192 GB RAM

Roadmap item #4: Move `agent.db` and `chroma_db/` to a RAM disk to eliminate
all disk I/O latency from the command path. At ~4ms per aiosqlite write, this
saves 4–8ms per routed command at 60 Hz operation.

## Tool: ImDisk Toolkit (free)

Download: https://sourceforge.net/projects/imdisk-toolkit/

## Setup

Run `setup_ramdisk.bat` as Administrator, or follow these manual steps:

```bat
REM 1. Create a 32 GB RAM disk at R:\
REM    (ImDisk command-line tool ships with ImDisk Toolkit)
imdisk -a -s 32G -m R: -p "/fs:NTFS /q /y"

REM 2. Copy existing DBs to RAM disk
if exist agent.db         copy agent.db        R:\agent.db
if exist chroma_db\       xcopy /e /i chroma_db R:\chroma_db\
if exist gaze_calibration.json copy gaze_calibration.json R:\gaze_calibration.json

REM 3. Replace originals with symlinks pointing to RAM disk
REM    (run as Administrator)
del agent.db
mklink agent.db R:\agent.db

rd /s /q chroma_db
mklink /d chroma_db R:\chroma_db

REM 4. On shutdown, rsync back to SSD
REM    (setup_ramdisk.bat registers a scheduled task for this)
```

## Persistence on Shutdown

The RAM disk is volatile — contents are lost on power loss or reboot.
`setup_ramdisk.bat` creates a Windows Task Scheduler task that runs on system
shutdown and copies `R:\agent.db` and `R:\chroma_db\` back to the project directory.

```bat
REM Registered shutdown task command:
xcopy /e /i /y R:\chroma_db E:\Personal_Desktop_Agent\chroma_db_backup\
copy /y R:\agent.db E:\Personal_Desktop_Agent\agent_backup.db
```

## Expected Impact

| Operation | Before (SSD) | After (RAM disk) |
|-----------|-------------|-----------------|
| aiosqlite WAL write | ~4 ms | ~0.1 ms |
| ChromaDB query (mmap) | ~80 ms | ~5 ms |
| ChromaDB add (embedding write) | ~20 ms | ~2 ms |
| Session summary persist | ~15 ms | ~1 ms |

Total savings per command: ~4–8 ms. At 60 Hz with 30% command rate: ~1.2–2.4 ms average.
Largest win is RAG retrieval in DevAgent plan generation (was 80ms → 5ms).

## VRAM vs RAM trade-off

32 GB RAM disk uses 16.7% of 192 GB RAM. 160 GB remains for OS, Python, and
future llama.cpp CPU-layer offload for 72B models. This is a sound trade-off.
