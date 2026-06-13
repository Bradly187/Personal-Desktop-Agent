# One-time firewall remediation for Personal Desktop Agent (run elevated).
# 1) Scoped allow rules for the agent's venv python (iPad bridge + mDNS)
# 2) Re-enable the Private-profile firewall
# 3) Reclassify the WireGuard tunnel as Private
# (OnVUE leftover-rule cleanup intentionally excluded — needs separate user confirmation)
$ErrorActionPreference = 'Continue'
$log = 'E:\Personal_Desktop_Agent\logs\fix_firewall.log'
Start-Transcript -Path $log -Force | Out-Null

$python = 'E:\Personal_Desktop_Agent\.venv\Scripts\python.exe'
$scope  = @('LocalSubnet', '10.99.0.0/24')

foreach ($name in 'Desktop Agent - iPad bridge (TCP 8765)', 'Desktop Agent - mDNS discovery (UDP 5353)') {
    Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue | Remove-NetFirewallRule
}

New-NetFirewallRule -DisplayName 'Desktop Agent - iPad bridge (TCP 8765)' `
    -Direction Inbound -Action Allow -Program $python `
    -Protocol TCP -LocalPort 8765 -RemoteAddress $scope -Profile Any | Out-Null
Write-Output 'Added: iPad bridge rule (TCP 8765, LocalSubnet + 10.99.0.0/24)'

New-NetFirewallRule -DisplayName 'Desktop Agent - mDNS discovery (UDP 5353)' `
    -Direction Inbound -Action Allow -Program $python `
    -Protocol UDP -LocalPort 5353 -RemoteAddress $scope -Profile Any | Out-Null
Write-Output 'Added: mDNS discovery rule (UDP 5353)'

Set-NetFirewallProfile -Profile Private -Enabled True
Write-Output 'Private-profile firewall re-enabled'

Set-NetConnectionProfile -InterfaceAlias 'DesktopAgent' -NetworkCategory Private
Write-Output 'WireGuard tunnel (DesktopAgent) reclassified as Private'

Write-Output 'DONE'
Stop-Transcript | Out-Null
