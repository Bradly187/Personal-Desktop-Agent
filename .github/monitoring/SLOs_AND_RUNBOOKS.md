SLOs and Runbooks for ContinuousTrainer & Agent Health

SLO examples:
1. Adaptation Loop Availability
   - SLI: Successful adaptation passes per 10-minute window (increase(continuous_trainer_adaptation_pass_total[10m]))
   - SLO: >= 1 adaptation pass per 10 minutes (99.9% monthly)
   - Alert: LowAdaptationRate (fired when <1/10m for 10m)

2. Routing Latency
   - SLI: 95th percentile hybrid_route_latency_ms over 5m
   - SLO: p95 < 500ms
   - Alert: HighHybridRouteLatency (avg 5m > 500ms)

3. Metrics Endpoint
   - SLI: up{job="personal_desktop_agent"} == 1
   - SLO: 99.99% uptime
   - Alert: MetricsEndpointDown

Runbook: MetricsEndpointDown
- Severity: critical
- Symptoms: Alerts, Prometheus cannot scrape /metrics, no recent datapoints.
- Immediate checks:
  1. SSH / RDP to host and verify process is running: `tasklist | findstr python` or `Get-Service -Name AgentService`.
  2. Check service logs in C:\path\to\agent\logs for crash traces.
  3. Curl locally: `curl http://localhost:8000/metrics`.
  4. Restart service: `Restart-Service -Name AgentService` or restart python process.
- Post-mortem notes: collect logs, metrics, recent code changes (PRs), and correlate with deployments.

Runbook: LowAdaptationRate
- Severity: critical
- Symptoms: adaptation counter not increasing.
- Immediate checks:
  1. Inspect ContinuousTrainer logs for exceptions or DB errors.
  2. Verify DB connectivity and that the trainer scheduler is active.
  3. Run a manual adaptation dry-run (if available) and check metrics emission.
  4. If persistent, roll back recent changes or restart the process.

Runbook: HighHybridRouteLatency
- Severity: warning
- Symptoms: increased route latency, possible degraded UX.
- Immediate checks:
  1. Check network connectivity to cloud endpoints (ping/traceroute).
  2. Review cloud provider health and quotas.
  3. Identify which route label is slow: query `hybrid_route_latency_ms` by route in Prometheus.
  4. If cloud backend is slow, consider temporary local fallback.

Notes:
- Add on-call contact and escalation matrix in your team's runbook system.
- Link dashboards and logs for quick triage in alerts.
