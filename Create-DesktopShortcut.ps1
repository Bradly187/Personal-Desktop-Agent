# Create-DesktopShortcut.ps1
# Creates a "Desktop Agent" shortcut on the Windows desktop that launches
# start_desktop.bat with a custom icon (if available).
#
# Run once:  powershell -ExecutionPolicy Bypass -File Create-DesktopShortcut.ps1

$root      = Split-Path $MyInvocation.MyCommand.Definition -Parent
$launcher  = Join-Path $root "start_desktop.bat"
$desktop   = [Environment]::GetFolderPath("Desktop")
$shortcut  = Join-Path $desktop "Desktop Agent.lnk"

$wsh    = New-Object -ComObject WScript.Shell
$lnk    = $wsh.CreateShortcut($shortcut)

$lnk.TargetPath      = $launcher
$lnk.WorkingDirectory = $root
$lnk.WindowStyle     = 1   # 1 = normal window
$lnk.Description     = "Launch Personal Desktop Agent (iPad sensor bridge + voice)"

# Use a built-in Windows icon if no custom one exists
$iconCandidates = @(
    (Join-Path $root "assets\agent.ico"),
    "$env:SystemRoot\System32\imageres.dll,27"   # microphone icon
)
foreach ($candidate in $iconCandidates) {
    $parts = $candidate -split ","
    if (Test-Path $parts[0]) {
        $lnk.IconLocation = $candidate
        break
    }
}

$lnk.Save()
Write-Host "Shortcut created: $shortcut"

# Also offer to add to the Startup folder so it runs at login
$startupFolder = [Environment]::GetFolderPath("Startup")
$startupShortcut = Join-Path $startupFolder "Desktop Agent.lnk"
$resp = Read-Host "Add to Windows Startup folder so it runs at login? [y/N]"
if ($resp -match "^[Yy]") {
    $lnk2 = $wsh.CreateShortcut($startupShortcut)
    $lnk2.TargetPath       = $launcher
    $lnk2.WorkingDirectory  = $root
    $lnk2.WindowStyle      = 7   # 7 = minimised — unobtrusive at login
    $lnk2.Description      = "Personal Desktop Agent autostart"
    $lnk2.IconLocation     = $lnk.IconLocation
    $lnk2.Save()
    Write-Host "Startup shortcut created: $startupShortcut"
} else {
    Write-Host "Skipped Startup folder. To add later, re-run this script."
}
