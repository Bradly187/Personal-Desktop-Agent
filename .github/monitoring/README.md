Prometheus & Grafana monitoring examples for Personal Desktop Agent

Files:
- prometheus-scrape.yml — example Prometheus scrape job for the agent (/metrics at port 8000)
- grafana/agent_dashboard.json — Grafana dashboard JSON skeleton to import

How to use:
1. Copy prometheus-scrape.yml into your Prometheus server and include it, or merge the scrape_configs block into your prometheus.yml.
2. Configure the target host:port to the machine where the agent exposes /metrics (METRICS_PORT).
3. Import grafana/agent_dashboard.json into Grafana (Dashboards → Import) and wire the datasource.

Notes:
- The scrape job uses a plain static target for development. For many-host deployments, prefer file_sd_configs or Kubernetes service discovery.
- The dashboard is a skeleton — adapt panels and data source names to your Grafana setup.
