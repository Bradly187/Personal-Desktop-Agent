<#
.SYNOPSIS
    Adopt the Electron desktop shell (desktop_app/) as the daily entry point,
    or revert to the watchdog with -Uninstall.

.DESCRIPTION
    Adoption (default):
      1. Disables the PersonalDesktopAgent watchdog logon task (the shell owns
         the backend lifecycle instead; spec desktop-app-shell R2).
         PersonalDesktopAgent-Proxy is left untouched - the :8768 WSL action
         proxy is independent of the shell.
      2. Registers PersonalDesktopAgentShell: at-logon task launching the
         shell's electron.exe directly (visible GUI window). Same PT1M delay
         as the watchdog task - the repo's ReFS E: volume mounts after logon.
      3. Creates a "Desktop Agent" shortcut on the Desktop for manual launches.

    -Uninstall reverts: removes the shell task + shortcut and re-enables the
    watchdog task.

    Closing the shell window stops an OWNED backend (that is the ownership
    model). If the backend was already running when the shell started, the
    shell attaches and closing leaves it alone.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\Install-ShellEntrypoint.ps1
#>
[CmdletBinding()]
param(
    [switch]$Uninstall,
    [switch]$NoStart
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$AppDir = Join-Path $Root "desktop_app"
$Electron = Join-Path $AppDir "node_modules\electron\dist\electron.exe"
$ShellTask = "PersonalDesktopAgentShell"
$WatchdogTask = "PersonalDesktopAgent"
$ShortcutPath = Join-Path ([Environment]::GetFolderPath("Desktop")) "Desktop Agent.lnk"

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $ShellTask -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $ShellTask -Confirm:$false
        Write-Host "Removed scheduled task: $ShellTask"
    }
    if (Test-Path $ShortcutPath) {
        Remove-Item $ShortcutPath -Confirm:$false
        Write-Host "Removed shortcut: $ShortcutPath"
    }
    if (Get-ScheduledTask -TaskName $WatchdogTask -ErrorAction SilentlyContinue) {
        Enable-ScheduledTask -TaskName $WatchdogTask | Out-Null
        Write-Host "Re-enabled watchdog task: $WatchdogTask (takes effect next logon)"
    }
    return
}

if (-not (Test-Path $Electron)) {
    throw "Electron not installed: $Electron  - run 'npm install' in desktop_app\ first."
}

# 1. Watchdog task off (shell owns the backend now).
if (Get-ScheduledTask -TaskName $WatchdogTask -ErrorAction SilentlyContinue) {
    Disable-ScheduledTask -TaskName $WatchdogTask | Out-Null
    Write-Host "Disabled watchdog task: $WatchdogTask  (revert: Enable-ScheduledTask $WatchdogTask)"
} else {
    Write-Host "Watchdog task not registered (skipped): $WatchdogTask"
}

# 2. Shell logon task. GUI app - runs visibly in the interactive session.
$action = New-ScheduledTaskAction -Execute $Electron `
    -Argument "`"$AppDir`"" -WorkingDirectory $AppDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
# E: (ReFS) mounts after -AtLogOn fires - same race and fix as the watchdog task.
$trigger.Delay = "PT1M"
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable
Register-ScheduledTask -TaskName $ShellTask -Action $action -Trigger $trigger `
    -Settings $settings -Description ("Desktop Agent shell (desktop_app/ Electron). " +
    "Auto-starts at logon and owns the backend (spawns main.py --chat --chat-no-browser).") `
    -Force | Out-Null
Write-Host "Registered scheduled task: $ShellTask  (at logon, $env:USERDOMAIN\$env:USERNAME)"

# 3. Desktop shortcut for manual launches.
$wsh = New-Object -ComObject WScript.Shell
$lnk = $wsh.CreateShortcut($ShortcutPath)
$lnk.TargetPath = $Electron
$lnk.Arguments = "`"$AppDir`""
$lnk.WorkingDirectory = $AppDir
$lnk.Description = "Personal Desktop Agent shell"
$lnk.Save()
Write-Host "Created shortcut: $ShortcutPath"

if (-not $NoStart) {
    Start-ScheduledTask -TaskName $ShellTask
    Write-Host "Shell started."
}
