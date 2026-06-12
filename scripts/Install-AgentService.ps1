<#
.SYNOPSIS
    Register (or remove) the Personal Desktop Agent auto-start + crash-recovery
    scheduled tasks.

.DESCRIPTION
    Creates two Windows Task Scheduler tasks that run at logon of the current
    user, each launching scripts\agent_watchdog.ps1 hidden:

      PersonalDesktopAgent        -> main.py            (iPad bridge :8765)
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

$Tasks = @(
    @{ Name = "PersonalDesktopAgent";       Target = "agent"
       Description = "Personal Desktop Agent (main.py, iPad bridge :8765). Auto-starts at logon; watchdog restarts on crash (3x/10min bound)." },
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

foreach ($t in $Tasks) {
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Watchdog`" -Target $($t.Target)" `
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
