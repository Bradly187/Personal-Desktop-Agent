Windows Exporter (windows_exporter) - example and Prometheus scrape

Install (Windows):
1. Download windows_exporter from https://github.com/prometheus-community/windows_exporter/releases
2. Unzip and install as service (run as Administrator):
   .\windows_exporter.exe --config.file=windows_exporter.yml --collector.process
   sc.exe create windows_exporter binPath= "C:\path\to\windows_exporter.exe" start= auto
3. Configure Windows firewall to allow scrape port (default 9182).

Prometheus scrape example (add to prometheus.yml or include):

scrape_configs:
  - job_name: 'windows_exporter'
    static_configs:
      - targets: ['HOSTNAME_OR_IP:9182']
        labels:
          role: windows

Notes:
- Enable the 'process' collector to expose per-process metrics (process_cpu_seconds_total, process_resident_memory_bytes).
- For production, run the exporter as a dedicated monitoring user and restrict network access.
