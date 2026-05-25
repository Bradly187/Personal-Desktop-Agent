# Metrics & Prometheus

This project exposes a lightweight Prometheus metrics endpoint for ContinuousTrainer and routing metrics.

How to enable

1. Install dependencies (from repo root):

   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt

2. Start the agent with the metrics server (default port 8000):

   # Optionally set METRICS_PORT
   set METRICS_PORT=8000
   python main.py --full

3. View metrics

   Open http://localhost:8000/metrics or run:

   curl http://localhost:8000/metrics

Notes

- Metrics are optional: the code falls back to no-ops when prometheus_client isn't installed.
- Useful metrics: continuous_trainer_adaptation_pass_total, continuous_trainer_gate1_cloud_rate, gesture_confidence_floor, hybrid_route_total, hybrid_route_latency_ms.
