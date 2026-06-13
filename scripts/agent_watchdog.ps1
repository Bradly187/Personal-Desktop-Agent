<#
.SYNOPSIS
    Process-level crash recovery for the Personal Desktop Agent.

.DESCRIPTION
    Runs the target process (main agent or Windows action proxy) in a loop:
      - exit code 0  -> intentional stop, watchdog exits
      - non-zero     -> crash, restart with backoff (5s / 10s / 20s)
      - bound        -> 3 restarts within a rolling 10-minute window -> give up
                        (logged loudly; the agent stays down until the user or
                        the next logon starts it again)

    Designed to be launched by the scheduled tasks that
    scripts\Install-AgentService.ps1 registers, but can be run by hand:

        powershell -File scripts\agent_watchdog.ps1 -Target agent
        powershell -File scripts\agent_watchdog.ps1 -Target proxy

    The agent's own graceful shutdown (Ctrl-C / SIGTERM) exits 0, so stopping
    it deliberately does NOT fight the watchdog. Killing the process (or a
    crash) exits non-zero and is restarted. PowerShell 5.1 compatible; no
    external dependencies.

.PARAMETER Target
    agent  -> main.py            (iPad bridge :8765, full pipeline)
    proxy  -> windows_action_proxy.py --port 8768  (WSL-mode action proxy)

.PARAMETER TestCommand
    Test hook: run this command line (via cmd /c) instead of the real target,
    and skip the already-running guard. Lets the restart/backoff logic be
    exercised without starting sensors or LLMs.

.PARAMETER TestChild
    Test hook: path to a Python script run IN PLACE of the real target, but
    through the real venv python, real log redirection, and real cmd quoting —
    unlike -TestCommand, this exercises the production launch path end-to-end.
    Skips the already-running guard.

.PARAMETER TestLogDir
    Test hook: write all logs here instead of <repo>\logs (keeps test runs
    from polluting the real agent logs).
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("agent", "proxy")]
    [string]$Target,

    [string]$TestCommand = "",
    [string]$TestChild = "",
    [string]$TestLogDir = "",

    # Backoff bound: give up after $MaxRestarts crashes within $WindowSeconds.
    [int]$MaxRestarts = 3,
    [int]$WindowSeconds = 600
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Py = Join-Path $Root ".venv\Scripts\python.exe"
if ($TestLogDir) { $LogDir = $TestLogDir } else { $LogDir = Join-Path $Root "logs" }
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

switch ($Target) {
    "agent" {
        $childArgs = "main.py"
        $outLog = Join-Path $LogDir "agent_startup.log"
        $prevLog = Join-Path $LogDir "agent_startup.prev.log"   # preserved post-crash trail
        $watchLog = Join-Path $LogDir "watchdog_agent.log"
        $listenPort = 8765
        $healthUrl = ""
    }
    "proxy" {
        $childArgs = "windows_action_proxy.py --port 8768"
        $outLog = Join-Path $LogDir "windows_proxy.log"
        $prevLog = ""                                            # proxy log is append-only
        $watchLog = Join-Path $LogDir "watchdog_proxy.log"
        $listenPort = 0
        $healthUrl = "http://127.0.0.1:8768/health"
    }
}

function Write-WatchLog([string]$Message) {
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -Path $watchLog -Value $line -Encoding utf8
    Write-Host $line
}

# ── Already-running guard ────────────────────────────────────────────────────
# Another instance (manual start_agent.bat / start_windows_proxy.bat, or a
# second copy of this task) already owns the port -> nothing to do.
if (-not $TestCommand) {
    if (-not (Test-Path $Py)) {
        Write-WatchLog "ERROR: venv python not found at $Py - run setup first. Exiting."
        exit 1
    }
}
if (-not $TestCommand -and -not $TestChild) {
    $alreadyUp = $false
    if ($listenPort -gt 0) {
        try {
            $conn = Get-NetTCPConnection -LocalPort $listenPort -State Listen -ErrorAction Stop
            if ($conn) { $alreadyUp = $true }
        } catch {}
    }
    if ($healthUrl) {
        try {
            $resp = Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 2
            if ($resp.StatusCode -eq 200) { $alreadyUp = $true }
        } catch {}
    }
    if ($alreadyUp) {
        Write-WatchLog "$Target already running - watchdog not needed, exiting."
        exit 0
    }
}

# Test hook: substitute the child but keep the real launch path (quoting,
# redirection, exit-code propagation) intact.
if ($TestChild) {
    $childArgs = "`"$TestChild`""
}

# ── Supervision loop ─────────────────────────────────────────────────────────
$crashTimes = New-Object System.Collections.Generic.List[datetime]
$attempt = 0

while ($true) {
    $attempt++

    if ($TestCommand) {
        $cmdLine = $TestCommand
    } else {
        # Rotate the agent log (matches start_agent.bat behaviour) so a
        # post-crash trail survives the restart.
        if ($prevLog -and (Test-Path $outLog)) {
            Copy-Item -Path $outLog -Destination $prevLog -Force
        }
        if ($prevLog) {
            # agent: fresh log each start
            $cmdLine = "`"$Py`" $childArgs > `"$outLog`" 2>&1"
        } else {
            # proxy: append
            $cmdLine = "`"$Py`" $childArgs >> `"$outLog`" 2>&1"
        }
    }

    Write-WatchLog "starting $Target (attempt $attempt): $cmdLine"

    # cmd keeps stdout+stderr merged into one raw-byte log file (PowerShell
    # redirection would re-encode) and propagates the child's exit code.
    # /s + outer quotes is load-bearing: $cmdLine contains 4 quote chars, and
    # without /s cmd's strip-first-and-last-quote rule leaves the redirect
    # inside a quoted span ("The filename, directory name, or volume label
    # syntax is incorrect" - the child never starts). /s makes cmd strip
    # exactly the outer pair, preserving the inner quoting as written.
    $proc = Start-Process -FilePath "$env:ComSpec" -ArgumentList "/d /s /c `" $cmdLine `"" `
        -WorkingDirectory $Root -WindowStyle Hidden -PassThru
    $proc.WaitForExit()
    $code = $proc.ExitCode

    if ($code -eq 0) {
        Write-WatchLog "$Target exited cleanly (code 0) - intentional stop, watchdog exiting."
        exit 0
    }

    $now = Get-Date
    $crashTimes.Add($now)
    while ($crashTimes.Count -gt 0 -and ($now - $crashTimes[0]).TotalSeconds -gt $WindowSeconds) {
        $crashTimes.RemoveAt(0)
    }

    Write-WatchLog "$Target exited with code $code (crash $($crashTimes.Count)/$MaxRestarts in the last $WindowSeconds s)"

    if ($crashTimes.Count -ge $MaxRestarts) {
        Write-WatchLog "CRASH LOOP: $MaxRestarts crashes within $WindowSeconds s - giving up. $Target stays DOWN until restarted manually (Start-ScheduledTask) or at next logon. Check $outLog for the cause."
        # exit 0 on purpose: Task Scheduler's RestartCount is reserved for the
        # watchdog itself dying, not for re-running a crash-looping child.
        exit 0
    }

    $delay = 5 * [math]::Pow(2, $crashTimes.Count - 1)   # 5s, 10s, 20s
    Write-WatchLog "restarting $Target in $delay s"
    Start-Sleep -Seconds $delay
}
