# Install-DesktopShortcut.ps1
# Creates "Personal Desktop Agent" shortcut on the desktop.
# Run once from PowerShell (no admin required):
#   powershell -ExecutionPolicy Bypass -File E:\Personal_Desktop_Agent\Install-DesktopShortcut.ps1

$ProjectRoot  = "E:\Personal_Desktop_Agent"
$LaunchScript = "$ProjectRoot\Start-DesktopAgent.ps1"
$Desktop      = [Environment]::GetFolderPath("Desktop")
$ShortcutPath = "$Desktop\Personal Desktop Agent.lnk"

# ── Build the shortcut ────────────────────────────────────────────────────────
$Shell     = New-Object -ComObject WScript.Shell
$Shortcut  = $Shell.CreateShortcut($ShortcutPath)

# Target: powershell.exe hidden so no blue PS window flashes on click
$Shortcut.TargetPath       = "powershell.exe"
$Shortcut.Arguments        = "-ExecutionPolicy Bypass -WindowStyle Hidden -File `"$LaunchScript`""
$Shortcut.WorkingDirectory = $ProjectRoot
$Shortcut.Description      = "Start Personal Desktop Agent (vLLM + Gemma 4)"

# ── Icon: prefer Windows Terminal, fall back to shell32 robot/computer icon ──
$WtExe = "$env:LOCALAPPDATA\Microsoft\WindowsApps\wt.exe"
if (Test-Path $WtExe) {
    $Shortcut.IconLocation = "$WtExe,0"
} else {
    # shell32.dll index 22 = monitor + sparkle; 19 = settings cog
    $Shortcut.IconLocation = "$env:SystemRoot\System32\shell32.dll,22"
}

$Shortcut.Save()

Write-Host "Shortcut created: $ShortcutPath"
Write-Host ""
Write-Host "To use:"
Write-Host "  1. Run start_windows_proxy.bat OR just double-click the shortcut"
Write-Host "     (the shortcut starts the proxy automatically)"
Write-Host "  2. The Windows Terminal will open running the WSL agent"
Write-Host ""
Write-Host "To auto-mount E drive at WSL startup (eliminates 'sudo mount'):"
Write-Host "  Run in WSL: bash $($ProjectRoot -replace '\\','/')/setup_wsl_automount.sh"
