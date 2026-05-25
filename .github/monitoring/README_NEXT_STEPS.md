Next steps to validate observability locally

1. Run the monitoring stack (docker-compose):
   cd .github/monitoring
   docker-compose up -d

2. Start the agent locally (METRICS_PORT=8000) and confirm Prometheus scrapes http://host.docker.internal:8000/metrics

3. Import Grafana dashboard (.github/grafana/agent_dashboard.json) and wire datasources.

4. Deploy promtail on the host (or use docker promtail) to ship logs to Loki.

5. Instrument adaptation code with instrumentation/telemetry.py tracer and run Otto Collector for traces.

If you want, run a smoke test script next to validate /metrics and logs ingestion.
