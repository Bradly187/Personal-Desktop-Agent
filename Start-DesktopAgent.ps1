# Start-DesktopAgent.ps1
# Launched by the desktop shortcut.  Starts the Windows action proxy silently,
# then opens Windows Terminal running the WSL agent.
#
# Do not run this directly — use the desktop shortcut created by
# Install-DesktopShortcut.ps1, or double-click it from Explorer.

param(
    [int]$ProxyPort   = 8768,
    [string]$WslDistro = "Ubuntu",
    [string]$ProjectRoot = "E:\Personal_Desktop_Agent"
)

$ProxyUrl    = "http://127.0.0.1:$ProxyPort/health"
$PythonW     = "$ProjectRoot\.venv\Scripts\pythonw.exe"   # no console window
$ProxyScript = "$ProjectRoot\windows_action_proxy.py"
$WslScript   = "/mnt/e/Personal_Desktop_Agent/start_agent_wsl.sh"
$Wt          = "$env:LOCALAPPDATA\Microsoft\WindowsApps\wt.exe"

# ── Helper: test if proxy is already answering ────────────────────────────────
function Test-Proxy {
    try {
        $r = Invoke-WebRequest -Uri $ProxyUrl -UseBasicParsing -TimeoutSec 1 -ErrorAction Stop
        return $r.StatusCode -eq 200
    } catch { return $false }
}

# ── 1. Windows action proxy ───────────────────────────────────────────────────
if (Test-Proxy) {
    Write-Host "[proxy] Already running on :$ProxyPort"
} else {
    Write-Host "[proxy] Starting Windows action proxy..."
    Start-Process `
        -FilePath      $PythonW `
        -ArgumentList  "$ProxyScript --port $ProxyPort" `
        -WorkingDirectory $ProjectRoot `
        -WindowStyle   Hidden

    # Wait up to 8 seconds for proxy to come up
    $deadline = (Get-Date).AddSeconds(8)
    $ready = $false
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 400
        if (Test-Proxy) { $ready = $true; break }
    }
    if ($ready) {
        Write-Host "[proxy] Ready on :$ProxyPort"
    } else {
        Write-Host "[warn]  Proxy did not respond — desktop control may fail."
        Write-Host "        Check logs\windows_proxy.log for errors."
    }
}

# ── 2. WSL agent in Windows Terminal ─────────────────────────────────────────
Write-Host "[agent] Opening Windows Terminal → WSL agent..."
$WslCmd = "wsl.exe -d $WslDistro -- bash $WslScript"

if (Test-Path $Wt) {
    # Windows Terminal: new window, "Ubuntu" profile, descriptive title
    Start-Process $Wt -ArgumentList `
        "new-tab --profile `"$WslDistro`" --title `"Desktop Agent`" $WslCmd"
} else {
    # Fallback: plain cmd window
    Write-Host "[warn]  Windows Terminal not found; using cmd.exe"
    Start-Process "cmd.exe" -ArgumentList "/c $WslCmd"
}
