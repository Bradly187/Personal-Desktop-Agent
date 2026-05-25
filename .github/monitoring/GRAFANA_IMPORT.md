Grafana import steps

1. Start Grafana (default http://localhost:3000). Login (admin/admin default credentials).
2. Dashboards -> Import -> Upload the JSON file
   - File: .github/grafana/agent_dashboard.json
   - Select Prometheus datasource
3. Adjust template variables: set 'instance' to the host you scrape (e.g., host.docker.internal:8000) and set datasources for Prometheus and Loki.

Notes:
- If panels show no data, verify Prometheus is scraping the targets and that labels include 'instance'.
- For Loki logs, ensure promtail is configured with correct __path__ and Loki endpoint.
