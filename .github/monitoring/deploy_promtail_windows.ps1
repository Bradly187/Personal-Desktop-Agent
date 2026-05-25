# Download and install promtail on Windows (example)
$version = "2.8.2"
$zip = "promtail-windows-amd64.zip"
$url = "https://github.com/grafana/loki/releases/download/v$version/$zip"

$installDir = "C:\\opt\\promtail"
New-Item -ItemType Directory -Path $installDir -Force | Out-Null

$zipPath = Join-Path $installDir $zip
Invoke-WebRequest -Uri $url -OutFile $zipPath
Expand-Archive -LiteralPath $zipPath -DestinationPath $installDir -Force

# Copy example config (ensure promtail-config.yml exists in same folder)
Copy-Item -Path "promtail-config.yml" -Destination $installDir -Force

# Register as a service using nssm (example) or sc.exe as needed
Write-Host "Promtail downloaded to $installDir. Configure promtail-config.yml and run promtail.exe with the -config.file flag."