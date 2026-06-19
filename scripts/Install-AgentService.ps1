<#
.SYNOPSIS
    Register (or remove) the Personal Desktop Agent auto-start + crash-recovery
    scheduled tasks.

.DESCRIPTION
    Creates two Windows Task Scheduler tasks that run at logon of the current
    user, each launching scripts\agent_watchdog.ps1 hidden:

      PersonalDesktopAgent        -> main.py --chat     (iPad bridge :8765, dashboard :8770)
      PersonalDesktopAgent-Proxy  -> windows_action_proxy.py (:8768, WSL mode)

    The watchdog restarts its target on crash with bounded backoff
    (3 restarts / 10 min, then stop + log). Tasks run interactively as the
    registering user (required: pyautogui needs the interactive desktop), have
    no execution time limit, ignore duplicate launches, and restart the
    watchdog itself up to 3 times if IT dies.

    No admin rights required. Native Task Scheduler only - no NSSM/services.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\Install-AgentService.ps1
    # register both tasks and start them now

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\Install-AgentService.ps1 -NoStart
    # register only; tasks first run at next logon

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\Install-AgentService.ps1 -Uninstall
    # remove both tasks (does not stop a currently running agent)
#>
[CmdletBinding()]
param(
    [switch]$Uninstall,
    [switch]$NoStart
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Watchdog = Join-Path $Root "scripts\agent_watchdog.ps1"
# Hidden launcher: an interactive scheduled task that runs powershell.exe shows a
# console window even with -WindowStyle Hidden. Launching via wscript.exe + this
# .vbs (WshShell.Run window-style 0) starts the watchdog with no visible window.
$Launcher = Join-Path $Root "scripts\run_hidden.vbs"

$Tasks = @(
    @{ Name = "PersonalDesktopAgent";       Target = "agent"
       Description = "Personal Desktop Agent (main.py --chat, iPad bridge :8765, dashboard :8770). Auto-starts at logon; watchdog restarts on crash (3x/10min bound)." },
    @{ Name = "PersonalDesktopAgent-Proxy"; Target = "proxy"
       Description = "Windows Action Proxy (windows_action_proxy.py :8768) for WSL-mode accessibility. Auto-starts at logon; watchdog restarts on crash (3x/10min bound)." }
)

if ($Uninstall) {
    foreach ($t in $Tasks) {
        $existing = Get-ScheduledTask -TaskName $t.Name -ErrorAction SilentlyContinue
        if ($existing) {
            Unregister-ScheduledTask -TaskName $t.Name -Confirm:$false
            Write-Host "Removed scheduled task: $($t.Name)"
        } else {
            Write-Host "Not registered (skipped): $($t.Name)"
        }
    }
    Write-Host "Done. Note: a currently running agent/proxy is NOT stopped by uninstall."
    return
}

if (-not (Test-Path $Watchdog)) {
    throw "Watchdog script not found: $Watchdog"
}
if (-not (Test-Path $Launcher)) {
    throw "Hidden launcher not found: $Launcher"
}

foreach ($t in $Tasks) {
    # Launch the watchdog through wscript.exe + run_hidden.vbs so no PowerShell
    # console window appears at logon (the .vbs starts it with a hidden window).
    $action = New-ScheduledTaskAction -Execute "wscript.exe" `
        -Argument "`"$Launcher`" $($t.Target)" `
        -WorkingDirectory $Root

    $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"

    # ExecutionTimeLimit zero = unlimited (the default 72h limit would kill a
    # long-running agent). RestartCount/Interval guard the WATCHDOG process
    # itself; the child's crash backoff lives in agent_watchdog.ps1.
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -MultipleInstances IgnoreNew `
        -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
        -StartWhenAvailable

    Register-ScheduledTask -TaskName $t.Name -Action $action -Trigger $trigger `
        -Settings $settings -Description $t.Description -Force | Out-Null
    Write-Host "Registered scheduled task: $($t.Name)  (at logon, $env:USERDOMAIN\$env:USERNAME)"

    if (-not $NoStart) {
        Start-ScheduledTask -TaskName $t.Name
        Write-Host "  started."
    }
}

Write-Host ""
Write-Host "Manage with:"
Write-Host "  Get-ScheduledTask  -TaskName 'PersonalDesktopAgent*'"
Write-Host "  Stop-ScheduledTask -TaskName 'PersonalDesktopAgent'        # hard-stops (next start will announce a crash restart)"
Write-Host "  Start-ScheduledTask -TaskName 'PersonalDesktopAgent'"
Write-Host "Watchdog logs: logs\watchdog_agent.log / logs\watchdog_proxy.log"
